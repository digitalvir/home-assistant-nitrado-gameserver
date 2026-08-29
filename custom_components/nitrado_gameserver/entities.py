"""Pure entity declarations for Nitrado Game Server."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    from homeassistant.components.binary_sensor import BinarySensorEntityDescription
    from homeassistant.components.button import ButtonEntityDescription
    from homeassistant.components.number import NumberEntityDescription
    from homeassistant.components.select import SelectEntityDescription
    from homeassistant.components.sensor import (
        SensorDeviceClass,
        SensorEntityDescription,
    )
    from homeassistant.components.switch import SwitchEntityDescription
    from homeassistant.const import EntityCategory
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class EntityCategory:  # type: ignore[no-redef]
        """Fallback HA entity categories for local tests."""

        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    class SensorDeviceClass:  # type: ignore[no-redef]
        """Fallback HA sensor device classes for local tests."""

        TIMESTAMP = "timestamp"

    @dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:  # type: ignore[no-redef]
        """Fallback HA sensor entity description for local tests."""

        key: str
        device_class: Any | None = None
        entity_category: Any | None = None
        icon: str | None = None

    @dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:  # type: ignore[no-redef]
        """Fallback HA binary sensor entity description for local tests."""

        key: str
        entity_category: Any | None = None
        icon: str | None = None

    @dataclass(frozen=True, kw_only=True)
    class ButtonEntityDescription:  # type: ignore[no-redef]
        """Fallback HA button entity description for local tests."""

        key: str
        entity_category: Any | None = None
        entity_registry_enabled_default: bool = True
        icon: str | None = None

    @dataclass(frozen=True, kw_only=True)
    class SwitchEntityDescription:  # type: ignore[no-redef]
        """Fallback HA switch entity description for local tests."""

        key: str
        entity_category: Any | None = None
        entity_registry_enabled_default: bool = True
        icon: str | None = None

    @dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:  # type: ignore[no-redef]
        """Fallback HA number entity description for local tests."""

        key: str
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        native_unit_of_measurement: str | None = None
        entity_category: Any | None = None
        icon: str | None = None

    @dataclass(frozen=True, kw_only=True)
    class SelectEntityDescription:  # type: ignore[no-redef]
        """Fallback HA select entity description for local tests."""

        key: str
        entity_category: Any | None = None
        entity_registry_enabled_default: bool = True
        icon: str | None = None


from .const import (
    CONF_DRY_RUN_SERVICE_IDS,
    CONF_IDLE_MINUTES,
    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
    CONF_MAINTENANCE_SERVICE_IDS,
    CONF_STARTUP_COOLDOWN_MINUTES,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_STARTUP_COOLDOWN_MINUTES,
    RUNNING_STATUS,
    STOPPED_STATUS,
    TRANSITION_STATUSES,
)
from .plugins.base import (
    EntityDeclaration,
    ProfileManifestError,
    normalized_profile_entity_key,
)
from .runtime import ServiceRuntime, runtime_profile_extension_manifest

_LOGGER = logging.getLogger(__name__)

ValueFn = Callable[[ServiceRuntime], Any]


@dataclass(frozen=True, kw_only=True)
class CoreSensorDescription(SensorEntityDescription):
    """Sensor description declared by core/profile logic."""

    platform: str
    display_name: str
    native_unit_of_measurement: str | None = None
    entity_registry_enabled_default: bool = True
    profile_id: str | None = None
    value_fn: ValueFn | None = None
    available_fn: ValueFn | None = None

    def unique_id(self, runtime: ServiceRuntime) -> str:
        """Return stable unique ID for this entity."""

        return runtime.state.identity.entity_unique_id(self.key, profile_id=self.profile_id)


@dataclass(frozen=True, kw_only=True)
class CoreBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor description declared by core/profile logic."""

    platform: str
    display_name: str
    entity_registry_enabled_default: bool = True
    profile_id: str | None = None
    value_fn: ValueFn | None = None
    available_fn: ValueFn | None = None

    def unique_id(self, runtime: ServiceRuntime) -> str:
        """Return stable unique ID for this entity."""

        return runtime.state.identity.entity_unique_id(self.key, profile_id=self.profile_id)


