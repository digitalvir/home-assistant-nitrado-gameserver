"""Tests for account coordinator behavior."""

from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoApiError,
    NitradoAuthError,
    NitradoService,
    ParsedServer,
)
from custom_components.nitrado_gameserver.const import DISCOVERY_ASK, DISCOVERY_AUTO_ADD
from custom_components.nitrado_gameserver.coordinator import (
    AccountCoordinatorOptions,
    NitradoAccountCoordinator,
    NitradoControlError,
    NitradoServiceUnknownError,
    _server_updates_from_profile_status,
)
from custom_components.nitrado_gameserver.extensions import ProfileExtensionError
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.plugins.base import (
    SUPPORTED,
    ActionDeclaration,
    LifecycleEvent,
    LifecycleHookDeclaration,
    MatchResult,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileStatus,
    profile_extension_manifest,
)
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile
from custom_components.nitrado_gameserver.plugins.registry import _ProfileCandidate, register_profile
from custom_components.nitrado_gameserver.runtime import ServiceRuntime


def service(service_id: str = "123456", game: str = "palworldxb") -> NitradoService:
    """Build a service fixture."""

    return NitradoService(
        service_id=service_id,
        name="Palworld Xbox",
        game=game,
        game_human="Palworld",
        folder_short=game,
        type_human="Gameserver",
        raw_redacted={},
    )


def server(status: str, *, service_id: str = "123456", game: str = "palworldxb") -> ParsedServer:
    """Build a parsed server fixture."""

    return ParsedServer(
        service_id=service_id,
        raw_status=status,
        server_name="Server",
        address="203.0.113.10:12345",
        game_short=game,
        game_human="Palworld",
        player_count=1,
        player_max=10,
        player_names=("Alex",),
        query_valid=True,
        player_source="nitrado_query",
        raw_redacted={},
    )


def profile_candidate(profile):
    """Bind one test profile to the manifest snapshot runtime selection receives."""

    return _ProfileCandidate(profile=profile, manifest=profile_extension_manifest(profile))


class FakeNitradoClient:
    """Small async client test double."""

    def __init__(self) -> None:
        self.services = [service()]
        self.servers = {"123456": server("stopped")}
        self.root_files = {"entries": []}
        self.file_lists: dict[str | None, dict] = {}
        self.downloads: dict[str, str] = {}
        self.external_json: dict[str, dict] = {}
        self.players: dict[str, tuple[str, ...]] = {}
        self.started: list[tuple[str, str]] = []
        self.stopped: list[tuple[str, str]] = []

    async def service_list(self) -> list[NitradoService]:
        """Return configured services."""

        return self.services

    async def fetch_server(self, service_id: str) -> ParsedServer:
        """Return configured server status."""

        return self.servers[service_id]

    async def start_server(self, service_id: str, game_short: str) -> None:
        """Record a start command."""

        self.started.append((service_id, game_short))

    async def stop_server(self, service_id: str, reason: str) -> None:
        """Record a stop command."""

        self.stopped.append((service_id, reason))

    async def fetch_players(self, service_id: str) -> tuple[str, ...]:
        """Return configured explicit players."""

        return self.players.get(service_id, ())

    async def list_files(self, service_id: str, directory: str | None = None) -> dict:
        """Return configured file-browser data."""

        return self.file_lists.get(directory, self.root_files)

    async def download_file(self, service_id: str, path: str) -> str:
        """Return configured file content."""

        return self.downloads[path]

    async def fetch_external_json(
        self,
        url: str,
        *,
        allowed_hosts=(),
        headers=None,
        timeout_seconds: int = 10,
    ) -> dict:
        """Return configured external JSON payload."""

        return self.external_json[url]


