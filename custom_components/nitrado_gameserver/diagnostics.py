"""Diagnostics skeleton for Nitrado Game Server."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .api.nitrado import ParsedServer, redact_payload
from .const import CONF_PROFILE_OPTIONS, DOMAIN
from .coordinator import NitradoAccountCoordinator
from .plugins.base import (
    UNSET,
    CapabilityVerdict,
    ProfileManifestError,
    ProfileOptionType,
    ProfileStatus,
)
from .provider_runtime import get_provider_runtime
from .runtime import ServiceRuntime, runtime_profile_extension_manifest


async def async_get_config_entry_diagnostics(hass, config_entry) -> dict[str, Any]:
    """Return redacted diagnostics."""

    entry_id = getattr(config_entry, "entry_id", None)
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id) if hass and entry_id else None
    provider_runtime = get_provider_runtime(hass) if hass else None
    native_backup_diagnostics = (
        await provider_runtime.async_native_backup_diagnostics(entry_id)
        if provider_runtime is not None and entry_id is not None
        else None
    )
    return redact_payload(
        {
            "entry": {
                "data": dict(config_entry.data),
                "options": _redact_declared_profile_secrets(dict(config_entry.options), coordinator),
            },
            "runtime": coordinator_diagnostics(coordinator) if coordinator else None,
            "provider_connector": (
                {
                    "grant_store_valid": provider_runtime.authority.storage_error is None,
                    "grant_store_error": provider_runtime.authority.storage_error,
                    "grant_count": len(provider_runtime.authority.grants()),
                    "native_backup_restore": native_backup_diagnostics,
                }
                if provider_runtime is not None
                else None
            ),
        }
    )


def _redact_declared_profile_secrets(
    options: dict[str, Any], coordinator: NitradoAccountCoordinator | None
) -> dict[str, Any]:
    """Redact profile-option secrets by declaration, failing closed for unknown declarations."""

    result = deepcopy(options)
    profile_values = result.get(CONF_PROFILE_OPTIONS)
    if not isinstance(profile_values, dict):
        return result

    declared: dict[str, dict[str, ProfileOptionType | None]] = {}
    if coordinator is not None:
        for runtime in coordinator.services.values():
            try:
                manifest = runtime_profile_extension_manifest(runtime)
            except ProfileManifestError:
                continue
            profile_declarations = declared.setdefault(manifest.profile_id, {})
            for item in manifest.profile_options:
                previous = profile_declarations.get(item.key)
                if previous is None and item.key not in profile_declarations:
                    profile_declarations[item.key] = item.option_type
                elif previous != item.option_type:
                    # Declaration conflicts are untrusted. Never let iteration
                    # order downgrade a secret to a visible option.
                    profile_declarations[item.key] = None

    for profile_id, profile_options in profile_values.items():
        if not isinstance(profile_options, dict):
            continue
        known_options = declared.get(str(profile_id), {})
        for option_key, service_values in profile_options.items():
            option_type = known_options.get(str(option_key))
            if option_type != ProfileOptionType.SECRET and option_type is not None:
                continue
            if isinstance(service_values, dict):
                profile_options[option_key] = {service_id: "[redacted]" for service_id in service_values}
            else:
                profile_options[option_key] = "[redacted]"
    return result


def coordinator_diagnostics(coordinator: NitradoAccountCoordinator) -> dict[str, Any]:
    """Return a redacted, support-friendly coordinator diagnostic payload."""

    return {
        "account": coordinator.snapshot(),
        "last_refresh_error": coordinator.last_refresh_error,
        "last_request_failure": (
            coordinator.client.last_request_failure.as_dict()
            if getattr(coordinator.client, "last_request_failure", None) is not None
            else None
        ),
        "options": {
            "discovery_mode": coordinator.options.discovery_mode,
            "missing_service_threshold": coordinator.options.missing_service_threshold,
            "settle_seconds": coordinator.options.settle_seconds,
            "imported_service_ids": sorted(coordinator.options.imported_service_ids),
            "ignored_service_ids": sorted(coordinator.options.ignored_service_ids),
            "service_display_names": dict(coordinator.options.service_display_names),
            "service_area_ids": dict(coordinator.options.service_area_ids),
            "idle_shutdown_service_ids": sorted(coordinator.options.idle_shutdown_service_ids),
            "maintenance_service_ids": sorted(coordinator.options.maintenance_service_ids),
            "dry_run_service_ids": sorted(coordinator.options.dry_run_service_ids),
            "allow_plaintext_ftp_service_ids": sorted(coordinator.options.allow_plaintext_ftp_service_ids),
            "idle_minutes": dict(coordinator.options.idle_minutes),
            "startup_cooldown_minutes": dict(coordinator.options.startup_cooldown_minutes),
        },
        "services": {
            service_id: runtime_diagnostics(runtime) for service_id, runtime in sorted(coordinator.services.items())
        },
        "provider_filesystem": _filesystem_diagnostics(coordinator),
    }


def _filesystem_diagnostics(coordinator: NitradoAccountCoordinator) -> dict[str, Any]:
    """Return cached, secret-free provider-file facts without network access."""

    filesystem = coordinator.filesystem
    statuses = (
        {service_id: filesystem.observed_status(service_id) for service_id in sorted(coordinator.services)}
        if filesystem is not None
        else {}
    )
    return {
        "services": statuses,
        "unresolved": [
            {
                "issue": fact.issue.value,
                "transaction_id": fact.transaction_id,
                "service_id": fact.service.service_id if fact.service is not None else None,
                "operation": fact.operation.value if fact.operation is not None else None,
                "state": fact.state.value if fact.state is not None else None,
                "recovery_available": fact.recovery_available,
                "failure_code": fact.failure_code.value if fact.failure_code is not None else None,
            }
            for fact in coordinator.filesystem_recovery_facts
        ],
    }


def runtime_diagnostics(runtime: ServiceRuntime) -> dict[str, Any]:
    """Return support-friendly diagnostics for one managed service runtime."""

    profile_status = runtime.extra.get("profile_status")
    return {
        "state": {
            "service_id": runtime.state.service_id,
            "available": runtime.state.available,
            "pending_discovery": runtime.state.pending_discovery,
            "ignored": runtime.state.ignored,
            "missing_count": runtime.state.missing_count,
        },
        "service": _service_diagnostics(runtime),
        "profile": _profile_diagnostics(runtime),
        "status": _status_diagnostics(runtime),
        "players": _player_diagnostics(runtime, profile_status),
        "idle_shutdown": _idle_shutdown_diagnostics(runtime),
        "controls": {
            "start": _verdict_diagnostics(runtime.last_start_verdict),
            "stop": _verdict_diagnostics(runtime.last_stop_verdict),
        },
        "lifecycle_hooks": dict(runtime.last_lifecycle_hook_results),
        "lifecycle_dispatch_errors": dict(runtime.last_lifecycle_dispatch_errors),
        "profile_entity_errors": dict(runtime.last_profile_entity_errors),
        "profile_enrichment": _profile_status_diagnostics(profile_status),
    }


def _service_diagnostics(runtime: ServiceRuntime) -> dict[str, Any] | None:
    service = runtime.service
    if service is None:
        return None
    return {
        "service_id": service.service_id,
        "name": service.name,
        "game": service.game,
        "game_human": service.game_human,
        "folder_short": service.folder_short,
        "type_human": service.type_human,
    }


def _profile_diagnostics(runtime: ServiceRuntime) -> dict[str, Any] | None:
    profile = runtime.profile
    if profile is None:
        return None
    return {
        "profile_id": getattr(profile, "profile_id", None),
        "name": getattr(profile, "name", None),
        "supported_games": list(getattr(profile, "supported_games", ())),
        "idle_shutdown_supported": bool(getattr(profile, "idle_shutdown_supported", False)),
        "extensions": _profile_extension_diagnostics(runtime),
    }


def _profile_extension_diagnostics(runtime: ServiceRuntime) -> dict[str, Any]:
    """Return profile extension capability metadata without invoking handlers."""

    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError:
        return {
            "invalid": True,
            "error": "The selected profile manifest is invalid; details were logged.",
            "entities": [],
            "profile_options": [],
            "editable_files": [],
            "resources": [],
            "actions": [],
            "surfaces": [],
            "lifecycle_hooks": [],
            "validators": [],
        }
    return {
        "invalid": False,
        "entities": [
            {
                "platform": entity.platform,
                "key": entity.key,
                "name": entity.name,
                "kind": entity.kind,
                "enabled_default": bool(entity.attributes.get("entity_registry_enabled_default", True)),
                "entity_category": entity.attributes.get("entity_category"),
            }
            for entity in manifest.entities
        ],
        "profile_options": [
            {
                "key": option.key,
                "name": option.name,
                "option_type": option.option_type.value,
                "default": "[redacted]" if option.option_type == ProfileOptionType.SECRET else option.default,
            }
            for option in manifest.profile_options
        ],
        "editable_files": [
            {
                "key": file.key,
                "name": file.name,
                "requires_restart": file.requires_restart,
                "requires_stopped": file.requires_stopped,
                "requires_running": file.requires_running,
                "create_backup": file.create_backup,
                "validators": list(file.validators),
            }
            for file in manifest.editable_files
        ],
        "resources": [
            {
                "key": resource.key,
                "name": resource.name,
                "content_type": resource.content_type,
                "cache_seconds": resource.cache_seconds,
                "validators": list(resource.validators),
            }
            for resource in manifest.resources
        ],
        "actions": [
            {
                "key": action.key,
                "name": action.name,
                "requires_confirmation": action.requires_confirmation,
                "validators": list(action.validators),
            }
            for action in manifest.actions
        ],
        "surfaces": [
            {
                "key": surface.key,
                "name": surface.name,
                "resources": list(surface.resources),
                "actions": list(surface.actions),
                "controls": list(surface.controls),
                "editable_files": list(surface.editable_files),
                "renderer_hint": surface.renderer_hint,
                "validators": list(surface.validators),
            }
            for surface in manifest.surfaces
        ],
        "lifecycle_hooks": [
            {
                "key": hook.key,
                "event": hook.event.value,
                "order": hook.order,
                "blocking": hook.blocking,
                "validators": list(hook.validators),
            }
            for hook in manifest.lifecycle_hooks
        ],
        "validators": [
            {
                "key": validator.key,
                "name": validator.name,
                "target": validator.target.value,
                "applies_to": [target.value for target in validator.applies_to],
                "domains": [domain.value for domain in validator.domains],
            }
            for validator in manifest.validators
        ],
    }


def _status_diagnostics(runtime: ServiceRuntime) -> dict[str, Any]:
    server = runtime.server
    return {
        "raw_status": server.raw_status if server else None,
        "server_name": server.server_name if server else None,
        "game_short": server.game_short if server else None,
        "game_human": server.game_human if server else None,
        "address_available": bool(server and server.address),
        "status_fresh": runtime.status_fresh,
        "using_cached_data": runtime.using_cached_data,
        "profile_refresh_error": runtime.last_profile_refresh_error,
        "last_status": runtime.last_status,
        "refreshed_at": runtime.refreshed_at,
        "last_transition_at": runtime.last_transition_at,
        "stopped_since": runtime.stopped_since,
        "started_since": runtime.started_since,
    }


def _idle_shutdown_diagnostics(runtime: ServiceRuntime) -> dict[str, Any]:
    return {
        "idle_started_at": runtime.idle_started_at,
        "startup_cooldown_started_at": runtime.startup_cooldown_started_at,
        "shutdown_pending": runtime.shutdown_pending,
        "last_shutdown_reason": runtime.last_shutdown_reason,
        "last_reset_reason": runtime.last_reset_reason,
        "last_start_time": runtime.last_start_time,
        "last_stop_time": runtime.last_stop_time,
    }


def _player_diagnostics(runtime: ServiceRuntime, profile_status: Any) -> dict[str, Any]:
    server = runtime.server
    nitrado_query = _nitrado_query_diagnostics(server)
    profile_diag = _profile_status_diagnostics(profile_status)
    return {
        "final": {
            "player_count": server.player_count if server else None,
            "player_max": server.player_max if server else None,
            "player_names_count": len(server.player_names) if server else 0,
            "player_source": server.player_source if server else None,
            "query_valid": server.query_valid if server else None,
        },
        "nitrado_query": nitrado_query,
        "profile_override": {
            "applied": bool(profile_diag),
            "player_count": profile_diag.get("player_count") if profile_diag else None,
            "player_max": profile_diag.get("player_max") if profile_diag else None,
            "player_names_count": profile_diag.get("player_names_count") if profile_diag else None,
            "player_source": profile_diag.get("player_source") if profile_diag else None,
            "query_valid": profile_diag.get("query_valid") if profile_diag else None,
            "extra": profile_diag.get("extra", {}) if profile_diag else {},
        },
    }


def _nitrado_query_diagnostics(server: ParsedServer | None) -> dict[str, Any]:
    if server is None:
        return {}
    gameserver = server.raw_redacted.get("gameserver") if isinstance(server.raw_redacted, dict) else None
    query = gameserver.get("query") if isinstance(gameserver, dict) else None
    if not isinstance(query, dict):
        return {
            "present": False,
            "valid": False,
        }
    players = query.get("players")
    return {
        "present": True,
        "valid": server.player_source == "nitrado_query" or server.query_valid,
        "player_current": query.get("player_current"),
        "player_max": query.get("player_max"),
        "players_count": len(players) if isinstance(players, list) else None,
        "server_name": query.get("server_name"),
    }


def _profile_status_diagnostics(profile_status: Any) -> dict[str, Any]:
    if not isinstance(profile_status, ProfileStatus):
        return {}
    return {
        "player_count": _profile_value(profile_status.player_count),
        "player_max": _profile_value(profile_status.player_max),
        "player_names_count": len(profile_status.player_names) if profile_status.player_names is not UNSET else None,
        "player_source": _profile_value(profile_status.player_source),
        "query_valid": _profile_value(profile_status.query_valid),
        "display_name": profile_status.display_name if profile_status.display_name is not UNSET else None,
        "extra": dict(profile_status.extra),
    }


def _profile_value(value: Any) -> Any:
    """Return a diagnostics-safe profile value."""

    return None if value is UNSET else value


def _verdict_diagnostics(verdict: CapabilityVerdict | None) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {
        "state": verdict.state.value,
        "allowed": verdict.allowed,
        "reason": verdict.reason,
        "source": verdict.source.value,
        "overridable": verdict.overridable,
    }