@dataclass(frozen=True, kw_only=True)
class CoreButtonDescription(ButtonEntityDescription):
    """Button description declared by core/profile logic."""

    platform: str
    display_name: str
    profile_id: str | None = None
    value_fn: ValueFn | None = None
    available_fn: ValueFn | None = None
    action_fn: Callable[[Any], Any] | None = None

    def unique_id(self, runtime: ServiceRuntime) -> str:
        """Return stable unique ID for this entity."""

        return runtime.state.identity.entity_unique_id(self.key, profile_id=self.profile_id)


@dataclass(frozen=True, kw_only=True)
class CoreSwitchDescription(SwitchEntityDescription):
    """Switch description declared by core/profile logic."""

    platform: str
    display_name: str
    option_key: str | None = None
    profile_id: str | None = None
    value_fn: ValueFn | None = None
    available_fn: ValueFn | None = None
    turn_on_fn: Callable[[Any], Any] | None = None
    turn_off_fn: Callable[[Any], Any] | None = None

    def unique_id(self, runtime: ServiceRuntime) -> str:
        """Return stable unique ID for this entity."""

        return runtime.state.identity.entity_unique_id(self.key, profile_id=self.profile_id)


@dataclass(frozen=True, kw_only=True)
class CoreNumberDescription(NumberEntityDescription):
    """Number description declared by core/profile logic."""

    platform: str
    display_name: str
    option_key: str | None = None
    default_value: float = 0
    entity_registry_enabled_default: bool = True
    profile_id: str | None = None
    value_fn: ValueFn | None = None
    available_fn: ValueFn | None = None
    set_value_fn: Callable[[Any, float], Any] | None = None

    def unique_id(self, runtime: ServiceRuntime) -> str:
        """Return stable unique ID for this entity."""

        return runtime.state.identity.entity_unique_id(self.key, profile_id=self.profile_id)


@dataclass(frozen=True, kw_only=True)
class CoreSelectDescription(SelectEntityDescription):
    """Select description declared by core/profile logic."""

    platform: str
    display_name: str
    options: tuple[str, ...] = ()
    option_key: str | None = None
    entity_registry_enabled_default: bool = True
    profile_id: str | None = None
    value_fn: ValueFn | None = None
    available_fn: ValueFn | None = None
    options_fn: Callable[[ServiceRuntime], tuple[str, ...] | list[str]] | None = None
    select_option_fn: Callable[[Any, str], Any] | None = None

    def unique_id(self, runtime: ServiceRuntime) -> str:
        """Return stable unique ID for this entity."""

        return runtime.state.identity.entity_unique_id(self.key, profile_id=self.profile_id)


CoreEntityDescription = (
    CoreSensorDescription
    | CoreBinarySensorDescription
    | CoreButtonDescription
    | CoreSwitchDescription
    | CoreNumberDescription
    | CoreSelectDescription
)


def _epoch_to_datetime(value: int | None) -> datetime | None:
    """Return a timezone-aware datetime for Home Assistant timestamp sensors."""

    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _player_count_value(runtime: ServiceRuntime) -> int | None:
    """Return a known player count without manufacturing a running-server zero."""

    if not _player_data_valid(runtime):
        return None
    if runtime.server.raw_status == STOPPED_STATUS:
        return 0
    return runtime.server.player_count


def _player_data_valid(runtime: ServiceRuntime) -> bool:
    """Return whether the effective player count is currently known.

    A fresh stable stopped status is authoritative proof of zero connected
    players even though no game-native query endpoint is available.  A running
    server still requires an explicit profile-approved count. Transitional,
    stale, and cached snapshots remain unknown.
    """

    return bool(
        runtime.server is not None
        and runtime.status_fresh
        and not runtime.using_cached_data
        and (
            runtime.server.raw_status == STOPPED_STATUS
            or (
                runtime.server.raw_status == RUNNING_STATUS
                and runtime.server.query_valid
                and runtime.server.player_count is not None
                and runtime.server.player_count >= 0
            )
        )
    )


def _fresh_status(runtime: ServiceRuntime) -> bool:
    """Return whether live server status is present and non-cached."""

    return runtime.server is not None and runtime.status_fresh and not runtime.using_cached_data


def _online_players_value(runtime: ServiceRuntime) -> str | None:
    """Return player names without disguising missing data as an empty server."""

    if not _player_data_valid(runtime):
        return None
    if runtime.server.raw_status == STOPPED_STATUS:
        return "None"
    if runtime.server.player_names:
        return ", ".join(runtime.server.player_names)
    if runtime.server.player_count == 0:
        return "None"
    return None


