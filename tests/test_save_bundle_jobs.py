"""Lifecycle and binding tests for asynchronous save-bundle preparation."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from custom_components.nitrado_gameserver.const import PROFILE_REGISTRY_TRANSITION_DATA_KEY
from custom_components.nitrado_gameserver.filesystem import FileTransportError
from custom_components.nitrado_gameserver.save_bundle_jobs import (
    async_cancel_save_bundle_jobs,
    cancel_save_bundle_jobs,
    discard_save_bundle_job,
    require_save_bundle_job,
    start_save_bundle_job,
    take_save_bundle_job,
)


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def async_create_task(self, coroutine: Any, name: str) -> asyncio.Task[None]:
        return asyncio.create_task(coroutine, name=name)


class Closeable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


IDENTITY = {
    "user_id": "user-1",
    "account_entry_id": "entry-1",
    "service_id": "service-1",
    "bundle_key": "world",
    "action": "download",
}


class SaveBundleJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_transition_blocks_new_job_admission(self) -> None:
        hass = FakeHass()
        hass.data[PROFILE_REGISTRY_TRANSITION_DATA_KEY] = {"save_job_admission_blocked": True}

        with self.assertRaisesRegex(ValueError, "profile registry transition"):
            start_save_bundle_job(
                hass,
                **IDENTITY,
                operation=lambda _progress: asyncio.sleep(0, result=Closeable()),
            )

    async def test_ready_result_is_exact_bound_and_one_shot(self) -> None:
        hass = FakeHass()
        result = Closeable()
        job = start_save_bundle_job(hass, **IDENTITY, operation=lambda _progress: asyncio.sleep(0, result=result))
        await job.task
        self.assertEqual(require_save_bundle_job(hass, job_id=job.job_id, **IDENTITY).status, "ready")
        with self.assertRaises(ValueError):
            require_save_bundle_job(hass, job_id=job.job_id, **{**IDENTITY, "user_id": "other"})
        self.assertIs(take_save_bundle_job(hass, job_id=job.job_id, **IDENTITY), result)
        self.assertFalse(result.closed)
        with self.assertRaises(ValueError):
            take_save_bundle_job(hass, job_id=job.job_id, **IDENTITY)

    async def test_new_same_scope_job_waits_for_old_operation_to_drain(self) -> None:
        hass = FakeHass()
        cancelled = asyncio.Event()

        async def blocked(_progress) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        old = start_save_bundle_job(hass, **IDENTITY, operation=blocked)
        await asyncio.sleep(0)
        with self.assertRaisesRegex(ValueError, "running or draining"):
            start_save_bundle_job(
                hass,
                **IDENTITY,
                operation=lambda _progress: asyncio.sleep(0, result=Closeable()),
            )
        await asyncio.gather(old.task, return_exceptions=True)
        new = start_save_bundle_job(
            hass,
            **IDENTITY,
            operation=lambda _progress: asyncio.sleep(0, result=Closeable()),
        )
        await new.task
        self.assertTrue(cancelled.is_set())
        with self.assertRaises(ValueError):
            require_save_bundle_job(hass, job_id=old.job_id, **IDENTITY)

    async def test_cancel_closes_a_prepared_private_result(self) -> None:
        hass = FakeHass()
        result = Closeable()
        job = start_save_bundle_job(hass, **IDENTITY, operation=lambda _progress: asyncio.sleep(0, result=result))
        await job.task
        cancel_save_bundle_jobs(hass, account_entry_id="entry-1", service_id="service-1")
        self.assertTrue(result.closed)
        with self.assertRaises(ValueError):
            require_save_bundle_job(hass, job_id=job.job_id, **IDENTITY)

    async def test_account_scoped_cancel_does_not_abort_another_account(self) -> None:
        hass = FakeHass()
        release_a = asyncio.Event()
        cancelled_b = asyncio.Event()

        async def work_a(_progress) -> Closeable:
            await release_a.wait()
            return Closeable()

        async def work_b(_progress) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled_b.set()

        identity_a = {**IDENTITY, "account_entry_id": "entry-a", "service_id": "service-a"}
        identity_b = {**IDENTITY, "account_entry_id": "entry-b", "service_id": "service-b"}
        job_a = start_save_bundle_job(hass, **identity_a, operation=work_a)
        job_b = start_save_bundle_job(hass, **identity_b, operation=work_b)
        await asyncio.sleep(0)

        cancel_save_bundle_jobs(hass, account_entry_id="entry-b")
        await job_b.task
        self.assertTrue(cancelled_b.is_set())
        self.assertFalse(job_a.task.done())

        release_a.set()
        await job_a.task
        self.assertEqual(job_a.status, "ready")

    async def test_async_cancel_waits_for_real_operation_drain(self) -> None:
        hass = FakeHass()
        cancellation_seen = asyncio.Event()
        release_worker = asyncio.Event()

        async def stubborn_worker(_progress) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_worker.wait()
                raise

        job = start_save_bundle_job(hass, **IDENTITY, operation=stubborn_worker)
        await asyncio.sleep(0)
        draining = asyncio.create_task(
            async_cancel_save_bundle_jobs(hass, account_entry_id="entry-1", service_id="service-1")
        )
        await cancellation_seen.wait()
        self.assertFalse(draining.done())
        self.assertFalse(job.task.done())

        release_worker.set()
        await draining
        self.assertTrue(job.task.done())
        with self.assertRaises(ValueError):
            require_save_bundle_job(hass, job_id=job.job_id, **IDENTITY)

    async def test_unexpected_failure_is_not_exposed(self) -> None:
        hass = FakeHass()

        async def fail(_progress) -> None:
            raise RuntimeError("private provider detail")

        job = start_save_bundle_job(hass, **IDENTITY, operation=fail)
        await job.task
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, "Save-bundle preparation failed safely.")
        self.assertNotIn("private", job.error)

    async def test_failed_job_can_be_consumed_and_releases_state(self) -> None:
        hass = FakeHass()

        async def fail(_progress) -> None:
            raise RuntimeError("private detail")

        job = start_save_bundle_job(hass, **IDENTITY, operation=fail)
        await job.task
        discard_save_bundle_job(hass, job_id=job.job_id, **IDENTITY)
        with self.assertRaises(ValueError):
            require_save_bundle_job(hass, job_id=job.job_id, **IDENTITY)

    async def test_transport_failure_is_safe_actionable_and_reports_no_write(self) -> None:
        hass = FakeHass()

        async def fail(_progress) -> None:
            raise FileTransportError("private remote path and socket detail")

        job = start_save_bundle_job(hass, **IDENTITY, operation=fail)
        await job.task
        self.assertEqual(job.status, "error")
        self.assertIn("Nitrado FTPS could not finish reading", job.error)
        self.assertIn("No server data was changed", job.error)
        self.assertIn("Retry", job.error)
        self.assertNotIn("private remote path", job.error)

    async def test_running_job_exposes_bounded_structured_progress(self) -> None:
        hass = FakeHass()
        release = asyncio.Event()

        async def work(progress) -> Closeable:
            progress(
                "downloading",
                "Downloading live save files — 3 complete…",
                {
                    "files_done": 3,
                    "bytes_done": 2048,
                    "current_file": "Players/abc.sav",
                    "ignored": "private provider detail",
                },
            )
            await release.wait()
            return Closeable()

        job = start_save_bundle_job(hass, **IDENTITY, operation=work)
        await asyncio.sleep(0)
        payload = job.progress_payload()
        self.assertEqual(payload["stage"], "downloading")
        self.assertEqual(payload["files_done"], 3)
        self.assertEqual(payload["bytes_done"], 2048)
        self.assertEqual(payload["current_file"], "Players/abc.sav")
        self.assertNotIn("ignored", payload)
        release.set()
        await job.task


if __name__ == "__main__":
    unittest.main()
