"""Home Assistant entity helpers for Nitrado Game Server."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .coordinator import NitradoAccountCoordinator
from .entities import CoreEntityDescription
from .extensions import (
    profile_action_context as build_profile_action_context,
)
from .extensions import (
    profile_entity_context as build_profile_entity_context,
)
from .naming import service_display_name
from .operation_journal import OperationIntent
from .panel import device_configuration_url
from .plugins.base import ProfileActionContext, ProfileEntityContext, async_invoke_profile
from .plugins.registry import profile_registry_generation
from .profile_logging import log_profile_failure
from .runtime import ServiceRuntime

_LOGGER = logging.getLogger(__name__)


class ProfileEntityCallbackError(Exception):
    """Sanitized profile entity mutation failure safe for HA surfaces."""


def profile_action_context(coordinator: NitradoAccountCoordinator, runtime: ServiceRuntime) -> ProfileActionContext:
    """Build a profile action/editor context from current runtime state."""

    now_fn = getattr(coordinator, "now_fn", None)
    now = now_fn() if callable(now_fn) else None
    return build_profile_action_context(coordinator.profile_transport, runtime, now=now)


def profile_entity_context(runtime: ServiceRuntime) -> ProfileEntityContext:
    """Build a passive profile entity context from current runtime state."""

    return build_profile_entity_context(runtime)


class NitradoServiceEntity:
    """Mixin for an entity attached to one Nitrado service device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NitradoAccountCoordinator,
        service_id: str,
        description: CoreEntityDescription,
    ) -> None:
        super().__init__()
        self.service_id = service_id
        self.coordinator = coordinator
        self.entity_description = description
        self._attr_unique_id = description.unique_id(self.runtime)
        self._attr_name = description.display_name
        self._profile_generation = self.runtime.profile_generation if self.is_profile_entity else None
        self._profile_registry_generation = (
            self.runtime.profile_registry_generation_seen if self.is_profile_entity else None
        )
        if hasattr(description, "entity_registry_enabled_default"):
            self._attr_entity_registry_enabled_default = description.entity_registry_enabled_default

    @property
    def runtime(self) -> ServiceRuntime:
        """Return current service runtime."""

        return self.coordinator.get_runtime(self.service_id)

    @property
    def callback_context(self) -> ServiceRuntime | ProfileEntityContext:
        """Return the appropriate callback context for core or profile entity functions."""

        if self.is_profile_entity:
            return profile_entity_context(self.runtime)
        return self.runtime

    @property
    def is_profile_entity(self) -> bool:
        """Return true when this entity came from a profile declaration."""

        return bool(getattr(self.entity_description, "profile_id", None))

    def profile_callback_available(self, callback: Any) -> bool:
        """Return cached profile availability without invoking extension code."""

        del callback
        value = self._profile_cached_value("available", default=False)
        return bool(value)

    def profile_callback_value(self, callback: Any, *, default: Any = None, field: str = "value") -> Any:
        """Return a cached passive value without invoking extension code."""

        del callback
        return self._profile_cached_value(field, default=default)

    def _profile_cached_value(self, field: str, *, default: Any) -> Any:
        runtime = self.runtime
        if not self._profile_snapshot_current(runtime):
            return default
        snapshot = runtime.extra.get("_profile_entity_snapshot")
        if not isinstance(snapshot, dict):
            return default
        expected = self.coordinator.profile_dispatch_token(self.service_id)
        if snapshot.get("token") != expected:
            return default
        values = snapshot.get("values")
        if not isinstance(values, Mapping):
            return default
        entity_values = values.get(str(self.entity_description.key))
        if not isinstance(entity_values, Mapping):
            return default
        return entity_values.get(field, default)

    async def async_profile_callback(self, phase: str, callback: Any, *args: Any) -> Any:
        """Run a profile mutation callback and record failures before bubbling."""

        try:
            runtime = self.runtime

            async def operation() -> Any:
                intent = OperationIntent(
                    f"profile-entity-{phase}",
                    str(self.entity_description.key),
                    generation=(
                        f"{runtime.profile.profile_id}-{runtime.profile_generation}-"
                        f"{runtime.profile_registry_generation_seen}"
                    ),
                    expected_evidence="profile entity callback completed with its captured generation",
                )
                async with (
                    self.coordinator.async_operation(self.service_id, intent) as reservation,
                    runtime.profile_operation_lock,
                ):
                    if not self._profile_snapshot_current(runtime):
                        raise RuntimeError("The selected game profile changed; retry using the current entity")
                    await reservation.async_mark_dispatched()
                    result = await async_invoke_profile(
                        callback,
                        profile_action_context(self.coordinator, runtime),
                        *args,
                        require_async=True,
                    )
                    if not self._profile_snapshot_current(runtime):
                        raise RuntimeError("The selected game profile changed; retry using the current entity")
                    await reservation.async_mark_verifying()
                    return result

            result = await self.coordinator.async_track_profile_mutation(
                runtime.state.identity.service_id,
                operation,
            )
            await self.coordinator.async_refresh_profile_entity_snapshot(
                runtime.state.identity.service_id,
            )
            return result
        except Exception as err:
            message = self.record_profile_entity_error(phase, err)
            raise ProfileEntityCallbackError(message) from None

    def record_profile_entity_error(self, phase: str, err: Exception) -> str:
        """Record a profile entity callback error for diagnostics and return a user-facing message."""

        key = str(self.entity_description.key)
        message = f"Profile entity {key} {phase} failed; details were logged."
        self.runtime.last_profile_entity_errors[key] = {
            "phase": phase,
            "error": "Profile entity callback failed safely; details were logged.",
            "type": err.__class__.__name__,
        }
        log_profile_failure(
            _LOGGER,
            phase,
            err,
            profile_id=getattr(self.runtime.profile, "profile_id", "unknown"),
            key=key,
        )
        return message

    def _profile_snapshot_current(self, runtime: ServiceRuntime) -> bool:
        """Return whether this profile entity still belongs to the live registry generation."""

        return bool(
            self.is_profile_entity
            and self._profile_generation == runtime.profile_generation
            and self._profile_registry_generation == runtime.profile_registry_generation_seen
            and self._profile_registry_generation == profile_registry_generation()
        )

    async def async_added_to_hass(self) -> None:
        """Register for coordinator refresh notifications."""

        parent = getattr(super(), "async_added_to_hass", None)
        if parent:
            await parent()
        remove_listener = self.coordinator.async_add_listener(self.async_write_ha_state)
        if hasattr(self, "async_on_remove"):
            self.async_on_remove(remove_listener)

    @property
    def available(self) -> bool:
        """Return true when the service is available."""

        runtime = self.runtime
        return runtime.state.available and (not self.is_profile_entity or self._profile_snapshot_current(runtime))

    @property
    def device_info(self) -> dict[str, Any]:
        """Return Home Assistant device registry info."""

        runtime = self.runtime
        server = runtime.server
        service = runtime.service
        options = getattr(self.coordinator, "options", None)
        display_names = getattr(options, "service_display_names", {})
        name = service_display_name(runtime, display_names)

        game = None
        if server and server.game_human:
            game = server.game_human
        elif service and service.game_human:
            game = service.game_human

        return {
            "identifiers": {runtime.state.identity.device_identifier},
            "manufacturer": "Nitrado",
            "name": name,
            "model": game or "Game Server",
            "configuration_url": device_configuration_url(
                runtime.state.identity.service_id,
                self.coordinator.account_entry_id,
            ),
        }