def _idle_minutes_value(runtime: ServiceRuntime) -> float | None:
    """Return elapsed idle time, using zero when no countdown exists."""

    return round(runtime.idle_minutes(runtime.refreshed_at or 0), 1)


SERVICE_SENSORS: tuple[CoreSensorDescription, ...] = (
    CoreSensorDescription(
        platform="sensor",
        key="status",
        display_name="Status",
        value_fn=lambda r: r.server.raw_status if r.server else None,
        available_fn=_fresh_status,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="game",
        display_name="Game",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.server.game_short if r.server else r.service.game if r.service else None,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="server_name",
        display_name="Server Name",
        value_fn=lambda r: r.server.server_name if r.server else r.service.name if r.service else None,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="address",
        display_name="Address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.server.address if r.server else None,
        available_fn=_fresh_status,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="player_count",
        display_name="Player Count",
        value_fn=_player_count_value,
        available_fn=_player_data_valid,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="player_max",
        display_name="Player Max",
        value_fn=lambda r: r.server.player_max if r.server else None,
        available_fn=_fresh_status,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="online_players",
        display_name="Online Players",
        value_fn=_online_players_value,
        available_fn=_player_data_valid,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="last_transition_time",
        display_name="Last Transition Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: _epoch_to_datetime(r.last_transition_at),
    ),
    CoreSensorDescription(
        platform="sensor",
        key="last_refresh_time",
        display_name="Last Refresh Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: _epoch_to_datetime(r.refreshed_at),
    ),
    CoreSensorDescription(
        platform="sensor",
        key="idle_minutes",
        display_name="Idle Minutes",
        native_unit_of_measurement="min",
        value_fn=_idle_minutes_value,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="idle_time_remaining",
        display_name="Idle Time Remaining",
        native_unit_of_measurement="min",
        value_fn=lambda r: round(r.idle_remaining_minutes(r.refreshed_at or 0, DEFAULT_IDLE_MINUTES), 1),
    ),
    CoreSensorDescription(
        platform="sensor",
        key="auto_shutdown_status",
        display_name="Auto Shutdown Status",
    ),
    CoreSensorDescription(
        platform="sensor",
        key="last_start_time",
        display_name="Last Start Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: _epoch_to_datetime(r.last_start_time),
    ),
    CoreSensorDescription(
        platform="sensor",
        key="last_stop_time",
        display_name="Last Stop Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: _epoch_to_datetime(r.last_stop_time),
    ),
    CoreSensorDescription(
        platform="sensor",
        key="last_shutdown_reason",
        display_name="Last Shutdown Reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.last_shutdown_reason,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="last_reset_reason",
        display_name="Last Reset Reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.last_reset_reason,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="start_block_reason",
        display_name="Start Block Reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: (
            r.last_start_verdict.reason if r.last_start_verdict and not r.last_start_verdict.allowed else ""
        ),
        available_fn=_fresh_status,
    ),
    CoreSensorDescription(
        platform="sensor",
        key="stop_block_reason",
        display_name="Stop Block Reason",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: (
            r.last_stop_verdict.reason if r.last_stop_verdict and not r.last_stop_verdict.allowed else ""
        ),
        available_fn=_fresh_status,
    ),
)

SERVICE_BINARY_SENSORS: tuple[CoreBinarySensorDescription, ...] = (
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="running",
        display_name="Running",
        value_fn=lambda r: bool(r.server and r.server.raw_status == RUNNING_STATUS),
        available_fn=_fresh_status,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="player_data_valid",
        display_name="Player Data Valid",
        value_fn=_player_data_valid,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="transitioning",
        display_name="Transitioning",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: bool(r.server and r.server.raw_status in TRANSITION_STATUSES),
        available_fn=_fresh_status,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="status_fresh",
        display_name="Status Fresh",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.status_fresh,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="using_cached_data",
        display_name="Using Cached Data",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.using_cached_data,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="can_start",
        display_name="Can Start",
        value_fn=lambda r: bool(r.last_start_verdict and r.last_start_verdict.allowed),
        available_fn=_fresh_status,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="can_stop",
        display_name="Can Stop",
        value_fn=lambda r: bool(r.last_stop_verdict and r.last_stop_verdict.allowed),
        available_fn=_fresh_status,
    ),
    CoreBinarySensorDescription(
        platform="binary_sensor",
        key="shutdown_pending",
        display_name="Shutdown Pending",
        value_fn=lambda r: r.shutdown_pending,
    ),
)

