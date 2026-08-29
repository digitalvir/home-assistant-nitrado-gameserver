"""Tests for pure service runtime and control evaluation."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoService,
    ParsedServer,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.plugins.base import CapabilityState, CapabilityVerdict
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile
from custom_components.nitrado_gameserver.runtime import (
    ServiceRuntime,
    evaluate_start,
    evaluate_stop,
)


def service(game: str = "palworldxb") -> NitradoService:
    """Build a service fixture."""

    return NitradoService(
        service_id="123456",
        name="Palworld Xbox" if "palworld" in game else "ARK",
        game=game,
        game_human="Palworld" if "palworld" in game else "ARK",
        folder_short=game,
        type_human="Gameserver",
        raw_redacted={},
    )


def server(status: str, game: str = "palworldxb") -> ParsedServer:
    """Build a server fixture."""

    return ParsedServer(
        service_id="123456",
        raw_status=status,
        server_name="Server",
        address="203.0.113.10:12345",
        game_short=game,
        game_human="Palworld" if "palworld" in game else "ARK",
        player_count=None,
        player_max=10,
        player_names=(),
        query_valid=False,
        player_source=None,
        raw_redacted={},
    )


def runtime_with(status: str, *, observed_at: int = 1000) -> ServiceRuntime:
    """Build a runtime fixture with one status observation."""

    runtime = ServiceRuntime(ManagedServiceState("123456"))
    runtime.update_service(service())
    runtime.update_server(server(status), observed_at=observed_at)
    return runtime


class RuntimeTests(unittest.TestCase):
    """Service runtime tests."""

    def test_lifecycle_tracks_stopped_since_after_transition(self) -> None:
        runtime = runtime_with("gs_installation", observed_at=100)
        runtime.update_server(server("stopped"), observed_at=200)

        self.assertEqual(runtime.last_transition_at, 200)
        self.assertEqual(runtime.stopped_since, 200)
        self.assertEqual(runtime.stopped_for(260), 60)
        self.assertEqual(runtime.last_transition_age(260), 60)

    def test_lifecycle_tracks_direct_running_to_stopped_change(self) -> None:
        runtime = runtime_with("started", observed_at=100)
        runtime.update_server(server("stopped"), observed_at=200)

        self.assertEqual(runtime.last_transition_at, 200)
        self.assertEqual(runtime.stopped_since, 200)
        self.assertIsNone(runtime.started_since)

    def test_initial_observation_does_not_create_transition_time(self) -> None:
        runtime = runtime_with("stopped", observed_at=100)

        self.assertIsNone(runtime.last_transition_at)
        self.assertEqual(runtime.stopped_since, 100)

    def test_server_metadata_can_reselect_more_specific_profile(self) -> None:
        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.update_service(
            NitradoService(
                service_id="123456",
                name="Game server",
                game="",
                game_human="",
                folder_short="",
                type_human="Gameserver",
                raw_redacted={},
            )
        )

        self.assertEqual(runtime.profile.profile_id, "generic")

        runtime.update_server(server("started", game="palworldxb"), observed_at=100)

        self.assertEqual(runtime.profile.profile_id, "palworld")
        self.assertEqual(runtime.state.profile_id, "palworld")

    def test_safe_start_blocks_until_stopped_settle_window_passes(self) -> None:
        async def run() -> None:
            runtime = runtime_with("gs_installation", observed_at=100)
            runtime.update_server(server("stopped"), observed_at=200)

            verdict = await evaluate_start(runtime, now=260, settle_seconds=600)

            self.assertEqual(verdict.state, CapabilityState.BLOCKED)
            self.assertTrue(verdict.overridable)
            self.assertIn("settle window", verdict.reason)

        asyncio.run(run())

    def test_safe_start_allows_after_settle_window(self) -> None:
        async def run() -> None:
            runtime = runtime_with("gs_installation", observed_at=100)
            runtime.update_server(server("stopped"), observed_at=200)

            verdict = await evaluate_start(runtime, now=900, settle_seconds=600)

            self.assertEqual(verdict.state, CapabilityState.SUPPORTED)

        asyncio.run(run())

    def test_force_start_bypasses_overridable_settle_block(self) -> None:
        async def run() -> None:
            runtime = runtime_with("gs_installation", observed_at=100)
            runtime.update_server(server("stopped"), observed_at=200)

            verdict = await evaluate_start(runtime, now=260, settle_seconds=600, force=True)

            self.assertEqual(verdict.state, CapabilityState.SUPPORTED)

        asyncio.run(run())

    def test_force_start_does_not_bypass_missing_status(self) -> None:
        async def run() -> None:
            runtime = ServiceRuntime(ManagedServiceState("123456"))
            runtime.update_service(service())

            verdict = await evaluate_start(runtime, now=260, settle_seconds=600, force=True)

            self.assertEqual(verdict.state, CapabilityState.BLOCKED)
            self.assertFalse(verdict.overridable)

        asyncio.run(run())

    def test_safe_stop_requires_running_status(self) -> None:
        async def run() -> None:
            runtime = runtime_with("stopped", observed_at=100)

            blocked = await evaluate_stop(runtime)
            forced = await evaluate_stop(runtime, force=True)

            self.assertEqual(blocked.state, CapabilityState.BLOCKED)
            self.assertTrue(blocked.overridable)
            self.assertEqual(forced.state, CapabilityState.SUPPORTED)

        asyncio.run(run())

    def test_force_does_not_bypass_malformed_overridable_field(self) -> None:
        class MalformedProfile(GenericProfile):
            async def can_start(self, context):
                return CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="must remain blocked",
                    overridable="false",  # type: ignore[arg-type]
                )

        async def run() -> None:
            runtime = runtime_with("stopped", observed_at=100)
            runtime.profile = MalformedProfile()

            verdict = await evaluate_start(runtime, now=1000, settle_seconds=0, force=True)

            self.assertEqual(verdict.state, CapabilityState.BLOCKED)
            self.assertIn("invalid Start verdict", verdict.reason)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
