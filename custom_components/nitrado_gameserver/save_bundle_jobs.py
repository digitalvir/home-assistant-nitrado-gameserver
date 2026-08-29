"""Bounded in-memory preparation jobs for slow save-bundle operations."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .const import DOMAIN, PROFILE_REGISTRY_TRANSITION_DATA_KEY
from .extensions import ProfileExtensionError
from .filesystem import FileTransportError

_LOGGER = logging.getLogger(__name__)

SAVE_BUNDLE_JOB_SECONDS = 45 * 60
SAVE_BUNDLE_JOB_RUN_SECONDS = 30 * 60
MAX_SAVE_BUNDLE_JOBS = 8
_DATA_KEY = f"{DOMAIN}_save_bundle_jobs"


@dataclass(slots=True)
class SaveBundleJob:
    """One private user/server-bound asynchronous preparation."""

    job_id: str
    user_id: str
    account_entry_id: str
    service_id: str
    bundle_key: str
    action: str
    created_at: float
    expires_at: float
    task: asyncio.Task[None] | None = None
    result: Any | None = None
    error: str | None = None
    stage: str = "queued"
    message: str = "Waiting to start save-package preparation…"
    progress_details: dict[str, int | str] = field(default_factory=dict)
    updated_at: float = 0.0
    cancel_requested: bool = False

    @property
    def status(self) -> str:
        if self.error is not None:
            return "error"
        if self.result is not None:
            return "ready"
        return "running"

    def update_progress(self, stage: str, message: str, details: Any) -> None:
        """Record bounded content-free progress for authenticated polling."""

        clean: dict[str, int | str] = {}
        if isinstance(details, dict):
            for key in (
                "files_done",
                "files_total",
                "bytes_done",
                "bytes_total",
                "directories_done",
            ):
                value = details.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    clean[key] = value
            current_file = details.get("current_file")
            if isinstance(current_file, str) and 0 < len(current_file) <= 1024:
                clean["current_file"] = current_file
        self.stage = str(stage)[:64] if stage else "working"
        self.message = str(message)[:512] if message else "Preparing the save package…"
        self.progress_details = clean
        self.updated_at = time.monotonic()

    def progress_payload(self) -> dict[str, Any]:
        """Return the current progress snapshot without provider internals."""

        payload: dict[str, Any] = {
            "stage": self.stage,
            "message": self.message,
            "elapsed_seconds": max(0, int(time.monotonic() - self.created_at)),
        }
        payload.update(self.progress_details)
        return payload


def _jobs(hass: Any) -> dict[str, SaveBundleJob]:
    jobs = hass.data.get(_DATA_KEY)
    if not isinstance(jobs, dict):
        jobs = {}
        hass.data[_DATA_KEY] = jobs
    return jobs


def _close_result(result: Any | None) -> None:
    close = getattr(result, "close", None)
    if callable(close):
        close()


def _discard(job: SaveBundleJob) -> None:
    if job.task is not None and not job.task.done():
        if job.cancel_requested:
            return
        job.cancel_requested = True
        job.expires_at = min(job.expires_at, time.monotonic())
        job.update_progress("draining", "Cancelling and safely draining ZIP work…", job.progress_details)
        job.task.cancel()
        return
    _close_result(job.result)
    job.result = None


def _prune(hass: Any) -> None:
    jobs = _jobs(hass)
    now = time.monotonic()
    for job_id, job in tuple(jobs.items()):
        if job.expires_at <= now:
            _discard(job)
            if job.task is None or job.task.done():
                jobs.pop(job_id, None)
    if len(jobs) > MAX_SAVE_BUNDLE_JOBS:
        oldest = sorted(jobs.values(), key=lambda item: item.created_at)[: len(jobs) - MAX_SAVE_BUNDLE_JOBS]
        for job in oldest:
            _discard(job)
            if job.task is None or job.task.done():
                jobs.pop(job.job_id, None)


def _matches(
    job: SaveBundleJob,
    *,
    user_id: str,
    account_entry_id: str,
    service_id: str,
    bundle_key: str,
    action: str,
) -> bool:
    return (
        job.user_id,
        job.account_entry_id,
        job.service_id,
        job.bundle_key,
        job.action,
    ) == (str(user_id), str(account_entry_id), str(service_id), str(bundle_key), str(action))


async def _run(
    job: SaveBundleJob,
    operation: Callable[[Callable[[str, str, Any], None]], Awaitable[Any]],
) -> None:
    try:
        job.update_progress("starting", "Starting save-package preparation…", {})
        async with asyncio.timeout(SAVE_BUNDLE_JOB_RUN_SECONDS):
            job.result = await operation(job.update_progress)
        job.update_progress("ready", "Save package ready for download.", job.progress_details)
    except asyncio.CancelledError:
        job.error = "Save-bundle preparation was cancelled after its ZIP worker drained safely."
    except TimeoutError:
        job.error = "Save-bundle preparation exceeded the 30-minute safety limit."
    except ProfileExtensionError as err:
        job.error = str(err)
    except FileTransportError as err:
        job.error = (
            "Nitrado FTPS could not finish reading the save tree. No server data was changed. "
            "Retry the download; if it repeats, check the Home Assistant log."
        )
        _LOGGER.warning(
            "Save-bundle %s job %s failed during provider filesystem read error_type=%s",
            job.action,
            job.job_id,
            err.__class__.__name__,
        )
    except Exception as err:  # noqa: BLE001 - job must fail closed
        job.error = "Save-bundle preparation failed safely."
        _LOGGER.warning(
            "Save-bundle %s job %s failed error_type=%s",
            job.action,
            job.job_id,
            err.__class__.__name__,
        )


def start_save_bundle_job(
    hass: Any,
    *,
    user_id: str,
    account_entry_id: str,
    service_id: str,
    bundle_key: str,
    action: str,
    operation: Callable[[Callable[[str, str, Any], None]], Awaitable[Any]],
) -> SaveBundleJob:
    """Start one bounded job, replacing an older same-scope job."""

    transition = hass.data.get(PROFILE_REGISTRY_TRANSITION_DATA_KEY, {})
    if isinstance(transition, dict) and transition.get("save_job_admission_blocked") is True:
        raise ValueError("Save-bundle work is temporarily unavailable during a profile registry transition")
    if action not in {"download", "inspect"}:
        raise ValueError("invalid save-bundle job action")
    _prune(hass)
    jobs = _jobs(hass)
    identity = (str(user_id), str(account_entry_id), str(service_id), str(bundle_key), action)
    for old_id, old in tuple(jobs.items()):
        if (old.user_id, old.account_entry_id, old.service_id, old.bundle_key, old.action) == identity:
            _discard(old)
            if old.task is not None and not old.task.done():
                raise ValueError("A matching save-bundle job is still running or draining")
            jobs.pop(old_id, None)
    if len(jobs) >= MAX_SAVE_BUNDLE_JOBS:
        raise ValueError("Too many save-bundle jobs are still running or draining")
    now = time.monotonic()
    job = SaveBundleJob(
        job_id=secrets.token_urlsafe(24),
        user_id=identity[0],
        account_entry_id=identity[1],
        service_id=identity[2],
        bundle_key=identity[3],
        action=action,
        created_at=now,
        expires_at=now + SAVE_BUNDLE_JOB_SECONDS,
    )
    jobs[job.job_id] = job
    job.task = hass.async_create_task(_run(job, operation), f"{DOMAIN} save bundle {action}")
    _prune(hass)
    return job


def require_save_bundle_job(hass: Any, *, job_id: str, **identity: str) -> SaveBundleJob:
    """Return one exact private job without consuming its result."""

    _prune(hass)
    job = _jobs(hass).get(str(job_id))
    if job is None or not _matches(job, **identity):
        raise ValueError("The save-bundle job is missing, expired, or belongs to another context.")
    return job


def take_save_bundle_job(hass: Any, *, job_id: str, **identity: str) -> Any:
    """Consume one ready result so it can have exactly one owner."""

    job = require_save_bundle_job(hass, job_id=job_id, **identity)
    if job.status != "ready":
        raise ValueError("The save-bundle job is not ready.")
    _jobs(hass).pop(job.job_id, None)
    result = job.result
    job.result = None
    return result


def discard_save_bundle_job(hass: Any, *, job_id: str, **identity: str) -> None:
    """Consume one exact failed job and release any result defensively."""

    job = require_save_bundle_job(hass, job_id=job_id, **identity)
    if job.status != "error":
        raise ValueError("The save-bundle job has not failed.")
    _jobs(hass).pop(job.job_id, None)
    _discard(job)


def cancel_save_bundle_jobs(
    hass: Any,
    *,
    account_entry_id: str | None = None,
    service_id: str | None = None,
) -> None:
    """Cancel and close jobs invalidated by an integration/profile epoch."""

    jobs = _jobs(hass)
    for job_id, job in tuple(jobs.items()):
        if (account_entry_id is None or job.account_entry_id == str(account_entry_id)) and (
            service_id is None or job.service_id == str(service_id)
        ):
            _discard(job)
            if job.task is None or job.task.done():
                jobs.pop(job_id, None)


async def async_cancel_save_bundle_jobs(
    hass: Any,
    *,
    account_entry_id: str | None = None,
    service_id: str | None = None,
) -> None:
    """Cancel matching jobs and do not return until their real workers drain."""

    cancel_save_bundle_jobs(hass, account_entry_id=account_entry_id, service_id=service_id)
    jobs = _jobs(hass)
    matching = tuple(
        job
        for job in jobs.values()
        if (account_entry_id is None or job.account_entry_id == str(account_entry_id))
        and (service_id is None or job.service_id == str(service_id))
    )
    current = asyncio.current_task()
    tasks = tuple(
        job.task for job in matching if job.task is not None and job.task is not current and not job.task.done()
    )
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    for job in matching:
        if job.task is None or job.task.done():
            _discard(job)
            jobs.pop(job.job_id, None)
    if not jobs:
        hass.data.pop(_DATA_KEY, None)


__all__ = (
    "SaveBundleJob",
    "async_cancel_save_bundle_jobs",
    "cancel_save_bundle_jobs",
    "discard_save_bundle_job",
    "require_save_bundle_job",
    "start_save_bundle_job",
    "take_save_bundle_job",
)