SERVICE_BUTTONS: tuple[CoreButtonDescription, ...] = (
    CoreButtonDescription(platform="button", key="refresh", display_name="Refresh", icon="mdi:refresh"),
    CoreButtonDescription(platform="button", key="start", display_name="Start", icon="mdi:play"),
    CoreButtonDescription(platform="button", key="stop", display_name="Stop", icon="mdi:stop"),
    CoreButtonDescription(
        platform="button", key="cancel_pending_shutdown", display_name="Cancel Pending Shutdown", icon="mdi:cancel"
    ),
)

SERVICE_SWITCHES: tuple[CoreSwitchDescription, ...] = (
    CoreSwitchDescription(
        platform="switch",
        key="auto_shutdown",
        display_name="Auto Shutdown",
        option_key=CONF_IDLE_SHUTDOWN_SERVICE_IDS,
        icon="mdi:power-sleep",
    ),
    CoreSwitchDescription(
        platform="switch",
        key="maintenance_mode",
        display_name="Pause Auto Shutdown",
        option_key=CONF_MAINTENANCE_SERVICE_IDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:pause-circle-outline",
    ),
    CoreSwitchDescription(
        platform="switch",
        key="shutdown_dry_run",
        display_name="Auto Shutdown Dry Run",
        option_key=CONF_DRY_RUN_SERVICE_IDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:test-tube",
    ),
)

SERVICE_NUMBERS: tuple[CoreNumberDescription, ...] = (
    CoreNumberDescription(
        platform="number",
        key="idle_shutdown_minutes",
        display_name="Idle Shutdown Minutes",
        option_key=CONF_IDLE_MINUTES,
        default_value=DEFAULT_IDLE_MINUTES,
        native_min_value=3,
        native_max_value=120,
        native_step=1,
        native_unit_of_measurement="min",
        icon="mdi:timer-sand",
    ),
    CoreNumberDescription(
        platform="number",
        key="startup_cooldown_minutes",
        display_name="Startup Cooldown Minutes",
        option_key=CONF_STARTUP_COOLDOWN_MINUTES,
        default_value=DEFAULT_STARTUP_COOLDOWN_MINUTES,
        native_min_value=1,
        native_max_value=60,
        native_step=1,
        native_unit_of_measurement="min",
        icon="mdi:timer-outline",
    ),
)

SERVICE_SELECTS: tuple[CoreSelectDescription, ...] = ()


def core_service_entities() -> tuple[CoreEntityDescription, ...]:
    """Return all baseline service entity declarations."""

    return (
        SERVICE_SENSORS
        + SERVICE_BINARY_SENSORS
        + SERVICE_BUTTONS
        + SERVICE_SWITCHES
        + SERVICE_NUMBERS
        + SERVICE_SELECTS
    )


def service_sensor_descriptions(runtime: ServiceRuntime) -> tuple[CoreSensorDescription, ...]:
    """Return core plus profile-specific sensor descriptions for a runtime."""

    return SERVICE_SENSORS + tuple(
        description
        for description in profile_service_entities(runtime)
        if isinstance(description, CoreSensorDescription)
    )


def service_binary_sensor_descriptions(runtime: ServiceRuntime) -> tuple[CoreBinarySensorDescription, ...]:
    """Return core plus profile-specific binary sensor descriptions for a runtime."""

    return SERVICE_BINARY_SENSORS + tuple(
        description
        for description in profile_service_entities(runtime)
        if isinstance(description, CoreBinarySensorDescription)
    )


def service_button_descriptions(runtime: ServiceRuntime) -> tuple[CoreButtonDescription, ...]:
    """Return button descriptions for a runtime."""

    return SERVICE_BUTTONS + tuple(
        description
        for description in profile_service_entities(runtime)
        if isinstance(description, CoreButtonDescription)
    )


