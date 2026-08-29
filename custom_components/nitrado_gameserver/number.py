"""Number platform for Nitrado Game Server."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

try:
    from homeassistant.components.number import NumberEntity
    from homeassistant.exceptions import HomeAssistantError
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class NumberEntity:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""

    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        """Fallback HA error for local import/compile checks."""


from .const import CONF_IDLE_MINUTES, CONF_STARTUP_COOLDOWN_MINUTES
from .coordinator import coordinator_from_hass
from .entities import CoreNumberDescription, service_number_descriptions
from .entity import NitradoServiceEntity
from .operation_journal import OperationIntent, OperationReservation
from .plugins.base import CapabilityVerdict, valid_capability_verdict
from .service_options import (
    entry_option_update_lock,
    profile_option_value,
    profile_options_map,
    service_number_map,
    updated_idle_number_options,
    updated_profile_option_options,
)

IDLE_SHUTDOWN_NUMBER_KEYS = {"idle_shutdown_minutes", "startup_cooldown_minutes"}


async def async_setup_entry(hass: Any, entry: Any, async_add_entities: Any) -> None:
    """Set up numbers for a config entry."""

    coordinator = coordinator_from_hass(hass, entry)
    async_add_entities(
        [
            NitradoServiceNumber(coordinator, entry, service_id, description)
            for service_id in coordinator.services
            for description in service_number_descriptions(coordinator.get_runtime(service_id))
        ]
    )


class NitradoServiceNumber(NitradoServiceEntity, NumberEntity):
    """Persisted per-service number."""

    entity_description: CoreNumberDescription

    def __init__(self, coordinator: Any, entry: Any, service_id: str, description: CoreNumberDescription) -> None:
        self.entry = entry
        super().__init__(coordinator, service_id, description)

    @property
    def available(self) -> bool:
        """Return true when this number applies to the service/profile."""

        if not super().available:
            return False
        if self.entity_description.key in IDLE_SHUTDOWN_NUMBER_KEYS:
            return self.coordinator.idle_shutdown_configurable(self.service_id)
        available_fn = self.entity_description.available_fn
        if available_fn is not None:
            if self.is_profile_entity:
                return self.profile_callback_available(available_fn)
            return bool(available_fn(self.callback_context))
        return True

    @property
    def native_value(self) -> float | None:
        """Return persisted number state."""

        if self.entity_description.value_fn is not None:
            if self.is_profile_entity:
                value = self.profile_callback_value(self.entity_description.value_fn, default=None)
                try:
                    return None if value is None else float(value)
                except (TypeError, ValueError) as err:
                    self.record_profile_entity_error("value", err)
                    return None
            return float(self.entity_description.value_fn(self.callback_context))
        if self.entity_description.option_key is None:
            return float(self.entity_description.default_value)
        if self.is_profile_entity:
            value = profile_option_value(
                dict(self.entry.options),
                str(self.entity_description.profile_id),
                self.entity_description.option_key,
                self.service_id,
                self.entity_description.default_value,
            )
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(self.entity_description.default_value)
        values = service_number_map(
            dict(self.entry.options), self.entity_description.option_key, self.entity_description.default_value
        )
        return float(values.get(str(self.service_id), self.entity_description.default_value))

    async def async_set_native_value(self, value: float) -> None:
        """Persist and apply a per-service number."""

        minimum = self.entity_description.native_min_value
        maximum = self.entity_description.native_max_value
        if minimum is not None:
            value = max(float(minimum), float(value))
        if maximum is not None:
            value = min(float(maximum), float(value))

        if self.entity_description.set_value_fn is not None:
            try:
                result = await self.async_profile_callback("set_value", self.entity_description.set_value_fn, value)
            except Exception as err:
                raise HomeAssistantError(
                    f"Profile entity {self.entity_description.key} set_value failed: {err}"
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
            return

        if self.entity_description.option_key is None:
            raise HomeAssistantError(f"Unsupported Nitrado number: {self.entity_description.key}")
        intent = OperationIntent(
            "entity-option",
            str(self.entity_description.key),
            expected_evidence="Home Assistant config-entry options contain the selected numeric value",
        )
        async with (
            entry_option_update_lock(self.hass, self.entry.entry_id),
            self.coordinator.async_operation(self.service_id, intent) as reservation,
        ):
            await self._async_set_native_value_reserved(value, reservation)

    async def _async_set_native_value_reserved(
        self,
        value: float,
        reservation: OperationReservation,
    ) -> None:
        """Persist a number beneath the authoritative service reservation."""

        if self.is_profile_entity:
            options = updated_profile_option_options(
                dict(self.entry.options),
                str(self.entity_description.profile_id),
                self.entity_description.option_key,
                self.service_id,
                float(value),
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
        options = updated_idle_number_options(
            dict(self.entry.options),
            self.service_id,
            self.entity_description.option_key,
            value,
        )
        await reservation.async_mark_dispatched()
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        values = service_number_map(options, self.entity_description.option_key, self.entity_description.default_value)
        key = self.entity_description.option_key
        if key == CONF_IDLE_MINUTES:
            self.coordinator.options = replace(self.coordinator.options, idle_minutes=values)
        elif key == CONF_STARTUP_COOLDOWN_MINUTES:
            self.coordinator.options = replace(self.coordinator.options, startup_cooldown_minutes=values)
        self.coordinator.revoke_pending_shutdown(
            self.service_id,
            f"{self.entity_description.display_name} changed; pending shutdown revoked",
        )
        if write_state := getattr(self, "async_write_ha_state", None):
            write_state()
        await reservation.async_mark_verifying()
