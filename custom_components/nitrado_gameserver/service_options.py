"""Helpers for persisted per-service options."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .const import (
    CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    CONF_DISCOVERY_INTERVAL,
    CONF_DISCOVERY_MODE,
    CONF_DRY_RUN_SERVICE_IDS,
    CONF_IDLE_MINUTES,
    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
    CONF_IGNORED_SERVICE_IDS,
    CONF_IMPORTED_SERVICE_IDS,
    CONF_MAINTENANCE_SERVICE_IDS,
    CONF_MISSING_SERVICE_THRESHOLD,
    CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS,
    CONF_PROFILE_OPTIONS,
    CONF_SERVICE_AREA_IDS,
    CONF_SERVICE_DISPLAY_NAMES,
    CONF_SETTLE_SECONDS,
    CONF_STARTUP_COOLDOWN_MINUTES,
    CONF_STATUS_INTERVAL,
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_DISCOVERY_MODE,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_MISSING_SERVICE_THRESHOLD,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_STARTUP_COOLDOWN_MINUTES,
    DEFAULT_STATUS_INTERVAL,
    DISCOVERY_MODES,
)

_SCALAR_LIMITS: dict[str, tuple[int, int, int]] = {
    CONF_DISCOVERY_INTERVAL: (DEFAULT_DISCOVERY_INTERVAL, 60, 604800),
    CONF_STATUS_INTERVAL: (DEFAULT_STATUS_INTERVAL, 30, 86400),
    CONF_MISSING_SERVICE_THRESHOLD: (DEFAULT_MISSING_SERVICE_THRESHOLD, 1, 100),
    CONF_SETTLE_SECONDS: (DEFAULT_SETTLE_SECONDS, 0, 86400),
}


def entry_option_update_lock(hass: Any, entry_id: str) -> asyncio.Lock:
    """Return the one entry-wide config-options read/modify/write lock."""

    state = hass.data.setdefault("nitrado_gameserver_entry_option_locks", {})
    lock = state.get(str(entry_id))
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        state[str(entry_id)] = lock
    return lock


def remove_entry_option_update_lock(hass: Any, entry_id: str) -> None:
    """Remove one idle entry lock and prune the domain-private lock map."""

    state = hass.data.get("nitrado_gameserver_entry_option_locks")
    if not isinstance(state, dict):
        return
    lock = state.get(str(entry_id))
    if isinstance(lock, asyncio.Lock) and lock.locked():
        raise RuntimeError("config-entry option update is still active")
    state.pop(str(entry_id), None)
    if not state:
        hass.data.pop("nitrado_gameserver_entry_option_locks", None)


_MAX_PROFILE_OPTION_STRING_LENGTH = 16384


def service_id_set(options: dict[str, Any], key: str) -> set[str]:
    """Return a normalized service-id set from config entry options."""

    values = options.get(key, [])
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if str(value).isdigit()}


def default_service_options() -> dict[str, Any]:
    """Return the complete default config-entry option shape."""

    return {
        CONF_DISCOVERY_MODE: DEFAULT_DISCOVERY_MODE,
        CONF_DISCOVERY_INTERVAL: DEFAULT_DISCOVERY_INTERVAL,
        CONF_STATUS_INTERVAL: DEFAULT_STATUS_INTERVAL,
        CONF_MISSING_SERVICE_THRESHOLD: DEFAULT_MISSING_SERVICE_THRESHOLD,
        CONF_SETTLE_SECONDS: DEFAULT_SETTLE_SECONDS,
        CONF_IMPORTED_SERVICE_IDS: [],
        CONF_IGNORED_SERVICE_IDS: [],
        CONF_SERVICE_AREA_IDS: {},
        CONF_SERVICE_DISPLAY_NAMES: {},
        CONF_IDLE_SHUTDOWN_SERVICE_IDS: [],
        CONF_MAINTENANCE_SERVICE_IDS: [],
        CONF_DRY_RUN_SERVICE_IDS: [],
        CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS: [],
        CONF_IDLE_MINUTES: {},
        CONF_STARTUP_COOLDOWN_MINUTES: {},
        CONF_PROFILE_OPTIONS: {},
        CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS: {},
    }


def normalized_service_options(options: dict[str, Any]) -> dict[str, Any]:
    """Return config entry options with all runtime service option keys present."""

    updated = {**default_service_options(), **dict(options)}
    if updated.get(CONF_DISCOVERY_MODE) not in DISCOVERY_MODES:
        updated[CONF_DISCOVERY_MODE] = DEFAULT_DISCOVERY_MODE
    for key, (default, minimum, maximum) in _SCALAR_LIMITS.items():
        updated[key] = _normalized_int(updated.get(key), default=default, minimum=minimum, maximum=maximum)
    for key in (
        CONF_IMPORTED_SERVICE_IDS,
        CONF_IGNORED_SERVICE_IDS,
        CONF_IDLE_SHUTDOWN_SERVICE_IDS,
        CONF_MAINTENANCE_SERVICE_IDS,
        CONF_DRY_RUN_SERVICE_IDS,
        CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    ):
        updated[key] = sorted(service_id_set(updated, key))
    updated[CONF_SERVICE_AREA_IDS] = service_area_id_map(updated)
    updated[CONF_SERVICE_DISPLAY_NAMES] = service_display_name_map(updated)
    updated[CONF_IDLE_MINUTES] = idle_minutes_map(updated)
    updated[CONF_STARTUP_COOLDOWN_MINUTES] = startup_cooldown_minutes_map(updated)
    updated[CONF_PROFILE_OPTIONS] = profile_options_map(updated)
    updated[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS] = profile_option_acknowledgements_map(updated)
    return updated


def updated_service_options(
    options: dict[str, Any],
    *,
    import_service_id: str | None = None,
    ignore_service_id: str | None = None,
    remove_service_id: str | None = None,
    service_area_id: str | None = None,
    service_display_name: str | None = None,
) -> dict[str, Any]:
    """Return options updated for a service import/ignore/remove choice."""

    updated = normalized_service_options(options)
    imported = service_id_set(updated, CONF_IMPORTED_SERVICE_IDS)
    ignored = service_id_set(updated, CONF_IGNORED_SERVICE_IDS)
    areas = service_area_id_map(updated)
    names = service_display_name_map(updated)

    if import_service_id:
        imported.add(str(import_service_id))
        ignored.discard(str(import_service_id))
        if service_area_id and service_area_id.strip():
            areas[str(import_service_id)] = service_area_id.strip()
        if service_display_name and service_display_name.strip():
            names[str(import_service_id)] = service_display_name.strip()
    if ignore_service_id:
        ignored.add(str(ignore_service_id))
        imported.discard(str(ignore_service_id))
    if remove_service_id:
        imported.discard(str(remove_service_id))
        # A visible service must remain explicitly excluded after removal.
        # Otherwise discovery_mode=auto_add recreates it during the reload and
        # the old handler can then delete registry entries for the new runtime.
        ignored.add(str(remove_service_id))
        areas.pop(str(remove_service_id), None)
        names.pop(str(remove_service_id), None)

    updated[CONF_IMPORTED_SERVICE_IDS] = sorted(imported)
    updated[CONF_IGNORED_SERVICE_IDS] = sorted(ignored)
    updated[CONF_SERVICE_AREA_IDS] = areas
    updated[CONF_SERVICE_DISPLAY_NAMES] = names
    if ignore_service_id or remove_service_id:
        updated = clear_service_runtime_options(
            updated,
            str(ignore_service_id or remove_service_id),
        )
    return updated


def service_number_map(options: dict[str, Any], key: str, default: float) -> dict[str, float]:
    """Return normalized per-service numeric option overrides."""

    values = options.get(key, {})
    if not isinstance(values, dict):
        return {}
    normalized: dict[str, float] = {}
    for service_id, value in values.items():
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        minimum, maximum = _service_number_limits(key)
        if str(service_id).isdigit() and minimum <= parsed <= maximum:
            normalized[str(service_id)] = parsed
    return normalized


def idle_minutes_map(options: dict[str, Any]) -> dict[str, float]:
    """Return per-service idle shutdown minute overrides."""

    return service_number_map(options, CONF_IDLE_MINUTES, DEFAULT_IDLE_MINUTES)


def startup_cooldown_minutes_map(options: dict[str, Any]) -> dict[str, float]:
    """Return per-service startup cooldown minute overrides."""

    return service_number_map(options, CONF_STARTUP_COOLDOWN_MINUTES, DEFAULT_STARTUP_COOLDOWN_MINUTES)


def updated_idle_toggle_options(options: dict[str, Any], service_id: str, key: str, enabled: bool) -> dict[str, Any]:
    """Return options with one per-service idle-control toggle updated."""

    updated = normalized_service_options(options)
    service_ids = service_id_set(updated, key)
    if enabled:
        service_ids.add(str(service_id))
    else:
        service_ids.discard(str(service_id))
    updated[key] = sorted(service_ids)
    return updated


def updated_idle_number_options(options: dict[str, Any], service_id: str, key: str, value: float) -> dict[str, Any]:
    """Return options with one per-service idle-control number updated."""

    updated = normalized_service_options(options)
    values = service_number_map(updated, key, 0)
    values[str(service_id)] = float(value)
    updated[key] = values
    return updated


def clear_service_runtime_options(options: dict[str, Any], service_id: str) -> dict[str, Any]:
    """Return options with all per-service runtime controls removed."""

    updated = normalized_service_options(options)
    for key in (
        CONF_IDLE_SHUTDOWN_SERVICE_IDS,
        CONF_MAINTENANCE_SERVICE_IDS,
        CONF_DRY_RUN_SERVICE_IDS,
        CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    ):
        values = service_id_set(updated, key)
        values.discard(str(service_id))
        updated[key] = sorted(values)
    for key in (CONF_IDLE_MINUTES, CONF_STARTUP_COOLDOWN_MINUTES):
        values = service_number_map(updated, key, 0)
        values.pop(str(service_id), None)
        updated[key] = values
    profile_values = profile_options_map(updated)
    acknowledgement_values = profile_option_acknowledgements_map(updated)
    for profile_id in tuple(profile_values):
        for option_key in tuple(profile_values[profile_id]):
            profile_values[profile_id][option_key].pop(str(service_id), None)
            if not profile_values[profile_id][option_key]:
                profile_values[profile_id].pop(option_key, None)
        if not profile_values[profile_id]:
            profile_values.pop(profile_id, None)
    for profile_id in tuple(acknowledgement_values):
        for option_key in tuple(acknowledgement_values[profile_id]):
            acknowledgement_values[profile_id][option_key].pop(str(service_id), None)
            if not acknowledgement_values[profile_id][option_key]:
                acknowledgement_values[profile_id].pop(option_key, None)
        if not acknowledgement_values[profile_id]:
            acknowledgement_values.pop(profile_id, None)
    updated[CONF_PROFILE_OPTIONS] = profile_values
    updated[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS] = acknowledgement_values
    return updated


def profile_options_map(options: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """Return normalized namespaced per-profile, per-service option values."""

    raw = options.get(CONF_PROFILE_OPTIONS, {})
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, dict[str, Any]]] = {}
    for profile_id, profile_values in raw.items():
        if not isinstance(profile_id, str) or not isinstance(profile_values, dict):
            continue
        for option_key, service_values in profile_values.items():
            if not isinstance(option_key, str) or not isinstance(service_values, dict):
                continue
            safe_values = {}
            for service_id, value in service_values.items():
                if not str(service_id).isdigit() or not _profile_option_value_is_safe(value):
                    continue
                safe_values[str(service_id)] = value
            if safe_values:
                normalized.setdefault(profile_id, {})[option_key] = safe_values
    return normalized


def profile_option_value(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
    default: Any = None,
) -> Any:
    """Return one persisted profile option without exposing core option keys."""

    return profile_options_map(options).get(profile_id, {}).get(option_key, {}).get(str(service_id), default)


def profile_option_is_configured(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
) -> bool:
    """Return whether an administrator explicitly persisted one profile option."""

    return str(service_id) in profile_options_map(options).get(profile_id, {}).get(option_key, {})


def profile_option_acknowledgements_map(options: dict[str, Any]) -> dict[str, dict[str, dict[str, int]]]:
    """Return normalized acknowledgement revisions for profile options."""

    raw = options.get(CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS, {})
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, dict[str, int]]] = {}
    for profile_id, profile_values in raw.items():
        if not isinstance(profile_id, str) or not isinstance(profile_values, dict):
            continue
        for option_key, service_values in profile_values.items():
            if not isinstance(option_key, str) or not isinstance(service_values, dict):
                continue
            for service_id, revision in service_values.items():
                if str(service_id).isdigit() and type(revision) is int and 1 <= revision <= 1_000_000:
                    normalized.setdefault(profile_id, {}).setdefault(option_key, {})[str(service_id)] = revision
    return normalized


def profile_option_acknowledged(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
    revision: int,
) -> bool:
    """Return whether the current requirement revision was acknowledged.

    Explicit legacy values count as revision-one acknowledgement so an upgrade
    does not silently invalidate a choice the administrator already made.
    """

    stored = (
        profile_option_acknowledgements_map(options).get(profile_id, {}).get(option_key, {}).get(str(service_id), 0)
    )
    if stored >= revision:
        return True
    return revision == 1 and profile_option_is_configured(options, profile_id, option_key, service_id)


def updated_profile_option_acknowledgement_options(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
    revision: int,
) -> dict[str, Any]:
    """Record one explicit versioned administrator acknowledgement."""

    if type(revision) is not int or revision < 1:
        raise ValueError("acknowledgement revision must be a positive integer")
    updated = normalized_service_options(options)
    values = profile_option_acknowledgements_map(updated)
    values.setdefault(profile_id, {}).setdefault(option_key, {})[str(service_id)] = revision
    updated[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS] = values
    return updated


def cleared_profile_option_acknowledgement_options(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
) -> dict[str, Any]:
    """Clear one stored acknowledgement when its setting is reset."""

    updated = normalized_service_options(options)
    values = profile_option_acknowledgements_map(updated)
    profile_values = values.get(profile_id)
    if profile_values is not None:
        service_values = profile_values.get(option_key)
        if service_values is not None:
            service_values.pop(str(service_id), None)
            if not service_values:
                profile_values.pop(option_key, None)
        if not profile_values:
            values.pop(profile_id, None)
    updated[CONF_PROFILE_OPTION_ACKNOWLEDGEMENTS] = values
    return updated


def updated_profile_option_options(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
    value: str | int | float | bool,
) -> dict[str, Any]:
    """Return options with one namespaced profile value updated."""

    updated = normalized_service_options(options)
    values = profile_options_map(updated)
    values.setdefault(profile_id, {}).setdefault(option_key, {})[str(service_id)] = value
    updated[CONF_PROFILE_OPTIONS] = values
    return updated


def cleared_profile_option_options(
    options: dict[str, Any],
    profile_id: str,
    option_key: str,
    service_id: str,
) -> dict[str, Any]:
    """Return options with one namespaced profile value removed."""

    updated = normalized_service_options(options)
    values = profile_options_map(updated)
    profile_values = values.get(profile_id)
    if profile_values is not None:
        service_values = profile_values.get(option_key)
        if service_values is not None:
            service_values.pop(str(service_id), None)
            if not service_values:
                profile_values.pop(option_key, None)
        if not profile_values:
            values.pop(profile_id, None)
    updated[CONF_PROFILE_OPTIONS] = values
    return updated


def service_area_id_map(options: dict[str, Any]) -> dict[str, str]:
    """Return normalized per-service area-id overrides."""

    values = options.get(CONF_SERVICE_AREA_IDS, {})
    if not isinstance(values, dict):
        return {}
    return {
        str(service_id): str(area_id).strip()
        for service_id, area_id in values.items()
        if str(service_id).isdigit() and isinstance(area_id, str) and area_id.strip()
    }


def service_display_name_map(options: dict[str, Any]) -> dict[str, str]:
    """Return normalized per-service display-name overrides."""

    values = options.get(CONF_SERVICE_DISPLAY_NAMES, {})
    if not isinstance(values, dict):
        return {}
    return {
        str(service_id): str(name).strip()
        for service_id, name in values.items()
        if str(service_id).isdigit() and isinstance(name, str) and name.strip()
    }


def _normalized_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Return one persisted scalar as a safe bounded integer."""

    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if isinstance(value, float) and not value.is_integer():
        return default
    if not minimum <= parsed <= maximum:
        return default
    return parsed


def _service_number_limits(key: str) -> tuple[float, float]:
    if key == CONF_IDLE_MINUTES:
        return (3.0, 120.0)
    if key == CONF_STARTUP_COOLDOWN_MINUTES:
        return (1.0, 60.0)
    return (0.0, float("inf"))


def _profile_option_value_is_safe(value: Any) -> bool:
    """Return whether a persisted profile option is JSON/HA-state safe."""

    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= _MAX_PROFILE_OPTION_STRING_LENGTH
    if isinstance(value, int):
        try:
            return math.isfinite(float(value))
        except OverflowError:
            return False
    if isinstance(value, float):
        return math.isfinite(value)
    return False
