"""Home Assistant registry cleanup helpers."""

from __future__ import annotations

from typing import Any

from .const import DOMAIN
from .models import ServiceIdentity


def async_remove_service_registry_entries(
    hass: Any,
    service_id: str,
    account_entry_id: str | None = None,
) -> None:
    """Remove HA entity/device registry entries owned by one Nitrado service."""

    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
    except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.
        return

    identity = ServiceIdentity(str(service_id), str(account_entry_id) if account_entry_id else None)
    entity_registry = er.async_get(hass)
    for entity in tuple(entity_registry.entities.values()):
        if getattr(entity, "platform", None) != DOMAIN:
            continue
        unique_id = getattr(entity, "unique_id", "")
        if is_service_entity_unique_id(unique_id, service_id, account_entry_id):
            entity_registry.async_remove(entity.entity_id)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={identity.device_identifier})
    if device is not None:
        device_registry.async_remove_device(device.id)


def async_remove_entry_registry_entries(hass: Any, entry: Any) -> None:
    """Remove HA registry entries tied to an integration config entry."""

    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
    except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.
        return

    entity_registry = er.async_get(hass)
    for entity in tuple(entity_registry.entities.values()):
        if getattr(entity, "config_entry_id", None) == entry.entry_id and getattr(entity, "platform", None) == DOMAIN:
            entity_registry.async_remove(entity.entity_id)

    device_registry = dr.async_get(hass)
    for device in tuple(device_registry.devices.values()):
        config_entries = getattr(device, "config_entries", set())
        identifiers = getattr(device, "identifiers", set())
        if entry.entry_id in config_entries and any(identifier[0] == DOMAIN for identifier in identifiers):
            device_registry.async_remove_device(device.id)


def async_clear_entry_issues(hass: Any, entry: Any) -> None:
    """Clear integration-owned Repairs issues that belong to a config entry."""

    try:
        from homeassistant.helpers import issue_registry as ir
    except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.
        return

    issue_prefix = f"{entry.entry_id}_"
    registry = ir.async_get(hass)
    for domain, issue_id in tuple(registry.issues):
        if domain == DOMAIN and issue_id.startswith(issue_prefix):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def async_clear_service_issue(hass: Any, entry: Any, service_id: str) -> None:
    """Clear Repairs issue for a service that was removed by the user."""

    try:
        from homeassistant.helpers import issue_registry as ir
    except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.
        return

    ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_missing_service_{service_id}")


def is_service_entity_unique_id(
    unique_id: Any,
    service_id: str,
    account_entry_id: str | None = None,
) -> bool:
    """Return true for any entity unique ID owned by one Nitrado service."""

    prefix = f"account:{account_entry_id}:service:{service_id}:" if account_entry_id else f"service:{service_id}:"
    return isinstance(unique_id, str) and unique_id.startswith(prefix)
