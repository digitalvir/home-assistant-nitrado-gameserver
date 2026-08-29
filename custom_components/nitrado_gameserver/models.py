"""Pure models for Nitrado Game Server runtime state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .const import DOMAIN


@dataclass(slots=True, frozen=True)
class ServiceIdentity:
    """Stable Home Assistant identity for one Nitrado service."""

    service_id: str
    account_entry_id: str | None = None

    @property
    def device_identifier(self) -> tuple[str, str]:
        """Return HA device identifier."""

        if self.account_entry_id:
            return (DOMAIN, f"account:{self.account_entry_id}:service:{self.service_id}")
        return (DOMAIN, f"service:{self.service_id}")

    def entity_unique_id(self, entity_key: str, *, profile_id: str | None = None) -> str:
        """Return stable entity unique ID."""

        prefix = (
            f"account:{self.account_entry_id}:service:{self.service_id}"
            if self.account_entry_id
            else f"service:{self.service_id}"
        )
        if profile_id:
            return f"{prefix}:profile:{profile_id}:{entity_key}"
        return f"{prefix}:{entity_key}"


@dataclass(slots=True)
class ManagedServiceState:
    """Minimal reconciliation state for one managed service."""

    service_id: str
    account_entry_id: str | None = None
    missing_count: int = 0
    ignored: bool = False
    profile_id: str | None = None
    pending_discovery: bool = False
    available: bool = True

    @property
    def identity(self) -> ServiceIdentity:
        """Return stable service identity."""

        return ServiceIdentity(self.service_id, self.account_entry_id)


@dataclass(slots=True)
class DiscoveryReconcileResult:
    """Result of reconciling discovered services with managed services."""

    managed: dict[str, ManagedServiceState] = field(default_factory=dict)
    newly_discovered: set[str] = field(default_factory=set)
    newly_missing: set[str] = field(default_factory=set)
    recovered: set[str] = field(default_factory=set)
    removable_notice_services: set[str] = field(default_factory=set)


def reconcile_services(
    *,
    known: dict[str, ManagedServiceState],
    discovered_service_ids: Iterable[str],
    missing_threshold: int,
    auto_add: bool,
    imported_service_ids: Iterable[str] = (),
    ignored_service_ids: Iterable[str] = (),
) -> DiscoveryReconcileResult:
    """Reconcile known service states with the latest successful account discovery."""

    discovered = {str(service_id) for service_id in discovered_service_ids}
    imported = {str(service_id) for service_id in imported_service_ids}
    ignored = {str(service_id) for service_id in ignored_service_ids}
    result = DiscoveryReconcileResult(managed={key: replace(value) for key, value in known.items()})

    for service_id in sorted(discovered):
        state = result.managed.get(service_id)
        if state is None:
            is_ignored = service_id in ignored
            is_imported = service_id in imported
            pending_discovery = not (auto_add or is_imported or is_ignored)
            state = ManagedServiceState(
                service_id=service_id,
                ignored=is_ignored,
                pending_discovery=pending_discovery,
            )
            result.managed[service_id] = state
            if pending_discovery:
                result.newly_discovered.add(service_id)
            continue
        state.ignored = service_id in ignored
        if state.ignored:
            state.pending_discovery = False
            state.available = True
            state.missing_count = 0
            continue
        if state.missing_count:
            result.recovered.add(service_id)
        state.missing_count = 0
        state.available = True
        if auto_add or service_id in imported:
            state.pending_discovery = False

    for service_id, state in result.managed.items():
        if state.ignored or service_id in discovered or state.pending_discovery:
            continue
        state.missing_count += 1
        if state.missing_count == missing_threshold:
            result.newly_missing.add(service_id)
        if state.missing_count >= missing_threshold:
            state.available = False
            result.removable_notice_services.add(service_id)

    return result
