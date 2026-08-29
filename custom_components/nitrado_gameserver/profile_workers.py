"""Bounded isolated workers for synchronous third-party profile code."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable, Hashable
from typing import Any

MAX_PROFILE_WORKERS = 8
MAX_QUARANTINED_IDENTITIES = 256

_LOCK = threading.Lock()
_ACTIVE_WORKERS = 0
_QUARANTINED: set[Hashable] = set()
_QUARANTINE_ORDER: deque[Hashable] = deque()


class ProfileWorkerUnavailable(RuntimeError):
    """Raised when isolated profile execution is quarantined or saturated."""


def _quarantine(identity: Hashable) -> None:
    with _LOCK:
        if identity in _QUARANTINED:
            return
        _QUARANTINED.add(identity)
        _QUARANTINE_ORDER.append(identity)
        while len(_QUARANTINE_ORDER) > MAX_QUARANTINED_IDENTITIES:
            _QUARANTINED.discard(_QUARANTINE_ORDER.popleft())


def profile_worker_status() -> dict[str, int]:
    """Return bounded, non-secret worker diagnostics."""

    with _LOCK:
        return {"active": _ACTIVE_WORKERS, "quarantined": len(_QUARANTINED)}


async def async_run_profile_sync(
    identity: Hashable,
    callback: Callable[..., Any],
    *args: Any,
    timeout: float,
) -> Any:
    """Run synchronous extension code without consuming HA's shared executor."""

    global _ACTIVE_WORKERS
    with _LOCK:
        if identity in _QUARANTINED:
            raise ProfileWorkerUnavailable("This profile callback generation is quarantined")
        if _ACTIVE_WORKERS >= MAX_PROFILE_WORKERS:
            raise ProfileWorkerUnavailable("The isolated profile worker budget is exhausted")
        _ACTIVE_WORKERS += 1

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def complete_result(result: Any = None, error: BaseException | None = None) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    def worker() -> None:
        global _ACTIVE_WORKERS
        try:
            result = callback(*args)
        except Exception as err:  # noqa: BLE001 - isolate arbitrary extension failures.
            result = None
            error: BaseException | None = err
        except BaseException:  # noqa: BLE001 - never inject SystemExit/KeyboardInterrupt into HA's loop.
            result = None
            error = ProfileWorkerUnavailable("The profile callback terminated abnormally")
        else:
            error = None
        finally:
            with _LOCK:
                _ACTIVE_WORKERS -= 1
        if not loop.is_closed():
            loop.call_soon_threadsafe(complete_result, result, error)

    try:
        threading.Thread(target=worker, name="nitrado-profile-worker", daemon=True).start()
    except Exception:  # noqa: BLE001 - normalize thread resource failures.
        with _LOCK:
            _ACTIVE_WORKERS -= 1
        raise ProfileWorkerUnavailable("The isolated profile worker could not be started") from None

    def consume_late_result(completed: asyncio.Future[Any]) -> None:
        if completed.cancelled():
            return
        try:
            completed.exception()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - consume arbitrary late worker failures.
            return

    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(future)
    except TimeoutError:
        _quarantine(identity)
        future.add_done_callback(consume_late_result)
        raise
    except asyncio.CancelledError:
        future.add_done_callback(consume_late_result)
        raise
