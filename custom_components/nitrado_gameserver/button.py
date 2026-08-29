"""Button platform for Nitrado Game Server."""

from __future__ import annotations

from typing import Any

try:
    from homeassistant.components.button import ButtonEntity
    from homeassistant.exceptions import HomeAssistantError
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class ButtonEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""

    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        """Fallback HA error for local import/compile checks."""


from .coordinator import NitradoControlError, coordinator_from_hass
from .entities import CoreEntityDescription, service_button_descriptions
from .entity import NitradoServiceEntity
from .plugins.base import CapabilityVerdict, valid_capability_verdict


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    """Set up buttons for a config entry."""

    coordinator = coordinator_from_hass(hass, entry)
    async_add_entities(
        [
            NitradoServiceButton(coordinator, service_id, description)
            for service_id in coordinator.services
            for description in service_button_descriptions(coordinator.get_runtime(service_id))
        ]
    )


class NitradoServiceButton(NitradoServiceEntity, ButtonEntity):
    """Button backed by a service runtime command."""

    def __init__(self, coordinator: Any, service_id: str, description: CoreEntityDescription) -> None:
        super().__init__(coordinator, service_id, description)

    @property
    def available(self) -> bool:
        """Return true when this command is currently safe to press."""

        if not super().available:
            return False
        fresh_status = bool(
            self.runtime.server is not None and self.runtime.status_fresh and not self.runtime.using_cached_data
        )
        if self.entity_description.key == "start":
            return fresh_status and bool(self.runtime.last_start_verdict and self.runtime.last_start_verdict.allowed)
        if self.entity_description.key == "stop":
            return fresh_status and bool(self.runtime.last_stop_verdict and self.runtime.last_stop_verdict.allowed)
        if self.entity_description.key == "cancel_pending_shutdown":
            return self.coordinator.idle_shutdown_supported(self.service_id) and self.runtime.shutdown_pending
        available_fn = getattr(self.entity_description, "available_fn", None)
        if available_fn is not None:
            if self.is_profile_entity:
                return self.profile_callback_available(available_fn)
            return bool(available_fn(self.callback_context))
        return True

    async def async_press(self) -> None:
        """Run the button action."""

        try:
            if self.entity_description.key == "refresh":
                await self.coordinator.async_refresh_service(self.service_id)
                return
            if self.entity_description.key == "start":
                await self.coordinator.async_start_service(self.service_id)
                return
            if self.entity_description.key == "stop":
                await self.coordinator.async_stop_service(self.service_id)
                return
            if self.entity_description.key == "cancel_pending_shutdown":
                await self.coordinator.async_cancel_pending_shutdown(self.service_id)
                return
            action_fn = getattr(self.entity_description, "action_fn", None)
            if action_fn is not None:
                try:
                    result = await self.async_profile_callback("press", action_fn)
                except Exception as err:
                    raise HomeAssistantError(
                        f"Profile entity {self.entity_description.key} press failed: {err}"
                    ) from err
                if isinstance(result, CapabilityVerdict) and not valid_capability_verdict(result):
                    raise HomeAssistantError("Profile entity returned an invalid capability verdict")
                if isinstance(result, CapabilityVerdict) and not result.allowed:
                    raise HomeAssistantError(result.reason or result.state.value)
                return
        except NitradoControlError as err:
            raise HomeAssistantError(str(err)) from err

        raise HomeAssistantError(f"Unsupported Nitrado button: {self.entity_description.key}")
