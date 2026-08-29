"""Binary sensor platform for Nitrado Game Server."""

from __future__ import annotations

from typing import Any

try:
    from homeassistant.components.binary_sensor import BinarySensorEntity
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class BinarySensorEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""


from .coordinator import coordinator_from_hass
from .entities import CoreEntityDescription, service_binary_sensor_descriptions
from .entity import NitradoServiceEntity

IDLE_SHUTDOWN_BINARY_SENSOR_KEYS = {"shutdown_pending"}


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    """Set up binary sensors for a config entry."""

    coordinator = coordinator_from_hass(hass, entry)
    async_add_entities(
        [
            NitradoServiceBinarySensor(coordinator, service_id, description)
            for service_id in coordinator.services
            for description in service_binary_sensor_descriptions(coordinator.get_runtime(service_id))
        ]
    )


class NitradoServiceBinarySensor(NitradoServiceEntity, BinarySensorEntity):
    """Binary sensor backed by a service runtime value function."""

    def __init__(self, coordinator: Any, service_id: str, description: CoreEntityDescription) -> None:
        super().__init__(coordinator, service_id, description)

    @property
    def available(self) -> bool:
        """Return true when this binary sensor applies to the service/profile."""

        if not super().available:
            return False
        if self.entity_description.key in IDLE_SHUTDOWN_BINARY_SENSOR_KEYS:
            return self.coordinator.idle_shutdown_supported(self.service_id)
        available_fn = getattr(self.entity_description, "available_fn", None)
        if available_fn is not None:
            if self.is_profile_entity:
                return self.profile_callback_available(available_fn)
            return bool(available_fn(self.callback_context))
        return True

    @property
    def is_on(self) -> bool | None:
        """Return current binary state."""

        value_fn = self.entity_description.value_fn
        if value_fn is None:
            return None
        if self.is_profile_entity:
            value = self.profile_callback_value(value_fn, default=None)
            if value is None:
                return None
            if type(value) is not bool:
                self.record_profile_entity_error("value", TypeError("binary sensor value must be boolean"))
                return None
            return value
        return bool(value_fn(self.callback_context))