def service_switch_descriptions(runtime: ServiceRuntime) -> tuple[CoreSwitchDescription, ...]:
    """Return switch descriptions for a runtime."""

    return SERVICE_SWITCHES + tuple(
        description
        for description in profile_service_entities(runtime)
        if isinstance(description, CoreSwitchDescription)
    )


def service_number_descriptions(runtime: ServiceRuntime) -> tuple[CoreNumberDescription, ...]:
    """Return number descriptions for a runtime."""

    return SERVICE_NUMBERS + tuple(
        description
        for description in profile_service_entities(runtime)
        if isinstance(description, CoreNumberDescription)
    )


def service_select_descriptions(runtime: ServiceRuntime) -> tuple[CoreSelectDescription, ...]:
    """Return select descriptions for a runtime."""

    return SERVICE_SELECTS + tuple(
        description
        for description in profile_service_entities(runtime)
        if isinstance(description, CoreSelectDescription)
    )


def profile_service_entities(runtime: ServiceRuntime) -> tuple[CoreEntityDescription, ...]:
    """Return HA entity descriptions declared by the selected game profile."""

    profile = runtime.profile
    if profile is None:
        return ()
    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        _LOGGER.warning("Skipping invalid profile entity declarations: %s", err)
        return ()
    return tuple(
        description
        for declaration in manifest.entities
        if (description := _profile_entity_description(manifest.profile_id, declaration)) is not None
    )


def _profile_entity_description(profile_id: str, declaration: EntityDeclaration) -> CoreEntityDescription | None:
    """Convert a profile declaration into a core HA entity description."""

    key = normalized_profile_entity_key(profile_id, declaration.key)

    entity_category = _entity_category(declaration.attributes.get("entity_category"))
    enabled_default = bool(declaration.attributes.get("entity_registry_enabled_default", True))
    icon = declaration.attributes.get("icon")
    icon = icon if isinstance(icon, str) else None
    common = {
        "platform": declaration.platform,
        "key": key,
        "display_name": declaration.name,
        "entity_category": entity_category,
        "entity_registry_enabled_default": enabled_default,
        "icon": icon,
        "profile_id": profile_id,
        "value_fn": declaration.value_fn,
        "available_fn": declaration.available_fn,
    }

    if declaration.platform == "sensor":
        return CoreSensorDescription(**common)
    if declaration.platform == "binary_sensor":
        return CoreBinarySensorDescription(**common)
    if declaration.platform == "button":
        return CoreButtonDescription(**common, action_fn=declaration.action_fn)
    if declaration.platform == "switch":
        return CoreSwitchDescription(
            **common,
            option_key=_optional_string(declaration.attributes.get("option_key")),
            turn_on_fn=declaration.turn_on_fn,
            turn_off_fn=declaration.turn_off_fn,
        )
    if declaration.platform == "number":
        return CoreNumberDescription(
            **common,
            option_key=_optional_string(declaration.attributes.get("option_key")),
            default_value=_float_attribute(declaration.attributes.get("default_value"), 0),
            native_min_value=_optional_float(declaration.attributes.get("native_min_value")),
            native_max_value=_optional_float(declaration.attributes.get("native_max_value")),
            native_step=_optional_float(declaration.attributes.get("native_step")),
            native_unit_of_measurement=_optional_string(declaration.attributes.get("native_unit_of_measurement")),
            set_value_fn=declaration.set_value_fn,
        )
    if declaration.platform == "select":
        return CoreSelectDescription(
            **common,
            options=_string_tuple_attribute(declaration.attributes.get("options")),
            option_key=_optional_string(declaration.attributes.get("option_key")),
            options_fn=declaration.options_fn,
            select_option_fn=declaration.select_option_fn,
        )
    return None


def _entity_category(value: Any) -> Any:
    """Normalize profile entity category declarations."""

    if value == "config":
        return getattr(EntityCategory, "CONFIG", "config")
    if value == "diagnostic":
        return EntityCategory.DIAGNOSTIC
    return None


def _optional_string(value: Any) -> str | None:
    """Return a stripped string attribute when present."""

    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_float(value: Any) -> float | None:
    """Return a float attribute when parseable."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_attribute(value: Any, default: float) -> float:
    """Return a float attribute or default."""

    parsed = _optional_float(value)
    return default if parsed is None else parsed


def _string_tuple_attribute(value: Any) -> tuple[str, ...]:
    """Return a tuple of non-empty string attributes."""

    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
