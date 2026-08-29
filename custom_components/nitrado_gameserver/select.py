"""Select platform for Nitrado Game Server."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

try:
    from homeassistant.components.select import SelectEntity
    from homeassistant.exceptions import HomeAssistantError
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class SelectEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""

    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        """Fallback HA error for local import/compile checks."""


from .coordinator import coordinator_from_hass
from .entities import CoreSelectDescription, service_select_descriptions
from .entity import NitradoServiceEntity
from .operation_journal import OperationIntent, OperationReservation
from .plugins.base import CapabilityVerdict, valid_capability_verdict
from .service_options import (
    entry_option_update_lock,
    profile_option_value,
    profile_options_map,
    updated_profile_option_options,
)


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    """Set up selects for a config entry."""

    coordinator = coordinator_from_hass(hass, entry)
    async_add_entities(
        [
            NitradoServiceSelect(coordinator, entry, service_id, description)
            for service_id in coordinator.services
            for description in service_select_descriptions(coordinator.get_runtime(service_id))
        ]
    )


class NitradoServiceSelect(NitradoServiceEntity, SelectEntity):
    """Profile-declared select entity."""

    entity_description: CoreSelectDescription

    def __init__(self, coordinator: Any, entry: Any, service_id: str, description: CoreSelectDescription) -> None:
        self.entry = entry
        super().__init__(coordinator, service_id, description)

    @property
    def available(self) -> bool:
        """Return true when this select applies to the service/profile."""

        if not super().available:
            return False
        available_fn = self.entity_description.available_fn
        if available_fn is not None:
            if self.is_profile_entity:
                return self.profile_callback_available(available_fn)
            return bool(available_fn(self.callback_context))
        return True

    @property
    def options(self) -> list[str]:
        """Return selectable options."""

        if self.entity_description.options_fn is not None:
            if self.is_profile_entity:
                value = self.profile_callback_value(self.entity_description.options_fn, default=(), field="options")
                if not isinstance(value, (list, tuple)):
                    self.record_profile_entity_error("options", TypeError("options_fn must return a list or tuple"))
                    return []
                return [item for item in value if isinstance(item, str)]
            return list(self.entity_description.options_fn(self.callback_context))
        return list(self.entity_description.options)

    @property
    def current_option(self) -> str | None:
        """Return the selected option."""

        if self.entity_description.value_fn is not None:
            if self.is_profile_entity:
                value = self.profile_callback_value(self.entity_description.value_fn, default=None)
            else:
                value = self.entity_description.value_fn(self.callback_context)
            return value if isinstance(value, str) else None
        if self.is_profile_entity and self.entity_description.option_key is not None:
            available_options = self.options
            default = available_options[0] if available_options else None
            value = profile_option_value(
                dict(self.entry.options),
                str(self.entity_description.profile_id),
                self.entity_description.option_key,
                self.service_id,
                default,
            )
            return value if isinstance(value, str) and value in available_options else default
        value = None
        return value if isinstance(value, str) else None

    async def async_select_option(self, option: str) -> None:
        """Select an option through the profile handler."""

        if option not in self.options:
            raise HomeAssistantError(f"Unsupported Nitrado select option: {option}")
        handler = self.entity_description.select_option_fn
        if handler is None:
            if not self.is_profile_entity or self.entity_description.option_key is None:
                raise HomeAssistantError(f"Unsupported Nitrado select: {self.entity_description.key}")
            intent = OperationIntent(
                "entity-option",
                str(self.entity_description.key),
                expected_evidence="Home Assistant config-entry options contain the selected option value",
            )
            async with (
                entry_option_update_lock(self.hass, self.entry.entry_id),
                self.coordinator.async_operation(self.service_id, intent) as reservation,
            ):
                await self._async_select_option_reserved(option, reservation)
            return
        try:
            result = await self.async_profile_callback("select_option", handler, option)
        except Exception as err:
            raise HomeAssistantError(
                f"Profile entity {self.entity_description.key} select_option failed: {err}"
            ) from err
        if isinstance(result, CapabilityVerdict) and not valid_capability_verdict(result):
            raise HomeAssistantError("Profile entity returned an invalid capability verdict")
        if isinstance(result, CapabilityVerdict) and not result.allowed:
            raise HomeAssistantError(result.reason or result.state.value)
        if (
            getattr(self, "hass", None) is not None
            and getattr(self, "entity_id", None) is not None
            and (write_state := getattr(self, "async_write_ha_state", None))
        ):
            write_state()

    async def _async_select_option_reserved(
        self,
        option: str,
        reservation: OperationReservation,
    ) -> None:
        """Persist one profile select beneath the shared option transaction."""

        if self.entity_description.option_key is None:
            raise HomeAssistantError(f"Unsupported Nitrado select: {self.entity_description.key}")
        options = updated_profile_option_options(
            dict(self.entry.options),
            str(self.entity_description.profile_id),
            self.entity_description.option_key,
            self.service_id,
            option,
        )
        await reservation.async_mark_dispatched()
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.coordinator.options = replace(
            self.coordinator.options,
            profile_options=profile_options_map(options),
        )
        await self.coordinator.async_profile_options_changed(
            self.runtime,
            f"{self.entity_description.display_name} changed; pending shutdown revoked",
        )
        if (
            getattr(self, "hass", None) is not None
            and getattr(self, "entity_id", None) is not None
            and (write_state := getattr(self, "async_write_ha_state", None))
        ):
            write_state()
        await reservation.async_mark_verifying()
