"""Switch platform for Nitrado Game Server."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

try:
    from homeassistant.components.switch import SwitchEntity
    from homeassistant.exceptions import HomeAssistantError
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class SwitchEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""

    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        """Fallback HA error for local import/compile checks."""


from .coordinator import coordinator_from_hass
from .entities import CoreSwitchDescription, service_switch_descriptions
from .entity import NitradoServiceEntity
from .operation_journal import OperationIntent, OperationReservation
from .plugins.base import CapabilityVerdict, valid_capability_verdict
from .service_options import (
    entry_option_update_lock,
    profile_option_value,
    profile_options_map,
    service_id_set,
    updated_idle_toggle_options,
    updated_profile_option_options,
)

IDLE_SHUTDOWN_SWITCH_KEYS = {"auto_shutdown", "maintenance_mode", "shutdown_dry_run"}


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    """Set up switches for a config entry."""

    coordinator = coordinator_from_hass(hass, entry)
    async_add_entities(
        [
            NitradoServiceSwitch(coordinator, entry, service_id, description)
            for service_id in coordinator.services
            for description in service_switch_descriptions(coordinator.get_runtime(service_id))
        ]
    )


class NitradoServiceSwitch(NitradoServiceEntity, SwitchEntity):
    """Persisted per-service switch."""

    entity_description: CoreSwitchDescription

    def __init__(self, coordinator: Any, entry: Any, service_id: str, description: CoreSwitchDescription) -> None:
        self.entry = entry
        super().__init__(coordinator, service_id, description)

    @property
    def available(self) -> bool:
        """Return true when this switch applies to the service/profile."""

        if not super().available:
            return False
        if self.entity_description.key in IDLE_SHUTDOWN_SWITCH_KEYS:
            return self.coordinator.idle_shutdown_configurable(self.service_id)
        available_fn = self.entity_description.available_fn
        if available_fn is not None:
            if self.is_profile_entity:
                return self.profile_callback_available(available_fn)
            return bool(available_fn(self.callback_context))
        return True

    @property
    def is_on(self) -> bool:
        """Return persisted switch state."""

        if self.entity_description.value_fn is not None:
            if self.is_profile_entity:
                value = self.profile_callback_value(self.entity_description.value_fn, default=None)
                if type(value) is not bool:
                    self.record_profile_entity_error("value", TypeError("switch value must be boolean"))
                    return False
                return value
            return bool(self.entity_description.value_fn(self.callback_context))
        if self.entity_description.option_key is None:
            return False
        if self.is_profile_entity:
            return bool(
                profile_option_value(
                    dict(self.entry.options),
                    str(self.entity_description.profile_id),
                    self.entity_description.option_key,
                    self.service_id,
                    False,
                )
            )
        return str(self.service_id) in service_id_set(dict(self.entry.options), self.entity_description.option_key)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the switch."""

        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the switch."""

        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Persist/apply a per-service toggle or dispatch a profile handler."""

        handler = self.entity_description.turn_on_fn if enabled else self.entity_description.turn_off_fn
        if handler is not None:
            phase = "turn_on" if enabled else "turn_off"
            try:
                result = await self.async_profile_callback(phase, handler)
            except Exception as err:
                raise HomeAssistantError(f"Profile entity {self.entity_description.key} {phase} failed: {err}") from err
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
            return
        await self._async_set_enabled_option(enabled)

    async def _async_set_enabled_option(self, enabled: bool) -> None:
        """Persist and apply a per-service toggle."""

        intent = OperationIntent(
            "entity-option",
            str(self.entity_description.key),
            expected_evidence="Home Assistant config-entry options contain the selected boolean value",
        )
        async with (
            entry_option_update_lock(self.hass, self.entry.entry_id),
            self.coordinator.async_operation(self.service_id, intent) as reservation,
        ):
            await self._async_set_enabled_option_reserved(enabled, reservation)

    async def _async_set_enabled_option_reserved(
        self,
        enabled: bool,
        reservation: OperationReservation,
    ) -> None:
        """Persist a toggle beneath the authoritative service reservation."""

        if self.entity_description.option_key is None:
            raise HomeAssistantError(f"Unsupported Nitrado switch: {self.entity_description.key}")
        if self.is_profile_entity:
            options = updated_profile_option_options(
                dict(self.entry.options),
                str(self.entity_description.profile_id),
                self.entity_description.option_key,
                self.service_id,
                enabled,
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
            return
        if (
            enabled
            and self.entity_description.key == "auto_shutdown"
            and not self.coordinator.idle_shutdown_configurable(self.service_id)
        ):
            missing = self.coordinator.unmet_idle_shutdown_profile_options(self.service_id)
            reason = (
                f"{missing[0].name} must be enabled before Auto Shutdown can be armed."
                if missing
                else "The selected game profile is not ready for Auto Shutdown."
            )
            raise HomeAssistantError(reason)
        options = updated_idle_toggle_options(
            dict(self.entry.options),
            self.service_id,
            self.entity_description.option_key,
            enabled,
        )
        await reservation.async_mark_dispatched()
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        values = frozenset(service_id_set(options, self.entity_description.option_key))
        key = self.entity_description.option_key
        if key == "idle_shutdown_service_ids":
            self.coordinator.options = replace(self.coordinator.options, idle_shutdown_service_ids=values)
        elif key == "maintenance_service_ids":
            self.coordinator.options = replace(self.coordinator.options, maintenance_service_ids=values)
        elif key == "dry_run_service_ids":
            self.coordinator.options = replace(self.coordinator.options, dry_run_service_ids=values)
        self.coordinator.revoke_pending_shutdown(
            self.service_id,
            f"{self.entity_description.display_name} changed; pending shutdown revoked",
        )
        if write_state := getattr(self, "async_write_ha_state", None):
            write_state()
        await reservation.async_mark_verifying()
