"""Tests for pure entity declarations."""

from __future__ import annotations

import asyncio
import sys
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoService,
    ParsedServer,
)
from custom_components.nitrado_gameserver.binary_sensor import (
    NitradoServiceBinarySensor,
)
from custom_components.nitrado_gameserver.button import NitradoServiceButton
from custom_components.nitrado_gameserver.coordinator import AccountCoordinatorOptions, NitradoAccountCoordinator
from custom_components.nitrado_gameserver.entities import (
    SERVICE_BINARY_SENSORS,
    SERVICE_BUTTONS,
    SERVICE_SELECTS,
    SERVICE_SENSORS,
    SERVICE_SWITCHES,
    core_service_entities,
    profile_service_entities,
    service_binary_sensor_descriptions,
    service_button_descriptions,
    service_number_descriptions,
    service_select_descriptions,
    service_sensor_descriptions,
    service_switch_descriptions,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.number import NitradoServiceNumber
from custom_components.nitrado_gameserver.plugins.base import (
    SUPPORTED,
    EntityDeclaration,
    MatchResult,
    blocked,
)
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile
from custom_components.nitrado_gameserver.runtime import ServiceRuntime
from custom_components.nitrado_gameserver.select import NitradoServiceSelect
from custom_components.nitrado_gameserver.sensor import NitradoServiceSensor
from custom_components.nitrado_gameserver.switch import NitradoServiceSwitch


def runtime_fixture() -> ServiceRuntime:
    """Build a runtime fixture with service and server data."""

    runtime = ServiceRuntime(ManagedServiceState("123456"))
    runtime.update_service(
        NitradoService(
            service_id="123456",
            name="Palworld Xbox",
            game="palworldxb",
            game_human="Palworld",
            folder_short="palworldxb",
            type_human="Gameserver",
            raw_redacted={},
        )
    )
    runtime.update_server(
        ParsedServer(
            service_id="123456",
            raw_status="started",
            server_name="Example Server",
            address="203.0.113.10:8211",
            game_short="palworldxb",
            game_human="Palworld",
            player_count=2,
            player_max=10,
            player_names=("Alex", "Jordan"),
            query_valid=True,
            player_source="nitrado_query",
            raw_redacted={},
        ),
        observed_at=1000,
    )
    return runtime


class FakeCoordinator:
    """Minimal coordinator double for HA entity wrapper tests."""

    def __init__(
        self,
        runtime: ServiceRuntime,
        *,
        idle_supported: bool = True,
        idle_configurable: bool | None = None,
        auto_shutdown_status: str | None = "Waiting for zero players",
    ) -> None:
        self.runtime = runtime
        self.services = {runtime.state.identity.service_id: runtime}
        self.account_entry_id = "entry"
        self.idle_supported = idle_supported
        self.idle_configurable = idle_supported if idle_configurable is None else idle_configurable
        self.auto_shutdown_status_value = auto_shutdown_status
        self.client = object()
        # Profile mutation callbacks receive the coordinator's service-bound
        # file transport. These entity tests do not exercise file I/O, but the
        # fixture must still satisfy the public action-context contract.
        self.profile_transport = object()
        self.calls: list[tuple[str, str, dict]] = []
        self.listeners: list = []
        self.options = AccountCoordinatorOptions()

    def get_runtime(self, service_id: str) -> ServiceRuntime:
        """Return fixture runtime."""

        self.assert_service_id = service_id
        return self.runtime

    def profile_dispatch_token(self, service_id: str):
        """Return the same generation token as the real coordinator."""

        runtime = self.get_runtime(service_id)
        return (
            id(runtime),
            str(getattr(runtime.profile, "profile_id", "")),
            runtime.profile_generation,
            runtime.profile_registration_generation,
            runtime.profile_registry_generation_seen,
        )

    async def async_refresh_profile_entity_snapshot(self, service_id: str) -> None:
        """Exercise the production passive-callback cache boundary."""

        await NitradoAccountCoordinator._async_refresh_profile_entity_snapshot(
            self,
            self.get_runtime(service_id),
        )

    async def async_refresh_service(self, service_id: str) -> None:
        """Record refresh call."""

        self.calls.append(("refresh", service_id, {}))

    async def async_start_service(self, service_id: str, **kwargs) -> None:
        """Record start call."""

        self.calls.append(("start", service_id, kwargs))

    async def async_stop_service(self, service_id: str, **kwargs) -> None:
        """Record stop call."""

        self.calls.append(("stop", service_id, kwargs))

    async def async_cancel_pending_shutdown(self, service_id: str) -> None:
        """Record cancel call."""

        self.calls.append(("cancel_pending_shutdown", service_id, {}))

    def idle_shutdown_supported(self, service_id: str) -> bool:
        """Return whether idle-shutdown entities apply."""

        return self.idle_supported

    def idle_shutdown_configurable(self, service_id: str) -> bool:
        """Return whether idle-shutdown options apply to the selected profile."""

        return self.idle_configurable

    def idle_limit_minutes(self, service_id: str) -> float:
        """Return a stable idle limit for sensor tests."""

        return 15.0

    def auto_shutdown_status(self, service_id: str) -> str | None:
        """Return the configured semantic Auto Shutdown state."""

        return self.auto_shutdown_status_value

    def async_add_listener(self, listener) -> callable:
        """Record listener registrations."""

        self.listeners.append(listener)

        def remove() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return remove

    def _sync_profile_options(self, runtime: ServiceRuntime) -> None:
        """Expose the current profile namespace like the real coordinator."""

        profile_id = getattr(runtime.profile, "profile_id", None)
        values = {}
        if profile_id:
            for key, service_values in self.options.profile_options.get(profile_id, {}).items():
                if runtime.state.identity.service_id in service_values:
                    values[key] = service_values[runtime.state.identity.service_id]
        runtime.extra["_persisted_profile_options"] = values

    def revoke_pending_shutdown(self, service_id: str, reason: str) -> None:
        """Record conservative revocation after a profile option change."""

        self.calls.append(("revoke", service_id, {"reason": reason}))

    async def async_profile_options_changed(self, runtime: ServiceRuntime, reason: str) -> None:
        """Apply the profile option invalidation boundary used by entities."""

        self._sync_profile_options(runtime)
        self.revoke_pending_shutdown(runtime.state.identity.service_id, reason)
        await self.async_refresh_profile_entity_snapshot(runtime.state.identity.service_id)

    async def async_track_profile_mutation(self, service_id: str, operation):
        """Run one profile mutation like the real coordinator owner."""

        return await operation()

    @asynccontextmanager
    async def async_operation(self, service_id: str, intent):
        """Provide a minimal reservation for hosted-entity unit tests."""

        del service_id, intent

        class Reservation:
            async def async_mark_dispatched(self) -> None:
                return None

            async def async_mark_verifying(self) -> None:
                return None

        yield Reservation()


class FakeEntry:
    """Minimal config entry double for persisted entity tests."""

    def __init__(self) -> None:
        self.entry_id = "entry"
        self.options = {}


def profile_coordinator(runtime: ServiceRuntime) -> FakeCoordinator:
    """Build a coordinator whose passive profile cache has completed once."""

    coordinator = FakeCoordinator(runtime)
    asyncio.run(coordinator.async_refresh_profile_entity_snapshot(runtime.state.identity.service_id))
    return coordinator


class FakeConfigEntries:
    """Minimal config entry manager for hosted profile option tests."""

    @staticmethod
    def async_update_entry(entry: FakeEntry, *, options) -> None:
        entry.options = options


class FakeHass:
    """Minimal Home Assistant object for hosted profile option tests."""

    config_entries = FakeConfigEntries()

    def __init__(self) -> None:
        self.data = {}


class HostedOptionProfile(GenericProfile):
    """Profile fixture whose configuration state is persisted by core."""

    profile_id = "hosted_options"
    name = "Hosted Options"

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        return (
            EntityDeclaration(
                "switch",
                "feature_enabled",
                "Feature Enabled",
                attributes={"option_key": "feature_enabled"},
            ),
            EntityDeclaration(
                "number",
                "difficulty",
                "Difficulty",
                attributes={
                    "option_key": "difficulty",
                    "default_value": 2,
                    "native_min_value": 1,
                    "native_max_value": 10,
                },
            ),
            EntityDeclaration(
                "select",
                "world_slot",
                "World Slot",
                attributes={"option_key": "world_slot", "options": ("main", "event")},
            ),
        )


class ExtensibleProfile(GenericProfile):
    """Profile fixture that declares every profile-controlled HA entity type."""

    profile_id = "extensible"
    name = "Extensible"
    supported_games = ("extensible",)
    idle_shutdown_supported = False

    def matches(self, service, server=None):
        return MatchResult(True, confidence=1)

    async def _open_editor(self, context):
        return context.extra.setdefault("opened_editor", True)

    async def _turn_on(self, context):
        context.extra["feature_enabled"] = True

    async def _turn_off(self, context):
        context.extra["feature_enabled"] = False

    async def _set_difficulty(self, context, value):
        context.extra["difficulty"] = value

    async def _select_world(self, context, option):
        context.extra["world_slot"] = option

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        return (
            EntityDeclaration(
                "button",
                "open_editor",
                "Open Editor",
                action_fn=self._open_editor,
                attributes={"entity_category": "diagnostic", "entity_registry_enabled_default": False},
            ),
            EntityDeclaration(
                "switch",
                "feature_enabled",
                "Feature Enabled",
                value_fn=lambda context: bool(context.extra.get("feature_enabled")),
                turn_on_fn=self._turn_on,
                turn_off_fn=self._turn_off,
            ),
            EntityDeclaration(
                "number",
                "difficulty",
                "Difficulty",
                value_fn=lambda context: context.extra.get("difficulty", 1),
                set_value_fn=self._set_difficulty,
                attributes={"native_min_value": 1, "native_max_value": 10, "native_step": 1},
            ),
            EntityDeclaration(
                "select",
                "world_slot",
                "World Slot",
                value_fn=lambda context: context.extra.get("world_slot", "main"),
                options_fn=lambda context: tuple(context.extra.get("world_slots", ("main", "test"))),
                select_option_fn=self._select_world,
                attributes={"entity_category": "diagnostic", "entity_registry_enabled_default": False},
            ),
        )


class ExplodingProfile(GenericProfile):
    """Profile fixture whose HA entity callbacks fail."""

    profile_id = "exploding"
    name = "Exploding"
    supported_games = ("exploding",)

    def matches(self, service, server=None):
        return MatchResult(True, confidence=1)

    @staticmethod
    def _boom(*_args, **_kwargs):
        raise RuntimeError("profile callback exploded password=hunter2")

    @staticmethod
    async def _async_boom(*_args, **_kwargs):
        raise RuntimeError("profile callback exploded password=hunter2")

    def extra_entities(self) -> tuple[EntityDeclaration, ...]:
        return (
            EntityDeclaration(
                "sensor",
                "broken_sensor",
                "Broken Sensor",
                value_fn=self._boom,
                available_fn=self._boom,
            ),
            EntityDeclaration(
                "binary_sensor",
                "broken_binary",
                "Broken Binary",
                value_fn=self._boom,
            ),
            EntityDeclaration(
                "button",
                "broken_button",
                "Broken Button",
                action_fn=self._async_boom,
            ),
            EntityDeclaration(
                "switch",
                "broken_switch",
                "Broken Switch",
                value_fn=self._boom,
                turn_on_fn=self._async_boom,
                turn_off_fn=self._async_boom,
            ),
            EntityDeclaration(
                "number",
                "broken_number",
                "Broken Number",
                value_fn=lambda _context: "not-a-number",
                set_value_fn=self._async_boom,
            ),
            EntityDeclaration(
                "select",
                "broken_options",
                "Broken Options",
                value_fn=self._boom,
                options_fn=lambda _context: {"not": "a sequence"},
                select_option_fn=self._async_boom,
            ),
            EntityDeclaration(
                "select",
                "broken_select",
                "Broken Select",
                select_option_fn=self._async_boom,
                attributes={"options": ("main",)},
            ),
        )


class EntityDeclarationTests(unittest.TestCase):
    """Entity declaration tests."""

    def test_baseline_entity_keys_are_unique(self) -> None:
        keys = [entity.key for entity in core_service_entities()]

        self.assertEqual(len(keys), len(set(keys)))

    def test_entity_unique_ids_use_service_identity(self) -> None:
        runtime = runtime_fixture()

        self.assertEqual(SERVICE_SENSORS[0].unique_id(runtime), "service:123456:status")
        self.assertEqual(SERVICE_BUTTONS[1].unique_id(runtime), "service:123456:start")

    def test_profile_extra_entities_use_profile_scoped_unique_ids(self) -> None:
        runtime = runtime_fixture()
        sensors = {description.key: description for description in service_sensor_descriptions(runtime)}
        binary_sensors = {description.key: description for description in service_binary_sensor_descriptions(runtime)}

        self.assertIn("palworld_player_source", sensors)
        self.assertEqual(
            sensors["palworld_player_source"].unique_id(runtime),
            "service:123456:profile:palworld:palworld_player_source",
        )
        self.assertFalse(sensors["palworld_player_source"].entity_registry_enabled_default)
        self.assertNotIn("palworld_save_validation_available", binary_sensors)

    def test_profile_extra_controls_use_profile_scoped_unique_ids(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExtensibleProfile()
        buttons = {description.key: description for description in service_button_descriptions(runtime)}
        switches = {description.key: description for description in service_switch_descriptions(runtime)}
        numbers = {description.key: description for description in service_number_descriptions(runtime)}
        selects = {description.key: description for description in service_select_descriptions(runtime)}

        self.assertIn("extensible_open_editor", buttons)
        self.assertIn("extensible_feature_enabled", switches)
        self.assertIn("extensible_difficulty", numbers)
        self.assertIn("extensible_world_slot", selects)
        self.assertEqual(
            buttons["extensible_open_editor"].unique_id(runtime),
            "service:123456:profile:extensible:extensible_open_editor",
        )
        self.assertFalse(buttons["extensible_open_editor"].entity_registry_enabled_default)
        self.assertEqual(buttons["extensible_open_editor"].entity_category, "diagnostic")
        self.assertFalse(selects["extensible_world_slot"].entity_registry_enabled_default)
        self.assertEqual(selects["extensible_world_slot"].entity_category, "diagnostic")

    def test_invalid_profile_entity_manifest_is_omitted_from_entity_hosting(self) -> None:
        class InvalidProfile(ExtensibleProfile):
            def extra_entities(self) -> tuple[EntityDeclaration, ...]:
                return (
                    EntityDeclaration("sensor", "status", "Status"),
                    EntityDeclaration("sensor", "extensible_status", "Prefixed Status"),
                )

        runtime = runtime_fixture()
        runtime.profile = InvalidProfile()

        self.assertEqual(profile_service_entities(runtime), ())
        sensors = {description.key: description for description in service_sensor_descriptions(runtime)}
        self.assertNotIn("extensible_status", sensors)

    def test_sensor_values_read_runtime_snapshot(self) -> None:
        runtime = runtime_fixture()
        values = {entity.key: entity.value_fn(runtime) for entity in SERVICE_SENSORS if entity.value_fn}

        self.assertEqual(values["status"], "started")
        self.assertEqual(values["game"], "palworldxb")
        self.assertEqual(values["server_name"], "Example Server")
        self.assertEqual(values["player_count"], 2)
        self.assertEqual(values["online_players"], "Alex, Jordan")
        self.assertEqual(values["last_refresh_time"], datetime.fromtimestamp(1000, tz=UTC))

    def test_player_count_is_unknown_when_snapshot_has_no_valid_count(self) -> None:
        runtime = runtime_fixture()
        runtime.server = replace(runtime.server, player_count=None, player_names=(), query_valid=False)
        values = {entity.key: entity.value_fn(runtime) for entity in SERVICE_SENSORS if entity.value_fn}

        self.assertIsNone(values["player_count"])

    def test_player_count_is_unknown_when_status_is_stale_or_cached(self) -> None:
        runtime = runtime_fixture()
        player_count = next(description for description in SERVICE_SENSORS if description.key == "player_count")

        runtime.status_fresh = False
        self.assertIsNone(player_count.value_fn(runtime))
        self.assertFalse(NitradoServiceSensor(FakeCoordinator(runtime), "123456", player_count).available)

        runtime.status_fresh = True
        runtime.using_cached_data = True
        self.assertIsNone(player_count.value_fn(runtime))

    def test_player_count_preserves_explicit_trusted_zero(self) -> None:
        runtime = runtime_fixture()
        runtime.server = replace(runtime.server, player_count=0, player_names=(), query_valid=True)
        player_count = next(description for description in SERVICE_SENSORS if description.key == "player_count")

        self.assertEqual(player_count.value_fn(runtime), 0)
        self.assertTrue(NitradoServiceSensor(FakeCoordinator(runtime), "123456", player_count).available)

    def test_stopped_server_publishes_authoritative_zero_without_query_data(self) -> None:
        runtime = runtime_fixture()
        runtime.server = replace(
            runtime.server,
            raw_status="stopped",
            player_count=None,
            player_names=(),
            query_valid=False,
            player_source=None,
        )
        player_count = next(description for description in SERVICE_SENSORS if description.key == "player_count")
        online_players = next(description for description in SERVICE_SENSORS if description.key == "online_players")
        player_data_valid = next(
            description for description in SERVICE_BINARY_SENSORS if description.key == "player_data_valid"
        )

        self.assertEqual(player_count.value_fn(runtime), 0)
        self.assertTrue(NitradoServiceSensor(FakeCoordinator(runtime), "123456", player_count).available)
        self.assertEqual(online_players.value_fn(runtime), "None")
        self.assertTrue(player_data_valid.value_fn(runtime))

    def test_transitioning_server_does_not_publish_query_zero_or_complain(self) -> None:
        runtime = runtime_fixture()
        runtime.server = replace(
            runtime.server,
            raw_status="stopping",
            player_count=0,
            player_names=(),
            query_valid=True,
        )
        player_count = next(description for description in SERVICE_SENSORS if description.key == "player_count")
        player_data_valid = next(
            description for description in SERVICE_BINARY_SENSORS if description.key == "player_data_valid"
        )

        self.assertIsNone(player_count.value_fn(runtime))
        self.assertFalse(NitradoServiceSensor(FakeCoordinator(runtime), "123456", player_count).available)
        self.assertFalse(player_data_valid.value_fn(runtime))

    def test_malformed_negative_player_count_is_unknown(self) -> None:
        runtime = runtime_fixture()
        runtime.server = replace(runtime.server, player_count=-1, query_valid=True)
        player_count = next(description for description in SERVICE_SENSORS if description.key == "player_count")

        self.assertIsNone(player_count.value_fn(runtime))

    def test_player_count_stays_unknown_without_server_snapshot(self) -> None:
        runtime = ServiceRuntime(ManagedServiceState("123456"))
        values = {entity.key: entity.value_fn(runtime) for entity in SERVICE_SENSORS if entity.value_fn}

        self.assertIsNone(values["player_count"])

    def test_player_data_valid_is_explicit_for_automations(self) -> None:
        runtime = runtime_fixture()
        valid = next(description for description in SERVICE_BINARY_SENSORS if description.key == "player_data_valid")

        self.assertTrue(valid.value_fn(runtime))
        runtime.status_fresh = False
        self.assertFalse(valid.value_fn(runtime))

    def test_idle_timer_sensors_are_zero_until_countdown_is_armed(self) -> None:
        runtime = runtime_fixture()
        idle_minutes = next(description for description in SERVICE_SENSORS if description.key == "idle_minutes")
        idle_remaining = next(
            description for description in SERVICE_SENSORS if description.key == "idle_time_remaining"
        )
        coordinator = FakeCoordinator(runtime)

        self.assertEqual(idle_minutes.value_fn(runtime), 0.0)
        self.assertEqual(NitradoServiceSensor(coordinator, "123456", idle_remaining).native_value, 0.0)

        runtime.idle_started_at = 900
        self.assertEqual(idle_minutes.value_fn(runtime), 1.7)
        self.assertEqual(NitradoServiceSensor(coordinator, "123456", idle_remaining).native_value, 13.3)

    def test_auto_shutdown_status_sensor_uses_semantic_coordinator_state(self) -> None:
        runtime = runtime_fixture()
        description = next(description for description in SERVICE_SENSORS if description.key == "auto_shutdown_status")

        known = NitradoServiceSensor(
            FakeCoordinator(runtime, auto_shutdown_status="Inactive — server stopped"),
            "123456",
            description,
        )
        unavailable = NitradoServiceSensor(
            FakeCoordinator(runtime, auto_shutdown_status=None),
            "123456",
            description,
        )

        self.assertTrue(known.available)
        self.assertEqual(known.native_value, "Inactive — server stopped")
        self.assertFalse(unavailable.available)

    def test_binary_values_read_runtime_snapshot(self) -> None:
        runtime = runtime_fixture()
        runtime.last_start_verdict = SUPPORTED
        runtime.last_stop_verdict = blocked("Not running.", overridable=True)
        values = {entity.key: entity.value_fn(runtime) for entity in SERVICE_BINARY_SENSORS if entity.value_fn}

        self.assertTrue(values["running"])
        self.assertFalse(values["transitioning"])
        self.assertTrue(values["status_fresh"])
        self.assertFalse(values["using_cached_data"])
        self.assertTrue(values["can_start"])
        self.assertFalse(values["can_stop"])

    def test_block_reason_values_read_cached_verdicts(self) -> None:
        runtime = runtime_fixture()
        runtime.last_start_verdict = blocked("Still settling.", overridable=True)
        runtime.last_stop_verdict = SUPPORTED
        values = {entity.key: entity.value_fn(runtime) for entity in SERVICE_SENSORS if entity.value_fn}

        self.assertEqual(values["start_block_reason"], "Still settling.")
        self.assertEqual(values["stop_block_reason"], "")

    def test_ha_sensor_wrapper_reads_runtime(self) -> None:
        runtime = runtime_fixture()
        entity = NitradoServiceSensor(FakeCoordinator(runtime), "123456", SERVICE_SENSORS[0])

        self.assertEqual(entity.native_value, "started")
        self.assertEqual(entity._attr_unique_id, "service:123456:status")
        self.assertEqual(entity.device_info["identifiers"], {("nitrado_gameserver", "service:123456")})
        self.assertEqual(
            entity.device_info["configuration_url"],
            "homeassistant://nitrado-game-servers/entry/123456",
        )

    def test_timestamp_sensors_have_timestamp_device_class(self) -> None:
        descriptions = {description.key: description for description in SERVICE_SENSORS}

        self.assertEqual(descriptions["last_refresh_time"].device_class, "timestamp")
        self.assertEqual(descriptions["last_transition_time"].device_class, "timestamp")

    def test_server_name_sensor_uses_clean_display_name(self) -> None:
        runtime = runtime_fixture()
        runtime.server = replace(runtime.server, server_name="ni")
        entity = NitradoServiceSensor(FakeCoordinator(runtime), "123456", SERVICE_SENSORS[2])

        self.assertEqual(entity.native_value, "Palworld Xbox")

    def test_idle_shutdown_sensors_are_unavailable_when_profile_does_not_support_idle_shutdown(self) -> None:
        runtime = runtime_fixture()
        idle_minutes = next(description for description in SERVICE_SENSORS if description.key == "idle_minutes")
        idle_remaining = next(
            description for description in SERVICE_SENSORS if description.key == "idle_time_remaining"
        )

        self.assertFalse(
            NitradoServiceSensor(FakeCoordinator(runtime, idle_supported=False), "123456", idle_minutes).available
        )
        self.assertFalse(
            NitradoServiceSensor(FakeCoordinator(runtime, idle_supported=False), "123456", idle_remaining).available
        )
        self.assertTrue(
            NitradoServiceSensor(FakeCoordinator(runtime, idle_supported=True), "123456", idle_minutes).available
        )

    def test_idle_shutdown_controls_remain_configurable_while_current_capability_is_blocked(self) -> None:
        runtime = runtime_fixture()
        coordinator = FakeCoordinator(runtime, idle_supported=False, idle_configurable=True)
        auto_shutdown = next(description for description in SERVICE_SWITCHES if description.key == "auto_shutdown")
        idle_minutes = next(
            description
            for description in service_number_descriptions(runtime)
            if description.key == "idle_shutdown_minutes"
        )

        self.assertTrue(NitradoServiceSwitch(coordinator, FakeEntry(), "123456", auto_shutdown).available)
        self.assertTrue(NitradoServiceNumber(coordinator, FakeEntry(), "123456", idle_minutes).available)

    def test_ha_binary_sensor_wrapper_reads_runtime(self) -> None:
        runtime = runtime_fixture()
        entity = NitradoServiceBinarySensor(FakeCoordinator(runtime), "123456", SERVICE_BINARY_SENSORS[0])

        self.assertTrue(entity.is_on)

    def test_shutdown_pending_binary_sensor_is_unavailable_when_profile_does_not_support_idle_shutdown(self) -> None:
        runtime = runtime_fixture()
        shutdown_pending = next(
            description for description in SERVICE_BINARY_SENSORS if description.key == "shutdown_pending"
        )

        self.assertFalse(
            NitradoServiceBinarySensor(
                FakeCoordinator(runtime, idle_supported=False), "123456", shutdown_pending
            ).available
        )
        self.assertTrue(
            NitradoServiceBinarySensor(
                FakeCoordinator(runtime, idle_supported=True), "123456", shutdown_pending
            ).available
        )

    def test_ha_button_wrapper_has_stable_unique_id(self) -> None:
        runtime = runtime_fixture()
        entity = NitradoServiceButton(FakeCoordinator(runtime), "123456", SERVICE_BUTTONS[1])

        self.assertEqual(entity._attr_unique_id, "service:123456:start")

    def test_ha_button_availability_requires_fresh_status_and_allowed_verdict(self) -> None:
        runtime = runtime_fixture()
        start = NitradoServiceButton(FakeCoordinator(runtime), "123456", SERVICE_BUTTONS[1])
        runtime.last_start_verdict = blocked("Still settling.", overridable=True)

        self.assertFalse(start.available)

        runtime.last_start_verdict = SUPPORTED

        self.assertTrue(start.available)

        runtime.status_fresh = False
        self.assertFalse(start.available)

        runtime.status_fresh = True
        runtime.using_cached_data = True
        self.assertFalse(start.available)

    def test_live_block_reason_sensors_are_unavailable_when_status_is_stale(self) -> None:
        runtime = runtime_fixture()
        descriptions = {description.key: description for description in SERVICE_SENSORS}
        start_reason = NitradoServiceSensor(FakeCoordinator(runtime), "123456", descriptions["start_block_reason"])
        stop_reason = NitradoServiceSensor(FakeCoordinator(runtime), "123456", descriptions["stop_block_reason"])

        self.assertTrue(start_reason.available)
        self.assertTrue(stop_reason.available)
        runtime.status_fresh = False
        self.assertFalse(start_reason.available)
        self.assertFalse(stop_reason.available)

    def test_force_controls_are_not_entities(self) -> None:
        descriptions = {description.key: description for description in SERVICE_BUTTONS}

        self.assertNotIn("force_start", descriptions)
        self.assertNotIn("force_stop", descriptions)

    def test_idle_shutdown_switches_use_public_safe_labels(self) -> None:
        descriptions = {description.key: description for description in SERVICE_SWITCHES}

        self.assertEqual(descriptions["maintenance_mode"].display_name, "Pause Auto Shutdown")
        self.assertEqual(descriptions["maintenance_mode"].entity_category, "diagnostic")
        self.assertFalse(descriptions["maintenance_mode"].entity_registry_enabled_default)

    def test_player_reporting_verified_is_not_an_entity(self) -> None:
        descriptions = {description.key: description for description in SERVICE_SWITCHES}

        self.assertNotIn("player_reporting_verified", descriptions)

    def test_shutdown_dry_run_is_disabled_diagnostic_by_default(self) -> None:
        descriptions = {description.key: description for description in SERVICE_SWITCHES}
        dry_run = descriptions["shutdown_dry_run"]

        self.assertEqual(dry_run.display_name, "Auto Shutdown Dry Run")
        self.assertEqual(dry_run.entity_category, "diagnostic")
        self.assertFalse(dry_run.entity_registry_enabled_default)

    def test_last_shutdown_reason_restores_previous_state(self) -> None:
        runtime = runtime_fixture()
        runtime.last_shutdown_reason = "Never"
        description = next(description for description in SERVICE_SENSORS if description.key == "last_shutdown_reason")

        class RestoringSensor(NitradoServiceSensor):
            async def async_get_last_state(self):
                return type("State", (), {"state": "zero players for configured idle period"})()

            def async_on_remove(self, _callback) -> None:
                return None

            def async_write_ha_state(self) -> None:
                return None

        entity = RestoringSensor(FakeCoordinator(runtime), "123456", description)

        asyncio.run(entity.async_added_to_hass())

        self.assertEqual(runtime.last_shutdown_reason, "zero players for configured idle period")

    def test_shutdown_history_remains_available_when_execution_is_temporarily_unsupported(self) -> None:
        runtime = runtime_fixture()
        runtime.last_shutdown_reason = "zero players for configured idle period"
        descriptions = {description.key: description for description in SERVICE_SENSORS}
        coordinator = FakeCoordinator(runtime, idle_supported=False, idle_configurable=True)

        history = NitradoServiceSensor(coordinator, "123456", descriptions["last_shutdown_reason"])
        live_timer = NitradoServiceSensor(coordinator, "123456", descriptions["idle_minutes"])

        self.assertTrue(history.available)
        self.assertEqual(history.native_value, "zero players for configured idle period")
        self.assertFalse(live_timer.available)

    def test_cancel_pending_shutdown_button_is_only_available_when_pending(self) -> None:
        runtime = runtime_fixture()
        cancel = next(description for description in SERVICE_BUTTONS if description.key == "cancel_pending_shutdown")
        entity = NitradoServiceButton(FakeCoordinator(runtime), "123456", cancel)

        self.assertFalse(entity.available)

        runtime.shutdown_pending = True

        self.assertTrue(entity.available)

    def test_cancel_pending_shutdown_button_is_unavailable_when_profile_does_not_support_idle_shutdown(self) -> None:
        runtime = runtime_fixture()
        runtime.shutdown_pending = True
        cancel = next(description for description in SERVICE_BUTTONS if description.key == "cancel_pending_shutdown")
        entity = NitradoServiceButton(FakeCoordinator(runtime, idle_supported=False), "123456", cancel)

        self.assertFalse(entity.available)

    def test_profile_declared_button_dispatches_profile_action(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExtensibleProfile()
        description = next(
            description
            for description in service_button_descriptions(runtime)
            if description.key == "extensible_open_editor"
        )

        asyncio.run(NitradoServiceButton(FakeCoordinator(runtime), "123456", description).async_press())

        self.assertTrue(runtime.profile_extra()["opened_editor"])

    def test_profile_declared_switch_dispatches_profile_handlers(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExtensibleProfile()
        description = next(
            description
            for description in service_switch_descriptions(runtime)
            if description.key == "extensible_feature_enabled"
        )
        entity = NitradoServiceSwitch(profile_coordinator(runtime), FakeEntry(), "123456", description)

        self.assertFalse(entity.is_on)
        asyncio.run(entity.async_turn_on())
        self.assertTrue(entity.is_on)

        asyncio.run(entity.async_turn_off())
        self.assertFalse(entity.is_on)

    def test_profile_entity_properties_read_cache_without_invoking_callback(self) -> None:
        calls = 0

        class CountingProfile(ExtensibleProfile):
            profile_id = "counting"

            def extra_entities(self):
                def value(_context):
                    nonlocal calls
                    calls += 1
                    return 7

                return (EntityDeclaration("sensor", "count", "Count", value_fn=value),)

        runtime = runtime_fixture()
        runtime.profile = CountingProfile()
        description = next(item for item in service_sensor_descriptions(runtime) if item.key == "counting_count")
        entity = NitradoServiceSensor(profile_coordinator(runtime), "123456", description)

        self.assertEqual(calls, 1)
        self.assertEqual(entity.native_value, 7)
        self.assertEqual(entity.native_value, 7)
        self.assertEqual(calls, 1)

    def test_profile_boolean_entities_do_not_coerce_strings_to_true(self) -> None:
        class MalformedBooleanProfile(ExtensibleProfile):
            profile_id = "malformed_boolean"

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        platform="binary_sensor",
                        key="reported",
                        name="Reported",
                        value_fn=lambda context: "false",
                    ),
                    EntityDeclaration(
                        platform="switch",
                        key="enabled",
                        name="Enabled",
                        value_fn=lambda context: "false",
                    ),
                )

        runtime = runtime_fixture()
        runtime.profile = MalformedBooleanProfile()
        binary_description = next(
            item for item in service_binary_sensor_descriptions(runtime) if item.key == "malformed_boolean_reported"
        )
        switch_description = next(
            item for item in service_switch_descriptions(runtime) if item.key == "malformed_boolean_enabled"
        )
        coordinator = profile_coordinator(runtime)
        binary = NitradoServiceBinarySensor(coordinator, "123456", binary_description)
        switch = NitradoServiceSwitch(coordinator, FakeEntry(), "123456", switch_description)

        self.assertIsNone(binary.is_on)
        self.assertFalse(switch.is_on)
        self.assertIn("malformed_boolean_reported", runtime.last_profile_entity_errors)
        self.assertIn("malformed_boolean_enabled", runtime.last_profile_entity_errors)

    def test_profile_entity_cache_rejects_objects_before_ha_properties_touch_them(self) -> None:
        touched = False

        class HostileValue:
            def __bool__(self):
                nonlocal touched
                touched = True
                raise RuntimeError("event-loop property evaluation was reached")

            def __str__(self):
                nonlocal touched
                touched = True
                raise RuntimeError("event-loop string conversion was reached")

        class HostileValueProfile(ExtensibleProfile):
            profile_id = "hostile_value"

            def extra_entities(self):
                return (
                    EntityDeclaration(
                        platform="binary_sensor",
                        key="reported",
                        name="Reported",
                        value_fn=lambda _context: HostileValue(),
                    ),
                )

        runtime = runtime_fixture()
        runtime.profile = HostileValueProfile()
        description = next(
            item for item in service_binary_sensor_descriptions(runtime) if item.key == "hostile_value_reported"
        )
        entity = NitradoServiceBinarySensor(profile_coordinator(runtime), "123456", description)

        self.assertIsNone(entity.is_on)
        self.assertFalse(touched)
        self.assertIn("hostile_value_reported", runtime.last_profile_entity_errors)

    def test_profile_declared_number_dispatches_profile_setter(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExtensibleProfile()
        description = next(
            description
            for description in service_number_descriptions(runtime)
            if description.key == "extensible_difficulty"
        )
        entity = NitradoServiceNumber(profile_coordinator(runtime), FakeEntry(), "123456", description)

        self.assertEqual(entity.native_value, 1)

        asyncio.run(entity.async_set_native_value(12))

        self.assertEqual(entity.native_value, 10)

    def test_profile_declared_select_dispatches_profile_handler(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExtensibleProfile()
        runtime.profile_extra()["world_slots"] = ("main", "event")
        description = next(
            description
            for description in service_select_descriptions(runtime)
            if description.key == "extensible_world_slot"
        )
        entity = NitradoServiceSelect(profile_coordinator(runtime), FakeEntry(), "123456", description)

        self.assertEqual(entity.options, ["main", "event"])
        self.assertEqual(entity.current_option, "main")
        self.assertFalse(entity._attr_entity_registry_enabled_default)

        asyncio.run(entity.async_select_option("event"))

        self.assertEqual(entity.current_option, "event")

    def test_profile_entity_read_callback_failures_degrade_and_record(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExplodingProfile()
        sensors = {description.key: description for description in service_sensor_descriptions(runtime)}
        binary_sensors = {description.key: description for description in service_binary_sensor_descriptions(runtime)}
        switches = {description.key: description for description in service_switch_descriptions(runtime)}
        numbers = {description.key: description for description in service_number_descriptions(runtime)}
        selects = {description.key: description for description in service_select_descriptions(runtime)}

        coordinator = profile_coordinator(runtime)
        sensor = NitradoServiceSensor(coordinator, "123456", sensors["exploding_broken_sensor"])
        binary = NitradoServiceBinarySensor(coordinator, "123456", binary_sensors["exploding_broken_binary"])
        switch = NitradoServiceSwitch(coordinator, FakeEntry(), "123456", switches["exploding_broken_switch"])
        number = NitradoServiceNumber(coordinator, FakeEntry(), "123456", numbers["exploding_broken_number"])
        select = NitradoServiceSelect(coordinator, FakeEntry(), "123456", selects["exploding_broken_options"])

        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.assertIsNone(binary.is_on)
        self.assertFalse(switch.is_on)
        self.assertIsNone(number.native_value)
        self.assertEqual(select.options, [])
        self.assertIsNone(select.current_option)

        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_sensor"]["phase"], "value")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_binary"]["phase"], "value")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_switch"]["phase"], "value")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_number"]["phase"], "value")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_options"]["phase"], "options")

    def test_synthetic_profile_entity_errors_do_not_log_fake_tracebacks(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExplodingProfile()
        selects = {description.key: description for description in service_select_descriptions(runtime)}
        with self.assertLogs("custom_components.nitrado_gameserver.coordinator", level="WARNING") as logs:
            coordinator = profile_coordinator(runtime)

        select = NitradoServiceSelect(coordinator, FakeEntry(), "123456", selects["exploding_broken_options"])
        self.assertEqual(select.options, [])

        output = "\n".join(logs.output)
        self.assertIn("Profile boundary failure", output)
        self.assertNotIn("NoneType: None", output)

    def test_profile_entity_mutation_callback_failures_raise_and_record(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = ExplodingProfile()
        buttons = {description.key: description for description in service_button_descriptions(runtime)}
        switches = {description.key: description for description in service_switch_descriptions(runtime)}
        numbers = {description.key: description for description in service_number_descriptions(runtime)}
        selects = {description.key: description for description in service_select_descriptions(runtime)}

        errors: list[Exception] = []
        operations = (
            NitradoServiceButton(FakeCoordinator(runtime), "123456", buttons["exploding_broken_button"]).async_press(),
            NitradoServiceSwitch(
                FakeCoordinator(runtime), FakeEntry(), "123456", switches["exploding_broken_switch"]
            ).async_turn_on(),
            NitradoServiceNumber(
                FakeCoordinator(runtime), FakeEntry(), "123456", numbers["exploding_broken_number"]
            ).async_set_native_value(5),
            NitradoServiceSelect(
                FakeCoordinator(runtime), FakeEntry(), "123456", selects["exploding_broken_select"]
            ).async_select_option("main"),
        )
        with self.assertLogs("custom_components.nitrado_gameserver.entity", level="WARNING") as logs:
            for operation in operations:
                with self.assertRaises(Exception) as caught:
                    asyncio.run(operation)
                errors.append(caught.exception)

        for error in errors:
            self.assertIn("details were logged", str(error))
            self.assertNotIn("hunter2", str(error))
        self.assertNotIn("hunter2", "\n".join(logs.output))

        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_button"]["phase"], "press")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_switch"]["phase"], "turn_on")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_number"]["phase"], "set_value")
        self.assertEqual(runtime.last_profile_entity_errors["exploding_broken_select"]["phase"], "select_option")

    def test_no_core_selects_exist_until_profiles_declare_them(self) -> None:
        self.assertEqual(SERVICE_SELECTS, ())

    def test_profile_hosted_options_persist_and_rehydrate_all_control_types(self) -> None:
        runtime = runtime_fixture()
        runtime.profile = HostedOptionProfile()
        coordinator = FakeCoordinator(runtime)
        entry = FakeEntry()
        descriptions = {description.key: description for description in profile_service_entities(runtime)}

        switch = NitradoServiceSwitch(coordinator, entry, "123456", descriptions["hosted_options_feature_enabled"])
        number = NitradoServiceNumber(coordinator, entry, "123456", descriptions["hosted_options_difficulty"])
        select = NitradoServiceSelect(coordinator, entry, "123456", descriptions["hosted_options_world_slot"])
        for entity in (switch, number, select):
            entity.hass = FakeHass()

        self.assertFalse(switch.is_on)
        self.assertEqual(number.native_value, 2)
        self.assertEqual(select.current_option, "main")

        asyncio.run(switch.async_turn_on())
        asyncio.run(number.async_set_native_value(7))
        asyncio.run(select.async_select_option("event"))

        self.assertTrue(switch.is_on)
        self.assertEqual(number.native_value, 7)
        self.assertEqual(select.current_option, "event")
        self.assertEqual(
            entry.options["profile_options"]["hosted_options"],
            {
                "feature_enabled": {"123456": True},
                "difficulty": {"123456": 7.0},
                "world_slot": {"123456": "event"},
            },
        )
        self.assertEqual(
            runtime.extra["_persisted_profile_options"],
            {"feature_enabled": True, "difficulty": 7.0, "world_slot": "event"},
        )


if __name__ == "__main__":
    unittest.main()
