from __future__ import annotations

import time
import unittest
from functools import partial
from unittest.mock import patch

from custom_components.nitrado_gameserver.plugins.base import async_invoke_profile
from custom_components.nitrado_gameserver.profile_workers import (
    ProfileWorkerUnavailable,
    async_run_profile_sync,
    profile_worker_status,
)


class ProfileWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_base_exception_cannot_escape_into_home_assistant_loop(self) -> None:
        async def terminate() -> None:
            raise SystemExit("profile tried to terminate the process")

        with self.assertRaisesRegex(ProfileWorkerUnavailable, "terminated abnormally"):
            await async_invoke_profile(terminate, timeout=1)

    async def test_sync_callback_returning_fatal_awaitable_is_normalized(self) -> None:
        def terminate_later():
            async def fatal() -> None:
                raise SystemExit("profile tried to terminate the process")

            return fatal()

        with self.assertRaisesRegex(ProfileWorkerUnavailable, "terminated abnormally"):
            await async_invoke_profile(terminate_later, timeout=1)

    async def test_base_exception_cannot_escape_into_home_assistant_loop(self) -> None:
        def terminate() -> None:
            raise SystemExit("profile tried to terminate the process")

        with self.assertRaisesRegex(ProfileWorkerUnavailable, "terminated abnormally"):
            await async_run_profile_sync(("system-exit", id(self)), terminate, timeout=1)

    async def test_thread_start_failure_releases_worker_capacity(self) -> None:
        before = profile_worker_status()["active"]
        with (
            patch("threading.Thread.start", side_effect=RuntimeError("thread unavailable")),
            self.assertRaisesRegex(ProfileWorkerUnavailable, "could not be started"),
        ):
            await async_run_profile_sync(("thread-start", id(self)), lambda: None, timeout=1)
        self.assertEqual(profile_worker_status()["active"], before)

    async def test_new_registry_generation_recovers_same_callback_from_quarantine(self) -> None:
        calls = 0

        def callback() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                time.sleep(0.05)
            return "ready"

        with (
            patch(
                "custom_components.nitrado_gameserver.plugins.registry.profile_registration_generation",
                return_value=1,
            ),
            patch(
                "custom_components.nitrado_gameserver.plugins.registry.profile_registry_generation",
                return_value=1,
            ),
            self.assertRaises(TimeoutError),
        ):
            await async_invoke_profile(callback, timeout=0.001)

        with (
            patch(
                "custom_components.nitrado_gameserver.plugins.registry.profile_registration_generation",
                return_value=2,
            ),
            patch(
                "custom_components.nitrado_gameserver.plugins.registry.profile_registry_generation",
                return_value=2,
            ),
        ):
            self.assertEqual(await async_invoke_profile(callback, timeout=1), "ready")

    async def test_unrelated_partial_callbacks_do_not_share_quarantine(self) -> None:
        def slow(value: str) -> str:
            time.sleep(0.05)
            return value

        def fast(value: str) -> str:
            return value

        with self.assertRaises(TimeoutError):
            await async_invoke_profile(partial(slow, "slow"), timeout=0.001)
        self.assertEqual(await async_invoke_profile(partial(fast, "fast"), timeout=1), "fast")

    async def test_slotted_callable_instances_do_not_share_quarantine(self) -> None:
        class Callback:
            __slots__ = ("delay",)

            def __init__(self, delay: float) -> None:
                self.delay = delay

            def __call__(self) -> str:
                time.sleep(self.delay)
                return "ready"

        with self.assertRaises(TimeoutError):
            await async_invoke_profile(Callback(0.05), timeout=0.001)
        self.assertEqual(await async_invoke_profile(Callback(0), timeout=1), "ready")

    async def test_same_profile_generation_cannot_bypass_quarantine_with_new_instance(self) -> None:
        class Profile:
            profile_id = "reload_guard"

            def callback(self) -> str:
                time.sleep(0.05)
                return "ready"

        with (
            patch(
                "custom_components.nitrado_gameserver.plugins.registry.profile_registration_generation",
                return_value=7,
            ),
            patch(
                "custom_components.nitrado_gameserver.plugins.registry.profile_registry_generation",
                return_value=11,
            ),
        ):
            with self.assertRaises(TimeoutError):
                await async_invoke_profile(Profile().callback, timeout=0.001)
            with self.assertRaises(ProfileWorkerUnavailable):
                await async_invoke_profile(Profile().callback, timeout=1)