class CoordinatorTests(unittest.TestCase):
    """Account coordinator tests."""

    def test_save_bundle_fresh_stop_probe_fails_closed(self) -> None:
        client = FakeNitradoClient()
        coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
        asyncio.run(coordinator._async_require_fresh_save_bundle_stop("123456"))

        client.servers["123456"] = server("started")
        with self.assertRaisesRegex(ProfileExtensionError, "freshly confirmed stopped"):
            asyncio.run(coordinator._async_require_fresh_save_bundle_stop("123456"))

    def test_auto_shutdown_status_distinguishes_inactive_countdown_and_unavailable_truth(self) -> None:
        class IdleProfile(GenericProfile):
            profile_id = "idle_profile"
            idle_shutdown_supported = True

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.update_service(service())
        runtime.update_server(server("stopped"), observed_at=1000)
        runtime.profile = IdleProfile()
        runtime.last_idle_shutdown_verdict = SUPPORTED
        coordinator = NitradoAccountCoordinator(
            FakeNitradoClient(),  # type: ignore[arg-type]
            options=AccountCoordinatorOptions(
                idle_shutdown_service_ids=frozenset({"123456"}),
                idle_minutes={"123456": 15},
            ),
            now_fn=lambda: 1000,
            services={"123456": runtime},
        )

        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Inactive — server stopped")

        runtime.server = replace(runtime.server, raw_status="stopping")
        self.assertEqual(
            coordinator.auto_shutdown_status("123456"),
            "Waiting for stable server status",
        )

        runtime.server = replace(runtime.server, raw_status="started", player_count=2, query_valid=True)
        coordinator.options.idle_shutdown_service_ids = frozenset()
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Disabled")

        coordinator.options.idle_shutdown_service_ids = frozenset({"123456"})
        coordinator.options.maintenance_service_ids = frozenset({"123456"})
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Paused")

        coordinator.options.maintenance_service_ids = frozenset()
        runtime.startup_cooldown_started_at = 900
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Startup cooldown")

        runtime.startup_cooldown_started_at = None
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Waiting for zero players")

        runtime.server = replace(runtime.server, player_count=0)
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Starting countdown")

        runtime.idle_started_at = 700
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "10.0 min remaining")

        runtime.shutdown_pending = True
        self.assertEqual(coordinator.auto_shutdown_status("123456"), "Final checks")

        runtime.shutdown_pending = False
        runtime.server = replace(runtime.server, player_count=None, query_valid=False)
        self.assertIsNone(coordinator.auto_shutdown_status("123456"))

        runtime.status_fresh = False
        self.assertIsNone(coordinator.auto_shutdown_status("123456"))

    def test_status_and_scheduled_lifecycle_events_are_dispatched(self) -> None:
        async def run() -> None:
            observed: list[tuple[LifecycleEvent, dict]] = []

            class HookProfile(GenericProfile):
                profile_id = "hook_profile"

                def lifecycle_hooks(self):
                    async def record_status(context):
                        observed.append((LifecycleEvent.STATUS_REFRESH, context.payload))

                    async def record_scheduled(context):
                        observed.append((LifecycleEvent.SCHEDULED_VALIDATION, context.payload))

                    return (
                        LifecycleHookDeclaration("status", LifecycleEvent.STATUS_REFRESH, record_status),
                        LifecycleHookDeclaration(
                            "scheduled",
                            LifecycleEvent.SCHEDULED_VALIDATION,
                            record_scheduled,
                        ),
                    )

            client = FakeNitradoClient()
            runtime = ServiceRuntime(ManagedServiceState("123456"))
            runtime.update_service(service())
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                services={"123456": runtime},
                now_fn=lambda: 500,
            )
            profile = HookProfile()
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                new=AsyncMock(return_value=profile_candidate(profile)),
            ):
                await coordinator.async_refresh_service("123456", handle_idle_shutdown=False)
                await coordinator.async_run_scheduled_validations()

            self.assertEqual(
                [event for event, _payload in observed],
                [
                    LifecycleEvent.STATUS_REFRESH,
                    LifecycleEvent.SCHEDULED_VALIDATION,
                ],
            )
            self.assertEqual(observed[0][1]["status"], "stopped")
            self.assertEqual(observed[1][1]["scheduled_at"], 500)

        asyncio.run(run())

    def test_direct_refresh_auth_failure_marks_account_stale(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            await coordinator.async_refresh_discovery()
            runtime = await coordinator.async_refresh_service("123456", handle_idle_shutdown=False)
            runtime.shutdown_pending = True

            class MustNotRunProfile(GenericProfile):
                profile_id = "must_not_run"

                async def can_start(self, context):
                    raise AssertionError("stale handling must not invoke profile start checks")

                async def can_stop(self, context):
                    raise AssertionError("stale handling must not invoke profile stop checks")

                def idle_shutdown_capability(self, context):
                    raise AssertionError("stale handling must not invoke profile idle checks")

            runtime.profile = MustNotRunProfile()

            async def fail_auth(service_id: str) -> ParsedServer:
                raise NitradoAuthError("expired token")

            client.fetch_server = fail_auth  # type: ignore[method-assign]
            with self.assertRaises(NitradoAuthError):
                await coordinator.async_refresh_service("123456", handle_idle_shutdown=False)

            self.assertFalse(runtime.status_fresh)
            self.assertTrue(runtime.using_cached_data)
            self.assertFalse(runtime.shutdown_pending)
            self.assertIn("reauthentication", coordinator.last_refresh_error)
            self.assertIn("reauthentication", runtime.last_start_verdict.reason)
            self.assertIs(runtime.last_start_verdict, runtime.last_stop_verdict)
            self.assertIs(runtime.last_start_verdict, runtime.last_idle_shutdown_verdict)

        asyncio.run(run())

    def test_refresh_failure_resets_idle_timer_and_recovery_starts_over(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=0)
            clock = [1000]
            coordinator.now_fn = lambda: clock[0]
            coordinator.options = replace(
                coordinator.options,
                idle_minutes={"123456": 15},
            )
            await coordinator.async_refresh_discovery()
            runtime = await coordinator.async_refresh_service("123456")
            self.assertEqual(runtime.idle_started_at, 1000)

            original_fetch = client.fetch_server

            async def fail_api(service_id: str) -> ParsedServer:
                raise NitradoApiError("temporary outage")

            clock[0] = 1300
            client.fetch_server = fail_api  # type: ignore[method-assign]
            with self.assertRaises(NitradoApiError):
                await coordinator.async_refresh_service("123456")

            self.assertFalse(runtime.status_fresh)
            self.assertIsNone(runtime.idle_started_at)
            self.assertFalse(runtime.shutdown_pending)

            clock[0] = 5000
            client.fetch_server = original_fetch  # type: ignore[method-assign]
            await coordinator.async_refresh_service("123456")
            self.assertEqual(runtime.idle_started_at, 5000)
            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_discovery_auth_failure_marks_existing_runtime_stale(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            await coordinator.async_refresh_discovery()
            runtime = await coordinator.async_refresh_service("123456", handle_idle_shutdown=False)
            runtime.idle_started_at = 100
            runtime.shutdown_pending = True

            async def fail_auth() -> list[NitradoService]:
                raise NitradoAuthError("expired token")

            client.service_list = fail_auth  # type: ignore[method-assign]
            with self.assertRaises(NitradoAuthError):
                await coordinator.async_refresh_discovery()

            self.assertFalse(runtime.status_fresh)
            self.assertTrue(runtime.using_cached_data)
            self.assertIsNone(runtime.idle_started_at)
            self.assertFalse(runtime.shutdown_pending)

        asyncio.run(run())

    def test_profile_status_display_name_only_does_not_override_server_player_data(self) -> None:
        updates = _server_updates_from_profile_status(ProfileStatus(display_name="Better Name"))

        self.assertEqual(updates, {})

    def test_idle_shutdown_capability_uses_profile_scoped_extra(self) -> None:
        class IdleProfile:
            profile_id = "idle"
            name = "Idle"
            supported_games = ("idle",)
            idle_shutdown_supported = True

            def matches(self, service, server=None):
                return MatchResult(True, confidence=1)

            def idle_shutdown_capability(self, context):
                context.extra["capability_checked"] = True
                return SUPPORTED

        async def run() -> None:
            service_runtime = ServiceRuntime(ManagedServiceState("123456"))
            service_runtime.update_service(service(game="idle"))
            service_runtime.update_server(server("started", game="idle"), observed_at=100)
            service_runtime.profile = IdleProfile()
            service_runtime.extra["core_only"] = "hidden"
            coordinator = NitradoAccountCoordinator(  # type: ignore[arg-type]
                FakeNitradoClient(), services={"123456": service_runtime}
            )

            await coordinator._refresh_control_verdicts(service_runtime)
            self.assertTrue(coordinator.idle_shutdown_supported("123456"))
            self.assertTrue(service_runtime.profile_extra()["capability_checked"])
            self.assertNotIn("capability_checked", service_runtime.extra)
            self.assertEqual(service_runtime.extra["core_only"], "hidden")

        asyncio.run(run())

    def test_profile_option_defaults_are_merged_before_persisted_false_overrides(self) -> None:
        class OptionProfile(GenericProfile):
            profile_id = "option_profile"

            def profile_options(self):
                return (
                    ProfileOptionDeclaration(
                        key="safe_mode",
                        name="Safe Mode",
                        option_type=ProfileOptionType.BOOLEAN,
                        default=True,
                    ),
                )

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime.profile = OptionProfile()
        coordinator = NitradoAccountCoordinator(FakeNitradoClient(), services={"123456": runtime})  # type: ignore[arg-type]

        coordinator._sync_profile_options(runtime)
        self.assertIs(runtime.extra["_persisted_profile_options"]["safe_mode"], True)

        coordinator.options.profile_options = {
            "option_profile": {"safe_mode": {"123456": False}},
        }
        coordinator._sync_profile_options(runtime)
        self.assertIs(runtime.extra["_persisted_profile_options"]["safe_mode"], False)

    def test_ask_discovery_creates_pending_not_runtime(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_ASK),
            )

            result = await coordinator.async_refresh_discovery()

            self.assertEqual(result.newly_discovered, {"123456"})
            self.assertEqual(coordinator.snapshot().pending_service_ids, ("123456",))
            self.assertEqual(coordinator.snapshot().runtime_service_ids, ())

        asyncio.run(run())

    def test_import_pending_service_creates_runtime(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.import_service("123456")

            self.assertEqual(runtime.service.service_id, "123456")
            self.assertEqual(coordinator.snapshot().runtime_service_ids, ("123456",))

        asyncio.run(run())

    def test_auto_add_discovery_creates_runtime(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
            )

            await coordinator.async_refresh_discovery()

            self.assertEqual(coordinator.snapshot().pending_service_ids, ())
            self.assertEqual(coordinator.snapshot().runtime_service_ids, ("123456",))

        asyncio.run(run())

    def test_imported_service_option_creates_runtime_after_reload(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_ASK,
                    imported_service_ids=frozenset({"123456"}),
                ),
            )

            await coordinator.async_refresh_discovery()

            self.assertEqual(coordinator.snapshot().pending_service_ids, ())
            self.assertEqual(coordinator.snapshot().runtime_service_ids, ("123456",))

        asyncio.run(run())

    def test_ignored_service_option_does_not_create_pending_issue_or_runtime(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_ASK,
                    ignored_service_ids=frozenset({"123456"}),
                ),
            )

            result = await coordinator.async_refresh_discovery()

            self.assertEqual(result.newly_discovered, set())
            self.assertEqual(coordinator.snapshot().pending_service_ids, ())
            self.assertEqual(coordinator.snapshot().runtime_service_ids, ())

        asyncio.run(run())

    def test_discovery_notifies_listeners(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
            calls: list[str] = []
            remove = coordinator.async_add_listener(lambda: calls.append("updated"))

            await coordinator.async_refresh_discovery()
            remove()
            await coordinator.async_refresh_discovery()

            self.assertEqual(calls, ["updated"])

        asyncio.run(run())

    def test_missing_threshold_marks_existing_runtime_unavailable(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, missing_service_threshold=3),
                known={"123456": ManagedServiceState("123456", missing_count=2)},
            )
            client.services = []

            await coordinator.async_refresh_discovery()

            self.assertEqual(coordinator.snapshot().missing_service_ids, ("123456",))
            self.assertIn("123456", coordinator.services)
            self.assertFalse(coordinator.services["123456"].state.available)

        asyncio.run(run())

    def test_safe_start_blocks_settle_window_and_force_sends(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=600),
                now_fn=lambda: 260,
            )
            await coordinator.async_refresh_discovery()
            runtime = coordinator.get_runtime("123456")
            runtime.update_server(server("gs_installation"), observed_at=100)
            client.servers["123456"] = server("stopped")

            with self.assertRaises(NitradoControlError):
                await coordinator.async_start_service("123456")

            await coordinator.async_start_service("123456", force=True)

            self.assertEqual(client.started, [("123456", "palworldxb")])

        asyncio.run(run())

    def test_palworld_start_does_not_require_existing_save_tree(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=0),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            await coordinator.async_start_service("123456")

            self.assertEqual(client.started, [("123456", "palworldxb")])

        asyncio.run(run())

    def test_start_revalidates_after_before_start_hook(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("stopped", game="unknown")

            class StateChangingProfile(GenericProfile):
                profile_id = "state_changing"

                def lifecycle_hooks(self):
                    async def change_state(context):
                        client.servers["123456"] = server("started", game="unknown")

                    return (LifecycleHookDeclaration("before_start", LifecycleEvent.BEFORE_START, change_state),)

            profile = StateChangingProfile()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=0),
            )
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                new=AsyncMock(return_value=profile_candidate(profile)),
            ):
                await coordinator.async_refresh_discovery()
                with self.assertRaises(NitradoControlError):
                    await coordinator.async_start_service("123456")

            self.assertEqual(client.started, [])

        asyncio.run(run())

    def test_profile_replacement_between_control_verdict_and_hook_aborts_command(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = server("started", game="unknown")
            verdict_started = asyncio.Event()
            release_verdict = asyncio.Event()
            hooks: list[str] = []

            class FirstProfile(GenericProfile):
                profile_id = "first_control"

                def __init__(self) -> None:
                    self.stop_calls = 0

                async def can_stop(self, context):
                    self.stop_calls += 1
                    if self.stop_calls == 2:
                        verdict_started.set()
                        await release_verdict.wait()
                    return SUPPORTED

            class SecondProfile(GenericProfile):
                profile_id = "second_control"

                def lifecycle_hooks(self):
                    async def wrong_hook(context):
                        hooks.append("second")

                    return (
                        LifecycleHookDeclaration(
                            "before_stop",
                            LifecycleEvent.BEFORE_STOP,
                            wrong_hook,
                        ),
                    )

            coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
            runtime = ServiceRuntime(ManagedServiceState("123456"))
            runtime._apply_selected_profile(FirstProfile())
            coordinator.services["123456"] = runtime

            command = asyncio.create_task(coordinator.async_stop_service("123456", reason="race"))
            await verdict_started.wait()

            async def replace_profile() -> None:
                async with runtime.profile_operation_lock:
                    runtime._apply_selected_profile(SecondProfile())

            replacement = asyncio.create_task(replace_profile())
            await asyncio.sleep(0)
            release_verdict.set()
            result = await asyncio.gather(command, return_exceptions=True)
            await replacement

            self.assertIsInstance(result[0], NitradoControlError)
            self.assertEqual(hooks, [])
            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_concurrent_start_commands_cross_transport_once(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("stopped", game="unknown")
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=30),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            results = await asyncio.gather(
                coordinator.async_start_service("123456", force=True),
                coordinator.async_start_service("123456", force=True),
                return_exceptions=True,
            )

            self.assertEqual(client.started, [("123456", "unknown")])
            self.assertEqual(sum(isinstance(result, NitradoControlError) for result in results), 1)

        asyncio.run(run())

    def test_remove_service_blocks_start_waiting_on_final_refresh(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("stopped", game="unknown")
            final_refresh_started = asyncio.Event()
            release_final_refresh = asyncio.Event()
            fetch_count = 0
            original_fetch = client.fetch_server

            async def delayed_second_fetch(service_id: str) -> ParsedServer:
                nonlocal fetch_count
                fetch_count += 1
                if fetch_count == 2:
                    final_refresh_started.set()
                    await release_final_refresh.wait()
                return await original_fetch(service_id)

            client.fetch_server = delayed_second_fetch  # type: ignore[method-assign]
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=0),
            )
            await coordinator.async_refresh_discovery()
            command = asyncio.create_task(coordinator.async_start_service("123456", force=True))
            await final_refresh_started.wait()

            drained = coordinator.remove_service("123456")
            release_final_refresh.set()
            results = await asyncio.gather(*drained, return_exceptions=True)

            self.assertIn(command, drained)
            self.assertTrue(any(isinstance(result, NitradoControlError) for result in results))
            self.assertEqual(client.started, [])

        asyncio.run(run())

    def test_shutdown_waits_for_start_already_inside_transport(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("stopped", game="unknown")
            transport_started = asyncio.Event()
            release_transport = asyncio.Event()

            async def delayed_start(service_id: str, game_short: str) -> None:
                transport_started.set()
                await release_transport.wait()
                client.started.append((service_id, game_short))

            client.start_server = delayed_start  # type: ignore[method-assign]
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=0),
            )
            await coordinator.async_refresh_discovery()
            command = asyncio.create_task(coordinator.async_start_service("123456", force=True))
            await transport_started.wait()

            shutdown = asyncio.create_task(coordinator.async_shutdown())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            release_transport.set()
            await shutdown
            await command

            self.assertEqual(client.started, [("123456", "unknown")])
            self.assertEqual(coordinator._active_control_tasks, {})

        asyncio.run(run())

    def test_remove_tombstone_blocks_discovery_readd_while_start_drains(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("stopped", game="unknown")
            transport_started = asyncio.Event()
            release_transport = asyncio.Event()

            async def delayed_start(service_id: str, game_short: str) -> None:
                transport_started.set()
                await release_transport.wait()
                client.started.append((service_id, game_short))

            client.start_server = delayed_start  # type: ignore[method-assign]
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=0),
            )
            await coordinator.async_refresh_discovery()
            first = asyncio.create_task(coordinator.async_start_service("123456", force=True))
            await transport_started.wait()

            drained = coordinator.remove_service("123456")
            await coordinator.apply_discovered_services(client.services)

            self.assertNotIn("123456", coordinator.services)
            with self.assertRaises(NitradoServiceUnknownError):
                await coordinator.async_start_service("123456", force=True)
            release_transport.set()
            await asyncio.gather(*drained, return_exceptions=True)
            await first

            self.assertEqual(client.started, [("123456", "unknown")])

        asyncio.run(run())

    def test_shutdown_still_drains_control_after_service_was_removed(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("stopped", game="unknown")
            transport_started = asyncio.Event()
            release_transport = asyncio.Event()

            async def delayed_start(service_id: str, game_short: str) -> None:
                transport_started.set()
                await release_transport.wait()
                client.started.append((service_id, game_short))

            client.start_server = delayed_start  # type: ignore[method-assign]
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=0),
            )
            await coordinator.async_refresh_discovery()
            command = asyncio.create_task(coordinator.async_start_service("123456", force=True))
            await transport_started.wait()

            coordinator.remove_service("123456")
            shutdown = asyncio.create_task(coordinator.async_shutdown())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            release_transport.set()
            await shutdown
            await command

            self.assertEqual(client.started, [("123456", "unknown")])
            self.assertEqual(coordinator._active_control_tasks, {})
            self.assertEqual(coordinator._control_transport_tasks, set())

        asyncio.run(run())

    def test_remove_wins_if_import_is_waiting_on_profile_selection(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
            await coordinator.async_refresh_discovery()
            selection_started = asyncio.Event()
            release_selection = asyncio.Event()

            async def delayed_selection(runtime, service_value, server_value):
                selection_started.set()
                await release_selection.wait()

            with patch.object(ServiceRuntime, "async_select_profile", new=delayed_selection):
                importing = asyncio.create_task(coordinator.import_service("123456"))
                await selection_started.wait()
                coordinator.remove_service("123456")
                release_selection.set()
                with self.assertRaises(NitradoServiceUnknownError):
                    await importing

            self.assertNotIn("123456", coordinator.services)
            self.assertIn("123456", coordinator._service_exclusion_tombstones)

        asyncio.run(run())

    def test_shutdown_wins_if_import_is_waiting_on_profile_selection(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
            await coordinator.async_refresh_discovery()
            selection_started = asyncio.Event()
            release_selection = asyncio.Event()

            async def delayed_selection(runtime, service_value, server_value):
                selection_started.set()
                await release_selection.wait()

            with patch.object(ServiceRuntime, "async_select_profile", new=delayed_selection):
                importing = asyncio.create_task(coordinator.import_service("123456"))
                await selection_started.wait()
                await coordinator.async_shutdown()
                release_selection.set()
                with self.assertRaises(NitradoServiceUnknownError):
                    await importing

            self.assertNotIn("123456", coordinator.services)

        asyncio.run(run())

    def test_removed_service_refresh_does_not_dispatch_lifecycle_after_fetch(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("started", game="unknown")
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()
            hooks: list[str] = []

            class HookProfile(GenericProfile):
                profile_id = "refresh_hook"

                def lifecycle_hooks(self):
                    async def record(context):
                        hooks.append(context.require_service_id())

                    return (
                        LifecycleHookDeclaration(
                            "status_refresh",
                            LifecycleEvent.STATUS_REFRESH,
                            record,
                        ),
                    )

            original_fetch = client.fetch_server

            async def delayed_fetch(service_id: str) -> ParsedServer:
                fetch_started.set()
                await release_fetch.wait()
                return await original_fetch(service_id)

            client.fetch_server = delayed_fetch  # type: ignore[method-assign]
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                new=AsyncMock(return_value=profile_candidate(HookProfile())),
            ):
                await coordinator.async_refresh_discovery()
                refreshing = asyncio.create_task(coordinator.async_refresh_service("123456"))
                await fetch_started.wait()
                coordinator.remove_service("123456")
                release_fetch.set()
                await refreshing

            self.assertEqual(hooks, [])

        asyncio.run(run())

    def test_status_refresh_caches_control_verdicts(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=600),
                now_fn=lambda: 100,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertIsNotNone(runtime.last_start_verdict)
            self.assertFalse(runtime.last_start_verdict.allowed)
            self.assertIn("settle window", runtime.last_start_verdict.reason)
            self.assertIsNotNone(runtime.last_stop_verdict)
            self.assertFalse(runtime.last_stop_verdict.allowed)

        asyncio.run(run())

    def test_status_refresh_merges_palworld_rest_enrichment(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = server("started")
            client.root_files = {
                "entries": [
                    {
                        "type": "dir",
                        "name": "palworldxb",
                        "path": "/games/ni123_456/ftproot/palworldxb",
                    }
                ]
            }
            client.downloads[
                "/games/ni123_456/ftproot/palworldxb/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
            ] = 'RESTAPIEnabled=True,PublicPort=8211,RESTAPIPort=8212,AdminPassword="secret")'
            client.external_json = {
                "http://203.0.113.10:12346/v1/api/info": {"servername": "Example Palworld Live"},
                "http://203.0.113.10:12346/v1/api/metrics": {"currentplayernum": 2, "maxplayernum": 12},
                "http://203.0.113.10:12346/v1/api/players": {"players": [{"name": "Alex"}, {"accountName": "Jordan"}]},
            }
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(runtime.server.player_count, 2)
            self.assertEqual(runtime.server.player_max, 12)
            self.assertEqual(runtime.server.player_names, ("Alex", "Jordan"))
            self.assertEqual(runtime.server.player_source, "palworld_rest")

        asyncio.run(run())

    def test_stopped_server_normalizes_players_to_zero_without_rest_complaint(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("stopped"),
                player_count=None,
                player_names=("stale-player",),
                query_valid=False,
                player_source=None,
            )
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(runtime.server.player_count, 0)
            self.assertEqual(runtime.server.player_names, ())
            self.assertEqual(runtime.server.player_source, "server_status")
            self.assertFalse(runtime.server.query_valid)
            self.assertTrue(runtime.last_idle_shutdown_verdict.allowed)
            self.assertIn("status is stopped", runtime.last_reset_reason)
            self.assertNotIn("Palworld REST", runtime.last_reset_reason)
            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_transition_state_suppresses_idle_shutdown_without_player_source_complaint(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("stopping"),
                player_count=None,
                player_names=(),
                query_valid=False,
                player_source=None,
            )
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertTrue(runtime.last_idle_shutdown_verdict.allowed)
            self.assertIn("status is stopping", runtime.last_reset_reason)
            self.assertNotIn("player", runtime.last_reset_reason.lower())
            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_palworld_enrichment_failure_cannot_publish_nitrado_false_zero(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("started"),
                player_count=0,
                player_names=(),
                query_valid=True,
                player_source="nitrado_query",
            )
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            await coordinator.async_refresh_discovery()

            with patch(
                "custom_components.nitrado_gameserver.plugins.palworld.PalworldProfile.enrich_status",
                new=AsyncMock(side_effect=RuntimeError("unexpected Palworld failure")),
            ):
                runtime = await coordinator.async_refresh_service("123456")

            self.assertIsNone(runtime.server.player_count)
            self.assertEqual(runtime.server.player_names, ())
            self.assertFalse(runtime.server.query_valid)
            self.assertIsNone(runtime.server.player_source)
            self.assertEqual(runtime.last_profile_refresh_error, "Profile enrichment failed; details were logged.")

        asyncio.run(run())

    def test_broken_profile_does_not_block_later_service_refreshes(self) -> None:
        class ExplodingProfile(GenericProfile):
            profile_id = "exploding"

            async def enrich_status(self, client, service, server, context):
                raise RuntimeError("profile exploded")

        class GoodProfile(GenericProfile):
            profile_id = "good"

            async def enrich_status(self, client, service, server, context):
                return ProfileStatus(player_count=7, query_valid=True, player_source="profile")

        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service("111", "first"), service("222", "second")]
            client.servers = {
                "111": server("started", service_id="111", game="first"),
                "222": server("started", service_id="222", game="second"),
            }
            exploding = ExplodingProfile()
            good = GoodProfile()

            async def choose(service_value, server_value=None):
                return profile_candidate(exploding if service_value.service_id == "111" else good)

            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                side_effect=choose,
            ):
                await coordinator.async_refresh_discovery()
                await coordinator.async_refresh_managed_services()

            first = coordinator.get_runtime("111")
            second = coordinator.get_runtime("222")
            self.assertEqual(first.last_profile_refresh_error, "Profile enrichment failed; details were logged.")
            self.assertTrue(first.status_fresh)
            self.assertIsNone(first.server.player_count)
            self.assertFalse(first.server.query_valid)
            self.assertIsNone(first.server.player_source)
            self.assertIsNone(second.last_profile_refresh_error)
            self.assertEqual(second.server.player_count, 7)

        asyncio.run(run())

    def test_same_service_refreshes_do_not_overlap(self) -> None:
        class SlowFetchClient(FakeNitradoClient):
            def __init__(self) -> None:
                super().__init__()
                self.services = [service(game="unknown")]
                self.servers = {"123456": server("stopped", game="unknown")}
                self.active = 0
                self.max_active = 0

            async def fetch_server(self, service_id: str) -> ParsedServer:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.005)
                result = self.servers[service_id]
                self.active -= 1
                return result

        async def run() -> None:
            client = SlowFetchClient()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            await coordinator.async_refresh_discovery()
            await asyncio.gather(
                coordinator.async_refresh_service("123456"),
                coordinator.async_refresh_service("123456"),
            )

            self.assertEqual(client.max_active, 1)

        asyncio.run(run())

    def test_safe_stop_sends_from_running_status(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = server("started")
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
            )
            await coordinator.async_refresh_discovery()

            await coordinator.async_stop_service("123456", reason="Idle")

            self.assertEqual(client.stopped, [("123456", "Idle")])

        asyncio.run(run())

    def test_concurrent_stop_commands_cross_transport_once(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="unknown")]
            client.servers["123456"] = server("started", game="unknown")
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD, settle_seconds=30),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            results = await asyncio.gather(
                coordinator.async_stop_service("123456", reason="One"),
                coordinator.async_stop_service("123456", reason="Two"),
                return_exceptions=True,
            )

            self.assertEqual(len(client.stopped), 1)
            self.assertEqual(sum(isinstance(result, NitradoControlError) for result in results), 1)

        asyncio.run(run())

    def test_palworld_idle_shutdown_dry_run_does_not_send_stop(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = server("started")
            client.root_files = {
                "entries": [
                    {
                        "type": "dir",
                        "name": "palworldxb",
                        "path": "/games/ni123_456/ftproot/palworldxb",
                    }
                ]
            }
            client.downloads[
                "/games/ni123_456/ftproot/palworldxb/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
            ] = 'RESTAPIEnabled=True,PublicPort=8211,RESTAPIPort=8212,AdminPassword="secret")'
            client.external_json = {
                "http://203.0.113.10:12346/v1/api/info": {"servername": "Example Palworld Live"},
                "http://203.0.113.10:12346/v1/api/metrics": {"currentplayernum": 0, "maxplayernum": 12},
                "http://203.0.113.10:12346/v1/api/players": {"players": []},
            }
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    dry_run_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(client.stopped, [])
            self.assertIn("Dry-run", runtime.last_shutdown_reason)
            self.assertEqual(runtime.idle_minutes(1000), 0)

        asyncio.run(run())

    def test_palworld_idle_shutdown_sends_stop_after_verified_zero_checks(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = server("started")
            client.root_files = {
                "entries": [
                    {
                        "type": "dir",
                        "name": "palworldxb",
                        "path": "/games/ni123_456/ftproot/palworldxb",
                    }
                ]
            }
            client.downloads[
                "/games/ni123_456/ftproot/palworldxb/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
            ] = 'RESTAPIEnabled=True,PublicPort=8211,RESTAPIPort=8212,AdminPassword="secret")'
            client.external_json = {
                "http://203.0.113.10:12346/v1/api/info": {"servername": "Example Palworld Live"},
                "http://203.0.113.10:12346/v1/api/metrics": {"currentplayernum": 0, "maxplayernum": 12},
                "http://203.0.113.10:12346/v1/api/players": {"players": []},
            }
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(client.stopped, [("123456", "zero players for configured idle period")])
            self.assertEqual(runtime.last_shutdown_reason, "zero players for configured idle period")

        asyncio.run(run())

    def test_cancel_pending_shutdown_cancels_in_flight_stop(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=30)
            await coordinator.async_refresh_discovery()

            refresh_task = asyncio.create_task(coordinator.async_refresh_service("123456"))
            await _wait_for_shutdown_pending(coordinator)
            await coordinator.async_cancel_pending_shutdown("123456")
            await refresh_task

            self.assertEqual(client.stopped, [])
            self.assertFalse(coordinator.get_runtime("123456").shutdown_pending)
            self.assertEqual(
                coordinator.get_runtime("123456").last_reset_reason,
                "Pending shutdown canceled manually",
            )

        asyncio.run(run())

    def test_disabling_shutdown_during_final_check_blocks_stop(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=1)
            await coordinator.async_refresh_discovery()

            refresh_task = asyncio.create_task(coordinator.async_refresh_service("123456"))
            await _wait_for_shutdown_pending(coordinator)
            coordinator.options = replace(
                coordinator.options,
                idle_shutdown_service_ids=frozenset(),
            )
            await refresh_task

            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_revoked_shutdown_stays_dead_after_reenable(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=30)
            await coordinator.async_refresh_discovery()

            refresh_task = asyncio.create_task(coordinator.async_refresh_service("123456"))
            await _wait_for_shutdown_pending(coordinator)
            coordinator.options = replace(
                coordinator.options,
                idle_shutdown_service_ids=frozenset(),
            )
            coordinator.revoke_pending_shutdown("123456", "Automatic shutdown disabled")
            coordinator.options = replace(
                coordinator.options,
                idle_shutdown_service_ids=frozenset({"123456"}),
            )
            await refresh_task

            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_coordinator_shutdown_cancels_pending_stop(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=30)
            await coordinator.async_refresh_discovery()

            refresh_task = asyncio.create_task(coordinator.async_refresh_service("123456"))
            await _wait_for_shutdown_pending(coordinator)
            await coordinator.async_shutdown()
            await refresh_task

            self.assertEqual(client.stopped, [])
            self.assertEqual(coordinator._shutdown_tasks, {})

        asyncio.run(run())

    def test_coordinator_shutdown_blocks_stop_from_in_flight_refresh(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=0)
            await coordinator.async_refresh_discovery()
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()
            original_fetch = client.fetch_server

            async def delayed_fetch(service_id: str) -> ParsedServer:
                fetch_started.set()
                await release_fetch.wait()
                return await original_fetch(service_id)

            client.fetch_server = delayed_fetch  # type: ignore[method-assign]
            refresh = asyncio.create_task(coordinator.async_refresh_service("123456"))
            await fetch_started.wait()

            await coordinator.async_shutdown()
            release_fetch.set()
            await refresh
            await asyncio.sleep(0)

            self.assertEqual(client.stopped, [])
            self.assertEqual(coordinator._shutdown_tasks, {})
            self.assertTrue(coordinator._shutting_down)

        asyncio.run(run())

    def test_shutdown_drains_automatic_stop_already_inside_transport(self) -> None:
        async def run() -> None:
            client, coordinator = _idle_shutdown_fixture(final_delay=0)
            await coordinator.async_refresh_discovery()
            transport_started = asyncio.Event()
            release_transport = asyncio.Event()

            async def delayed_stop(service_id: str, reason: str) -> None:
                transport_started.set()
                await release_transport.wait()
                client.stopped.append((service_id, reason))

            client.stop_server = delayed_stop  # type: ignore[method-assign]
            refresh = asyncio.create_task(coordinator.async_refresh_service("123456"))
            await transport_started.wait()

            shutdown = asyncio.create_task(coordinator.async_shutdown())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            release_transport.set()
            await shutdown
            await refresh

            self.assertEqual(client.stopped, [("123456", "zero players for configured idle period")])
            self.assertEqual(coordinator._control_transport_tasks, set())

        asyncio.run(run())

    def test_cancel_after_automatic_stop_transport_drains_success_audit(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="hooked")]
            client.servers["123456"] = replace(
                server("started", game="hooked"),
                player_count=0,
                player_names=(),
            )
            after_stop_started = asyncio.Event()
            release_after_stop = asyncio.Event()

            class SlowAfterStopProfile(GenericProfile):
                profile_id = "slow_after_stop"
                idle_shutdown_supported = True

                def matches(self, service, server=None):
                    return MatchResult(service.game == "hooked", confidence=1.0)

                async def enrich_status(self, client, service, server, context):
                    return ProfileStatus(
                        player_count=0,
                        player_max=server.player_max,
                        player_names=(),
                        player_source="profile",
                        query_valid=True,
                    )

                def idle_shutdown_capability(self, context):
                    return SUPPORTED

                def lifecycle_hooks(self):
                    async def wait_after_stop(context):
                        after_stop_started.set()
                        await release_after_stop.wait()

                    return (
                        LifecycleHookDeclaration(
                            key="after_stop",
                            event=LifecycleEvent.AFTER_STOP,
                            hook_fn=wait_after_stop,
                        ),
                    )

            async def choose_profile(service_value, server_value=None, profiles=None):
                return profile_candidate(SlowAfterStopProfile())

            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                ),
                now_fn=lambda: 1000,
            )
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                new=choose_profile,
            ):
                await coordinator.async_refresh_discovery()
                refresh = asyncio.create_task(coordinator.async_refresh_service("123456"))
                await after_stop_started.wait()

                cancel = asyncio.create_task(coordinator.async_cancel_pending_shutdown("123456"))
                await asyncio.sleep(0)
                self.assertFalse(cancel.done())
                release_after_stop.set()
                await cancel
                await refresh

            runtime = coordinator.get_runtime("123456")
            self.assertEqual(client.stopped, [("123456", "zero players for configured idle period")])
            self.assertEqual(runtime.last_shutdown_reason, "zero players for configured idle period")
            self.assertEqual(runtime.last_reset_reason, "Stop confirmed")

        asyncio.run(run())

    def test_coordinator_shutdown_cancels_active_profile_action(self) -> None:
        async def run() -> None:
            started = asyncio.Event()

            class ActionProfile(GenericProfile):
                profile_id = "action_profile"

                def actions(self):
                    async def wait_forever(context):
                        started.set()
                        await asyncio.Event().wait()

                    return (ActionDeclaration("wait", "Wait", wait_forever),)

            runtime = ServiceRuntime(ManagedServiceState("123456"))
            runtime.profile = ActionProfile()
            coordinator = NitradoAccountCoordinator(
                FakeNitradoClient(),  # type: ignore[arg-type]
                services={"123456": runtime},
            )
            task = asyncio.create_task(coordinator.async_run_profile_action("123456", "wait"))
            await started.wait()

            await coordinator.async_shutdown()

            self.assertTrue(task.cancelled())
            self.assertEqual(coordinator._active_profile_mutation_tasks, {})

        asyncio.run(run())

    def test_profile_dispatch_rejects_generation_changed_after_authorization(self) -> None:
        async def run() -> None:
            executed: list[str] = []

            class ActionProfile(GenericProfile):
                profile_id = "shared_action"

                def __init__(self, label: str) -> None:
                    self.label = label

                def actions(self):
                    async def execute(context):
                        executed.append(self.label)

                    return (ActionDeclaration("run", "Run", execute),)

            client = FakeNitradoClient()
            coordinator = NitradoAccountCoordinator(client)  # type: ignore[arg-type]
            runtime = ServiceRuntime(ManagedServiceState("123456"))
            runtime._apply_selected_profile(ActionProfile("authenticated"))
            coordinator.services["123456"] = runtime
            token = coordinator.profile_dispatch_token("123456")

            runtime.profile = ActionProfile("privileged")
            runtime.profile_manifest = profile_extension_manifest(runtime.profile)
            runtime.profile_generation += 1

            with self.assertRaises(ProfileExtensionError):
                await coordinator.async_run_profile_action(
                    "123456",
                    "run",
                    expected_profile=token,
                )
            self.assertEqual(executed, [])

        asyncio.run(run())

    def test_unregistered_external_profile_is_revoked_before_reload(self) -> None:
        async def run() -> None:
            executed: list[str] = []

            class ExternalProfile(GenericProfile):
                profile_id = "external_revocation"

                def actions(self):
                    async def execute(context):
                        executed.append("ran")

                    return (ActionDeclaration("run", "Run", execute),)

            unregister = register_profile(ExternalProfile)
            try:
                runtime = ServiceRuntime(ManagedServiceState("123456"))
                runtime._apply_selected_profile(ExternalProfile())
                coordinator = NitradoAccountCoordinator(
                    FakeNitradoClient(),  # type: ignore[arg-type]
                    services={"123456": runtime},
                )
                unregister()

                with self.assertRaises(ProfileExtensionError):
                    await coordinator.async_run_profile_action("123456", "run")
                self.assertFalse(coordinator._shutdown_intent_valid(runtime, runtime.shutdown_generation))
                self.assertEqual(executed, [])
            finally:
                unregister()

        asyncio.run(run())

    def test_new_profile_registration_revokes_stale_generic_dispatch(self) -> None:
        """A registry addition may replace Generic and must revoke old work immediately."""

        class NewlyAvailableProfile(GenericProfile):
            profile_id = "newly_available_profile"

        runtime = ServiceRuntime(ManagedServiceState("123456"))
        runtime._apply_selected_profile(GenericProfile())
        coordinator = NitradoAccountCoordinator(
            FakeNitradoClient(),  # type: ignore[arg-type]
            services={"123456": runtime},
        )
        token = coordinator.profile_dispatch_token("123456")

        unregister = register_profile(NewlyAvailableProfile)
        try:
            with self.assertRaises(ProfileExtensionError):
                coordinator.require_profile_dispatch_token("123456", token)
            self.assertFalse(coordinator._shutdown_intent_valid(runtime, runtime.shutdown_generation))
        finally:
            unregister()

    def test_palworld_idle_shutdown_rejects_nitrado_zero_when_rest_settings_missing(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("started"),
                player_count=0,
                player_names=(),
                query_valid=True,
                player_source="nitrado_query",
            )
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(runtime.server.player_source, "nitrado_query")
            self.assertFalse(runtime.server.query_valid)
            self.assertEqual(client.stopped, [])
            self.assertIn("Palworld REST", runtime.last_reset_reason)

        asyncio.run(run())

    def test_palworld_rest_and_idle_shutdown_are_blocked_without_plaintext_consent(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("started"),
                player_count=0,
                player_names=(),
                query_valid=True,
                player_source="nitrado_query",
            )
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertFalse(runtime.server.query_valid)
            self.assertEqual(client.stopped, [])
            self.assertIn("Palworld REST", runtime.last_reset_reason)
            profile_status = runtime.extra["profile_status"]
            self.assertEqual(
                profile_status.extra["palworld_rest_blocked"],
                "insecure_transport_not_approved",
            )

        asyncio.run(run())

    def test_palworld_idle_shutdown_rejects_nitrado_players_endpoint_zero(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("started"),
                player_count=None,
                player_names=(),
                query_valid=False,
                player_source=None,
            )
            client.players["123456"] = ()
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertIsNone(runtime.server.player_source)
            self.assertFalse(runtime.server.query_valid)
            self.assertIsNone(runtime.server.player_count)
            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_palworld_idle_shutdown_rejects_nitrado_zero_when_rest_is_disabled(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.servers["123456"] = replace(
                server("started"),
                player_count=0,
                player_names=(),
                query_valid=True,
                player_source="nitrado_query",
            )
            client.root_files = {
                "entries": [
                    {
                        "type": "dir",
                        "name": "palworldxb",
                        "path": "/games/ni123_456/ftproot/palworldxb",
                    }
                ]
            }
            client.downloads[
                "/games/ni123_456/ftproot/palworldxb/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"
            ] = "RESTAPIEnabled=False)"
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                    profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(runtime.server.player_source, "nitrado_query")
            self.assertFalse(runtime.server.query_valid)
            self.assertEqual(client.stopped, [])

        asyncio.run(run())

    def test_generic_profile_does_not_idle_shutdown(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [
                NitradoService(
                    service_id="123456",
                    name="ARK",
                    game="arksa",
                    game_human="ARK",
                    folder_short="arksa",
                    type_human="Gameserver",
                    raw_redacted={},
                )
            ]
            client.servers["123456"] = ParsedServer(
                service_id="123456",
                raw_status="started",
                server_name="ARK",
                address="203.0.113.10:12345",
                game_short="arksa",
                game_human="ARK",
                player_count=0,
                player_max=10,
                player_names=(),
                query_valid=True,
                player_source="nitrado_query",
                raw_redacted={},
            )
            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(
                    discovery_mode=DISCOVERY_AUTO_ADD,
                    idle_shutdown_service_ids=frozenset({"123456"}),
                    idle_minutes={"123456": 0},
                    startup_cooldown_minutes={"123456": 0},
                    final_shutdown_check_delay_seconds=0,
                ),
                now_fn=lambda: 1000,
            )
            await coordinator.async_refresh_discovery()

            runtime = await coordinator.async_refresh_service("123456")

            self.assertEqual(client.stopped, [])
            self.assertEqual(
                runtime.last_reset_reason, "Selected game profile does not support automatic idle shutdown"
            )

        asyncio.run(run())

    def test_before_stop_hook_cannot_reuse_pre_hook_zero_player_snapshot(self) -> None:
        async def run() -> None:
            client = FakeNitradoClient()
            client.services = [service(game="hooked")]
            client.servers["123456"] = server("started", game="hooked")

            class HookedProfile(GenericProfile):
                profile_id = "hooked"
                idle_shutdown_supported = True

                async def enrich_status(self, client, service, server, context):
                    return ProfileStatus(
                        player_count=server.player_count,
                        player_max=server.player_max,
                        player_names=server.player_names,
                        player_source="profile",
                        query_valid=True,
                    )

                def idle_shutdown_capability(self, context):
                    return SUPPORTED

                def lifecycle_hooks(self):
                    async def player_joins(context):
                        client.servers["123456"] = replace(
                            client.servers["123456"],
                            player_count=1,
                            player_names=("LatePlayer",),
                        )

                    return (
                        LifecycleHookDeclaration(
                            key="player_joins",
                            event=LifecycleEvent.BEFORE_STOP,
                            hook_fn=player_joins,
                        ),
                    )

            async def choose_profile(service_value, server_value=None, profiles=None):
                return profile_candidate(HookedProfile())

            coordinator = NitradoAccountCoordinator(
                client,  # type: ignore[arg-type]
                options=AccountCoordinatorOptions(discovery_mode=DISCOVERY_AUTO_ADD),
                now_fn=lambda: 1000,
            )
            with patch(
                "custom_components.nitrado_gameserver.runtime.async_select_profile_candidate",
                new=choose_profile,
            ):
                await coordinator.async_refresh_discovery()
                with self.assertRaises(NitradoControlError):
                    await coordinator.async_stop_service(
                        "123456",
                        _pre_send_guard=lambda: coordinator.get_runtime("123456").server.player_count == 0,
                    )

            self.assertEqual(client.stopped, [])
            self.assertEqual(coordinator.get_runtime("123456").server.player_count, 1)

        asyncio.run(run())


def _idle_shutdown_fixture(*, final_delay: int) -> tuple[FakeNitradoClient, NitradoAccountCoordinator]:
    """Return a Palworld zero-player coordinator ready to enter final checks."""

    client = FakeNitradoClient()
    client.servers["123456"] = server("started")
    client.root_files = {
        "entries": [
            {
                "type": "dir",
                "name": "palworldxb",
                "path": "/games/ni123_456/ftproot/palworldxb",
            }
        ]
    }
    client.downloads["/games/ni123_456/ftproot/palworldxb/Pal/Saved/Config/WindowsServer/PalWorldSettings.ini"] = (
        'RESTAPIEnabled=True,PublicPort=8211,RESTAPIPort=8212,AdminPassword="secret")'
    )
    client.external_json = {
        "http://203.0.113.10:12346/v1/api/info": {"servername": "Example Palworld Live"},
        "http://203.0.113.10:12346/v1/api/metrics": {"currentplayernum": 0, "maxplayernum": 12},
        "http://203.0.113.10:12346/v1/api/players": {"players": []},
    }
    coordinator = NitradoAccountCoordinator(
        client,  # type: ignore[arg-type]
        options=AccountCoordinatorOptions(
            discovery_mode=DISCOVERY_AUTO_ADD,
            idle_shutdown_service_ids=frozenset({"123456"}),
            idle_minutes={"123456": 0},
            startup_cooldown_minutes={"123456": 0},
            final_shutdown_check_delay_seconds=final_delay,
            profile_options={"palworld": {"allow_insecure_rest": {"123456": True}}},
        ),
        now_fn=lambda: 1000,
    )
    return client, coordinator


async def _wait_for_shutdown_pending(coordinator: NitradoAccountCoordinator) -> None:
    """Wait until the tracked final-check task has armed."""

    for _ in range(100):
        if coordinator.get_runtime("123456").shutdown_pending:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("automatic shutdown did not become pending")


if __name__ == "__main__":
    unittest.main()
