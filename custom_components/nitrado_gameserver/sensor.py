"""Sensor platform for Nitrado Game Server."""

from __future__ import annotations

from typing import Any

try:
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.helpers.restore_state import RestoreEntity
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class SensorEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""

    class RestoreEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""


from .coordinator import coordinator_from_hass
from .entities import CoreEntityDescription, service_sensor_descriptions
from .entity import NitradoServiceEntity
from .naming import service_display_name

IDLE_SHUTDOWN_TIMER_SENSOR_KEYS = {
    "idle_minutes",
    "idle_time_remaining",
}
IDLE_SHUTDOWN_AUDIT_SENSOR_KEYS = {
    "last_shutdown_reason",
    "last_reset_reason",
}
AUTO_SHUTDOWN_STATUS_SENSOR_KEY = "auto_shutdown_status"


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    """Set up sensors for a config entry."""

    coordinator = coordinator_from_hass(hass, entry)
    async_add_entities(
        [
            NitradoServiceSensor(coordinator, service_id, description)
            for service_id in coordinator.services
            for description in service_sensor_descriptions(coordinator.get_runtime(service_id))
        ]
    )


class NitradoServiceSensor(NitradoServiceEntity, RestoreEntity, SensorEntity):
    """Sensor backed by a service runtime value function."""

    def __init__(self, coordinator: Any, service_id: str, description: CoreEntityDescription) -> None:
        super().__init__(coordinator, service_id, description)

    async def async_added_to_hass(self) -> None:
        """Register updates and restore selected audit-only sensor values."""

        await super().async_added_to_hass()
        if self.entity_description.key != "last_shutdown_reason":
            return
        get_last_state = getattr(self, "async_get_last_state", None)
        if not get_last_state:
            return
        last_state = await get_last_state()
        if last_state is None or last_state.state in {"unknown", "unavailable", "None", ""}:
            return
        self.runtime.last_shutdown_reason = str(last_state.state)

    @property
    def available(self) -> bool:
        """Return true when this sensor applies to the service/profile."""

        if not super().available:
            return False
        if self.entity_description.key in IDLE_SHUTDOWN_TIMER_SENSOR_KEYS:
            return self.coordinator.idle_shutdown_supported(self.service_id)
        if self.entity_description.key == AUTO_SHUTDOWN_STATUS_SENSOR_KEY:
            return self.coordinator.auto_shutdown_status(self.service_id) is not None
        if self.entity_description.key in IDLE_SHUTDOWN_AUDIT_SENSOR_KEYS:
            return self.coordinator.idle_shutdown_configurable(self.service_id)
        available_fn = getattr(self.entity_description, "available_fn", None)
        if available_fn is not None:
            if self.is_profile_entity:
                return self.profile_callback_available(available_fn)
            return bool(available_fn(self.callback_context))
        return True

    @property
    def native_value(self) -> Any:
        """Return current sensor value."""

        if self.entity_description.key == "server_name":
            options = getattr(self.coordinator, "options", None)
            display_names = getattr(options, "service_display_names", {})
            return service_display_name(self.runtime, display_names)
        if self.entity_description.key == "idle_time_remaining":
            return round(
                self.runtime.idle_remaining_minutes(
                    self.runtime.refreshed_at or 0,
                    self.coordinator.idle_limit_minutes(self.service_id),
                ),
                1,
            )
        if self.entity_description.key == AUTO_SHUTDOWN_STATUS_SENSOR_KEY:
            return self.coordinator.auto_shutdown_status(self.service_id)

        value_fn = self.entity_description.value_fn
        if value_fn is None:
            return None
        if self.is_profile_entity:
            return self.profile_callback_value(value_fn)
        return value_fn(self.callback_context)
