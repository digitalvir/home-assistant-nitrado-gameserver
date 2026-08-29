"""Home Assistant ownership for the versioned provider connector API.

Companion integrations receive typed, revocable capabilities.  They never
receive a Nitrado client, filesystem object, FTP credential, or mutation lock.
This module is deliberately integration-private; companions import only
``provider_api``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .backups import (
    NativeBackupIdentity,
    NativeBackupInventory,
    NativeBackupRestoreObservation,
    NativeBackupRestoreRequest,
    NativeBackupRestoreResult,
    NitradoNativeBackupService,
)
from .const import (
    BLOCKED_STATUSES,
    DOMAIN,
    FILESYSTEM_TREE_REPLACE_ENABLED,
    NATIVE_BACKUP_RESTORE_ENABLED,
    RUNNING_STATUS,
    STOPPED_STATUS,
)
from .filesystem import FileManifestEntry, FileTreeManifest, SpoolTreeFiles
from .ha_native_backup_storage import HomeAssistantNativeBackupJournalStore
from .native_backup_journal import NativeBackupRestoreJournal, NativeRestoreFact
from .operation_journal import (
    HomeAssistantOperationJournalStore,
    OperationIntent,
    OperationJournal,
)
from .provider_api import (
    ProviderApiError,
    ProviderAuthorizationError,
    ProviderCapabilities,
    ProviderConnectorRegistry,
    ProviderFileContent,
    ProviderFileReadRequest,
    ProviderGrant,
    ProviderGrantRequest,
    ProviderMutationApproval,
    ProviderMutationPlan,
    ProviderScope,
    ProviderServiceRef,
    ProviderTreeContentChunk,
    ProviderTreeContentHandle,
    ProviderTreeFile,
    ProviderTreeManifest,
    ProviderTreeManifestEntry,
    ProviderTreeReplaceRequest,
    ProviderTreeReplaceResult,
    ProviderTreeRequest,
    ProviderTreeSnapshot,
    ProviderTreeVerifyRequest,
    ProviderTreeVerifyResult,
    install_provider_connector_registry,
    remove_provider_connector_registry,
)

_LOGGER = logging.getLogger(__name__)
_RUNTIME_DATA_KEY = f"{DOMAIN}_provider_runtime"
_GRANT_STORE_KEY = f"{DOMAIN}.provider_grants"
_GRANT_STORE_VERSION = 1
_MAX_PERSISTED_GRANTS = 256
_NATIVE_RESTORE_OBSERVATION_TIMEOUT = 300.0
_NATIVE_RESTORE_POLL_INTERVAL = 2.0
_CAPABILITY_PROBE_CACHE_SECONDS = 300.0
_CAPABILITY_PROBE_BACKOFF_MAX_SECONDS = 60.0
_FILESYSTEM_SCOPES = frozenset(
    {
        ProviderScope.FILESYSTEM_READ,
        ProviderScope.FILESYSTEM_SNAPSHOT,
        ProviderScope.FILESYSTEM_REPLACE,
    }
)
_DISABLED_PROVIDER_SCOPES = frozenset(
    scope
    for scope, enabled in (
        (ProviderScope.FILESYSTEM_REPLACE, FILESYSTEM_TREE_REPLACE_ENABLED),
        (ProviderScope.NATIVE_BACKUP_RESTORE, NATIVE_BACKUP_RESTORE_ENABLED),
    )
    if not enabled
)


@dataclass(frozen=True, slots=True)
class StoredProviderGrant:
    """Persistent exact authority for one companion and provider service."""

    grant_id: str
    consumer_domain: str
    consumer_entry_id: str
    service_ref: ProviderServiceRef
    scopes: frozenset[ProviderScope]
    permitted_roots: frozenset[str]

    @classmethod
    def from_grant(cls, grant: ProviderGrant) -> StoredProviderGrant:
        return cls(
            grant.grant_id,
            grant.consumer_domain,
            grant.consumer_entry_id,
            grant.service_ref,
            grant.scopes,
            grant.permitted_roots,
        )

    def as_grant(self) -> ProviderGrant:
        return ProviderGrant(
            self.grant_id,
            self.consumer_domain,
            self.consumer_entry_id,
            self.service_ref,
            self.scopes,
            permitted_roots=self.permitted_roots,
        )

    def as_storage(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "consumer_domain": self.consumer_domain,
            "consumer_entry_id": self.consumer_entry_id,
            "account_entry_id": self.service_ref.account_entry_id,
            "service_id": self.service_ref.service_id,
            "scopes": sorted(scope.value for scope in self.scopes),
            "permitted_roots": sorted(self.permitted_roots),
        }

    @classmethod
    def from_storage(cls, value: Any) -> StoredProviderGrant:
        if not isinstance(value, dict):
            raise ValueError("provider grant is malformed")
        grant = ProviderGrant(
            str(value["grant_id"]),
            str(value["consumer_domain"]),
            str(value["consumer_entry_id"]),
            ProviderServiceRef(str(value["account_entry_id"]), str(value["service_id"])),
            frozenset(ProviderScope(str(item)) for item in value["scopes"]),
            permitted_roots=frozenset(str(item) for item in value.get("permitted_roots", ())),
        )
        return cls.from_grant(grant)


@dataclass(frozen=True, slots=True)
class _ApprovalRecord:
    plan_id: str
    payload_digest: str
    expires_at: float


class HomeAssistantProviderAuthority:
    """Persist exact grants and consume ephemeral mutation approvals."""

    def __init__(self, hass: Any) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, _GRANT_STORE_VERSION, _GRANT_STORE_KEY)
        self._grants: dict[str, StoredProviderGrant] = {}
        self._approvals: dict[str, _ApprovalRecord] = {}
        self._lock = asyncio.Lock()
        self.storage_error: str | None = None

    async def async_initialize(self) -> None:
        payload = await self._store.async_load()
        if payload is None:
            return
        try:
            raw_grants = payload.get("grants") if isinstance(payload, dict) else None
            if not isinstance(raw_grants, list) or len(raw_grants) > _MAX_PERSISTED_GRANTS:
                raise ValueError("provider grant store has an invalid shape")
            loaded = tuple(StoredProviderGrant.from_storage(item) for item in raw_grants)
            for stored in loaded:
                _validate_grant_shape(stored)
            if len({item.grant_id for item in loaded}) != len(loaded):
                raise ValueError("provider grant store contains duplicate IDs")
        except (KeyError, TypeError, ValueError):
            # Corrupt authorization state must never broaden access. Keep the
            # store untouched for administrator recovery, but load no grants.
            _LOGGER.error("Nitrado provider grant storage is invalid; all companion access is denied")
            self._grants = {}
            self.storage_error = "Persistent provider grant storage is invalid; all companion access is denied."
            return
        self._grants = {item.grant_id: item for item in loaded}
        self.storage_error = None

    async def async_authorize_grant(self, request: ProviderGrantRequest) -> ProviderGrant:
        async with self._lock:
            candidates = (
                stored
                for stored in self._grants.values()
                if stored.consumer_domain == request.consumer_domain
                and stored.consumer_entry_id == request.consumer_entry_id
                and stored.service_ref == request.service_ref
                and request.requested_scopes <= stored.scopes
                and request.requested_roots <= stored.permitted_roots
            )
            stored = next(candidates, None)
            if stored is None:
                raise ProviderAuthorizationError(
                    "No persistent Nitrado provider grant matches this exact companion, service, scope, and root"
                )
            return stored.as_grant()

    async def async_put_grant(self, grant: ProviderGrant) -> None:
        """Persist one already authenticated, exact administrator grant."""

        stored = StoredProviderGrant.from_grant(grant)
        _validate_grant_shape(stored)
        async with self._lock:
            if stored.grant_id not in self._grants and len(self._grants) >= _MAX_PERSISTED_GRANTS:
                raise ProviderAuthorizationError("Persistent provider grant limit reached")
            updated = {**self._grants, stored.grant_id: stored}
            await self._async_save_grants_locked(updated)
            self._grants = updated
            self.storage_error = None

    async def async_delete_grant(self, grant_id: str) -> bool:
        async with self._lock:
            removed = str(grant_id) in self._grants
            if removed:
                updated = {key: value for key, value in self._grants.items() if key != str(grant_id)}
                await self._async_save_grants_locked(updated)
                self._grants = updated
            return removed

    async def async_delete_account_grants(self, account_entry_id: str) -> None:
        async with self._lock:
            if self.storage_error is not None:
                await _await_despite_cancellation(
                    self._store.async_remove(),
                    name="nitrado-provider-grant-remove-corrupt",
                )
                self._grants = {}
                self.storage_error = None
                return
            retained = {
                key: grant
                for key, grant in self._grants.items()
                if grant.service_ref.account_entry_id != str(account_entry_id)
            }
            if len(retained) != len(self._grants):
                await self._async_save_grants_locked(retained)
                self._grants = retained

    async def async_delete_service_grants(self, service_ref: ProviderServiceRef) -> None:
        """Delete authority when a service is permanently removed."""

        async with self._lock:
            retained = {key: grant for key, grant in self._grants.items() if grant.service_ref != service_ref}
            if len(retained) != len(self._grants):
                await self._async_save_grants_locked(retained)
                self._grants = retained

    def grants(self) -> tuple[StoredProviderGrant, ...]:
        return tuple(sorted(self._grants.values(), key=lambda item: item.grant_id))

    async def async_issue_approval(
        self,
        registry: ProviderConnectorRegistry,
        plan_id: str,
        payload_digest: str,
    ) -> ProviderMutationApproval:
        """Issue a one-shot token only for a registry-owned active plan."""

        plan = registry.get_pending_plan(plan_id, payload_digest)
        token = secrets.token_urlsafe(32)
        async with self._lock:
            self._prune_approvals_locked()
            self._approvals[token] = _ApprovalRecord(
                plan.plan_id,
                plan.payload_digest,
                plan.expires_at,
            )
        return ProviderMutationApproval(plan.plan_id, plan.payload_digest, token)

    async def async_authorize_approval(
        self,
        plan: ProviderMutationPlan,
        approval: ProviderMutationApproval,
    ) -> bool:
        """Atomically consume one exact, unexpired core-issued approval."""

        async with self._lock:
            self._prune_approvals_locked()
            record = self._approvals.pop(approval.approval_token, None)
            return bool(
                record is not None
                and record.plan_id == plan.plan_id == approval.plan_id
                and record.payload_digest == plan.payload_digest == approval.payload_digest
                and time.time() < min(record.expires_at, plan.expires_at)
            )

    def _prune_approvals_locked(self) -> None:
        now = time.time()
        self._approvals = {token: record for token, record in self._approvals.items() if now < record.expires_at}

    async def _async_save_grants_locked(self, grants: dict[str, StoredProviderGrant]) -> None:
        if not grants:
            await _await_despite_cancellation(
                self._store.async_remove(),
                name="nitrado-provider-grant-remove-empty",
            )
            return
        payload = {"grants": [grant.as_storage() for grant in sorted(grants.values(), key=lambda item: item.grant_id)]}
        await _await_despite_cancellation(
            self._store.async_save(payload),
            name="nitrado-provider-grant-save",
        )


class HomeAssistantProviderBackend:
    """Exact account/service backend implementing the private core protocol."""

    capabilities = ProviderCapabilities(frozenset(ProviderScope))

    def __init__(
        self,
        hass: Any,
        service_ref: ProviderServiceRef,
        coordinator: Any,
        native_backup_journal: NativeBackupRestoreJournal,
        owner: HomeAssistantProviderRuntime,
    ) -> None:
        self._hass = hass
        self.service_ref = service_ref
        self._coordinator = coordinator
        self._native_backup_journal = native_backup_journal
        self._owner = owner

    def _active_coordinator(self) -> Any:
        coordinator = self._hass.data.get(DOMAIN, {}).get(self.service_ref.account_entry_id)
        if (
            coordinator is not self._coordinator
            or bool(getattr(coordinator, "shutting_down", True))
            or self.service_ref.service_id not in getattr(coordinator, "services", {})
        ):
            raise ProviderApiError("The exact Nitrado account service is no longer active")
        return coordinator

    async def async_validate_scopes(self, scopes: frozenset[ProviderScope]) -> None:
        """Prove transport-dependent scopes before issuing live authority."""

        coordinator = self._active_coordinator()
        await self._owner.async_validate_scopes(self.service_ref, coordinator, scopes)
        self._active_coordinator()

    async def async_read_file(self, request: ProviderFileReadRequest) -> ProviderFileContent:
        coordinator = self._active_coordinator()
        async with coordinator.profile_transport.authoritative_read(self.service_ref.service_id):
            content = await coordinator.profile_transport.read_bytes(
                self.service_ref.service_id,
                request.path,
                max_bytes=request.max_bytes,
                require_ftp=True,
            )
        self._active_coordinator()
        return ProviderFileContent(
            self.service_ref,
            request.path,
            content,
            ProviderTreeFile.from_bytes(request.path, content).sha256,
        )

    async def async_snapshot_tree(self, request: ProviderTreeRequest) -> ProviderTreeSnapshot:
        coordinator = self._active_coordinator()
        async with coordinator.profile_transport.authoritative_read(self.service_ref.service_id):
            snapshot = await coordinator.profile_transport.snapshot_tree(
                self.service_ref.service_id,
                request.root,
                max_files=request.max_files,
                max_bytes=request.max_bytes,
                max_depth=request.max_depth,
            )
        try:
            self._active_coordinator()
            manifest = ProviderTreeManifest.from_entries(
                ProviderTreeManifestEntry(item.path, item.size, item.sha256) for item in snapshot.manifest.entries
            )

            async def chunks(chunk_bytes: int):
                for entry in snapshot.manifest.entries:
                    offset = 0
                    while offset < entry.size:
                        if isinstance(snapshot.files, SpoolTreeFiles):
                            content = await asyncio.to_thread(
                                snapshot.files.read_chunk,
                                entry.path,
                                offset,
                                chunk_bytes,
                            )
                        else:
                            content = snapshot.files[entry.path][offset : offset + chunk_bytes]
                        if not content:
                            raise ProviderApiError("Provider snapshot content ended unexpectedly")
                        next_offset = offset + len(content)
                        yield ProviderTreeContentChunk(
                            entry.path,
                            offset,
                            content,
                            next_offset == entry.size,
                        )
                        offset = next_offset
                    if entry.size == 0:
                        yield ProviderTreeContentChunk(entry.path, 0, b"", True)

            content = ProviderTreeContentHandle(manifest, chunks, snapshot.close)
            return ProviderTreeSnapshot(
                self.service_ref,
                request.root,
                manifest,
                content,
            )
        except BaseException:
            snapshot.close()
            raise

    async def async_verify_tree(self, request: ProviderTreeVerifyRequest) -> ProviderTreeVerifyResult:
        coordinator = self._active_coordinator()
        expected = _internal_manifest(request.expected)
        async with coordinator.profile_transport.authoritative_read(self.service_ref.service_id):
            await coordinator.profile_transport.verify_tree(
                self.service_ref.service_id,
                request.root,
                expected,
            )
        self._active_coordinator()
        return ProviderTreeVerifyResult(self.service_ref, request.root, request.expected)

    async def async_replace_tree(
        self,
        request: ProviderTreeReplaceRequest,
        *,
        pre_mutation_check: Callable[[], None],
        mutation_started: Callable[[], None],
    ) -> ProviderTreeReplaceResult:
        if not FILESYSTEM_TREE_REPLACE_ENABLED:
            raise ProviderAuthorizationError("Provider tree replacement is disabled in this release")
        coordinator = self._active_coordinator()
        service_id = self.service_ref.service_id
        intent = OperationIntent(
            "provider-tree-replace",
            "provider-tree",
            generation=request.proposed.digest,
            expected_evidence="filesystem journal verified the exact proposed tree or exact rollback",
        )
        async with coordinator.async_operation(service_id, intent) as reservation:
            pre_mutation_check()
            self._active_coordinator()
            coordinator.ensure_filesystem_mutation_allowed(service_id)

            async def require_stopped() -> None:
                pre_mutation_check()
                current = self._active_coordinator()
                runtime = await current.async_refresh_service(service_id, handle_idle_shutdown=False)
                if runtime.server is None or runtime.server.raw_status != STOPPED_STATUS:
                    raise ProviderApiError("Provider tree replacement requires a stably stopped server")

            await require_stopped()
            try:
                await reservation.async_mark_dispatched()
                result = await coordinator.profile_transport.replace_tree(
                    service_id,
                    request.root,
                    {item.path: item.content for item in request.files},
                    expected_current=_internal_manifest(request.expected_current),
                    require_stopped=require_stopped,
                    mutation_started=mutation_started,
                )
                await reservation.async_mark_verifying()
            finally:
                await coordinator.async_refresh_filesystem_recovery_facts()
                await self._owner.async_refresh_native_backup_repair(self.service_ref.account_entry_id)
        self._active_coordinator()
        return ProviderTreeReplaceResult(
            self.service_ref,
            request.root,
            request.proposed,
            result.transaction_id,
            True,
        )

    async def async_list_native_backups(self, *, limit: int) -> NativeBackupInventory:
        coordinator = self._active_coordinator()
        inventory = await self._native_backup_service(coordinator).async_list(limit=limit)
        self._active_coordinator()
        return inventory

    async def async_restore_native_backup(
        self,
        request: NativeBackupRestoreRequest,
        *,
        pre_mutation_check: Callable[[], None],
        mutation_started: Callable[[], None],
    ) -> NativeBackupRestoreResult:
        if not NATIVE_BACKUP_RESTORE_ENABLED:
            raise ProviderAuthorizationError("Native backup restore is disabled in this release")
        coordinator = self._active_coordinator()
        service_id = self.service_ref.service_id
        intent = OperationIntent(
            "native-backup-restore",
            f"backup-{request.identity.backup_id}",
            expected_evidence="Nitrado restore transition was observed and the native journal resolved",
        )
        try:
            async with coordinator.async_operation(service_id, intent) as reservation:
                pre_mutation_check()

                async def require_stopped() -> None:
                    pre_mutation_check()
                    current = self._active_coordinator()
                    runtime = await current.async_refresh_service(service_id, handle_idle_shutdown=False)
                    if runtime.server is None or runtime.server.raw_status != STOPPED_STATUS:
                        raise ProviderApiError("Native backup restore requires a stably stopped server")

                await require_stopped()
                await reservation.async_mark_dispatched()
                result = await self._native_backup_service(
                    coordinator,
                    pre_restore_check=require_stopped,
                    mutation_started=mutation_started,
                ).async_restore(request)
                await reservation.async_mark_verifying()
        finally:
            coordinator.profile_transport.invalidate_provider_state(service_id)
            with contextlib.suppress(Exception):
                await coordinator.async_refresh_service(service_id, handle_idle_shutdown=False)
            await self._owner.async_refresh_native_backup_repair(self.service_ref.account_entry_id)
        self._active_coordinator()
        return result

    def _native_backup_service(
        self,
        coordinator: Any,
        *,
        pre_restore_check: Callable[[], Awaitable[None]] | None = None,
        mutation_started: Callable[[], None] | None = None,
    ) -> NitradoNativeBackupService:
        return NitradoNativeBackupService(
            coordinator.client,
            self.service_ref.service_id,
            account_entry_id=self.service_ref.account_entry_id,
            journal=self._native_backup_journal,
            observer=_CoordinatorNativeRestoreObserver(self),
            pre_restore_check=pre_restore_check,
            mutation_started=mutation_started,
            observation_timeout=_NATIVE_RESTORE_OBSERVATION_TIMEOUT,
        )


class _CoordinatorNativeRestoreObserver:
    """Prove only that Nitrado entered and exited its restore transition."""

    def __init__(self, backend: HomeAssistantProviderBackend) -> None:
        self._backend = backend

    async def async_observe_native_restore(
        self,
        service_id: str,
        identity: NativeBackupIdentity,
    ) -> NativeBackupRestoreObservation:
        del identity  # Exact identity was verified before transport; status cannot prove game health.
        if str(service_id) != self._backend.service_ref.service_id:
            raise ProviderApiError("Native restore observer service binding changed")
        restore_started = False
        while True:
            coordinator = self._backend._active_coordinator()
            runtime = await coordinator.async_refresh_service(
                str(service_id),
                handle_idle_shutdown=False,
            )
            self._backend._active_coordinator()
            status = runtime.server.raw_status if runtime.server is not None else "unknown"
            if status == "backup_restore":
                restore_started = True
            elif restore_started and status in {RUNNING_STATUS, STOPPED_STATUS, *BLOCKED_STATUSES}:
                return NativeBackupRestoreObservation(
                    restore_started=True,
                    restore_finished=True,
                    provider_status=status,
                )
            await asyncio.sleep(_NATIVE_RESTORE_POLL_INTERVAL)


class HomeAssistantProviderRuntime:
    """One core-owned provider registry shared by every Nitrado account entry."""

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.authority = HomeAssistantProviderAuthority(hass)
        self.registry = ProviderConnectorRegistry(
            backend_resolver=self._async_resolve_backend,
            grant_authorizer=self.authority.async_authorize_grant,
            approval_authorizer=self.authority.async_authorize_approval,
            disabled_scopes=_DISABLED_PROVIDER_SCOPES,
        )
        self._initialized = False
        self._closed = False
        self._account_lock = asyncio.Lock()
        self._native_backup_journals: dict[str, NativeBackupRestoreJournal] = {}
        self._operation_journals: dict[str, OperationJournal] = {}
        self._account_coordinators: dict[str, Any] = {}
        self._capability_probe_locks: dict[ProviderServiceRef, asyncio.Lock] = {}
        self._capability_probe_success: dict[tuple[ProviderServiceRef, str], float] = {}
        self._capability_probe_failures: dict[tuple[ProviderServiceRef, str], tuple[int, float]] = {}

    async def async_initialize(self) -> None:
        if self._initialized:
            return
        await self.authority.async_initialize()
        install_provider_connector_registry(self.hass, self.registry)
        self._initialized = True

    async def _async_resolve_backend(
        self,
        service_ref: ProviderServiceRef,
    ) -> HomeAssistantProviderBackend:
        coordinator = self.hass.data.get(DOMAIN, {}).get(service_ref.account_entry_id)
        if (
            coordinator is None
            or not bool(getattr(coordinator, "setup_committed", True))
            or bool(getattr(coordinator, "shutting_down", True))
            or service_ref.service_id not in getattr(coordinator, "services", {})
        ):
            raise ProviderApiError("The exact Nitrado account entry does not manage this service")
        if str(getattr(coordinator, "account_entry_id", "")) != service_ref.account_entry_id:
            raise ProviderApiError("The Nitrado coordinator account binding does not match")
        journal = self._native_backup_journals.get(service_ref.account_entry_id)
        if journal is None:
            raise ProviderApiError("Durable native-backup tracking is not initialized for this account")
        return HomeAssistantProviderBackend(self.hass, service_ref, coordinator, journal, self)

    async def async_bind_account(self, account_entry_id: str, coordinator: Any | None = None) -> None:
        """Initialize one durable native-backup journal before connector use."""

        entry_id = str(account_entry_id)
        async with self._account_lock:
            if self._closed:
                raise ProviderApiError("The Nitrado provider runtime is closed")
            coordinator = coordinator or self.hass.data.get(DOMAIN, {}).get(entry_id)
            if coordinator is None or str(getattr(coordinator, "account_entry_id", "")) != entry_id:
                raise ProviderApiError("The exact Nitrado account coordinator is unavailable")
            if entry_id in self._native_backup_journals:
                operation_journal = self._operation_journals.get(entry_id)
                if operation_journal is None:
                    raise ProviderApiError("Durable operation tracking is not initialized for this account")
                await coordinator.async_bind_operation_journal(operation_journal)
                self._account_coordinators[entry_id] = coordinator
                return
            operation_journal = OperationJournal(HomeAssistantOperationJournalStore(self.hass, entry_id))
            await coordinator.async_bind_operation_journal(operation_journal)
            journal = NativeBackupRestoreJournal(HomeAssistantNativeBackupJournalStore(self.hass, entry_id))
            await journal.async_initialize()
            self._operation_journals[entry_id] = operation_journal
            self._native_backup_journals[entry_id] = journal
            self._account_coordinators[entry_id] = coordinator

    async def async_unbind_account(self, account_entry_id: str, *, coordinator: Any | None = None) -> bool:
        """Forget an unloaded account after its connector leases are drained."""

        async with self._account_lock:
            entry_id = str(account_entry_id)
            if coordinator is not None and self._account_coordinators.get(entry_id) is not coordinator:
                return False
            self._native_backup_journals.pop(entry_id, None)
            self._operation_journals.pop(entry_id, None)
            self._account_coordinators.pop(entry_id, None)
            return True

    async def async_native_backup_facts(
        self,
        account_entry_id: str,
    ) -> tuple[NativeRestoreFact, ...]:
        journal = self._native_backup_journals.get(str(account_entry_id))
        return await journal.async_unresolved_facts() if journal is not None else ()

    async def async_validate_scopes(
        self,
        service_ref: ProviderServiceRef,
        coordinator: Any,
        scopes: frozenset[ProviderScope],
    ) -> None:
        """Single-flight, cache, and back off provider capability probes."""

        disabled = scopes & _DISABLED_PROVIDER_SCOPES
        if disabled:
            names = ", ".join(sorted(scope.value for scope in disabled))
            raise ProviderAuthorizationError(f"Provider capability is disabled in this release: {names}")

        lock = self._capability_probe_locks.setdefault(service_ref, asyncio.Lock())
        async with lock:
            if scopes & _FILESYSTEM_SCOPES:
                await self._async_cached_capability_probe(
                    service_ref,
                    "ftp",
                    lambda: coordinator.profile_transport.ftp.capabilities(service_ref.service_id),
                )
            if scopes & {ProviderScope.NATIVE_BACKUP_READ, ProviderScope.NATIVE_BACKUP_RESTORE}:
                backup_service = NitradoNativeBackupService(coordinator.client, service_ref.service_id)
                await self._async_cached_capability_probe(
                    service_ref,
                    "native_backup_read",
                    lambda: backup_service.async_list(limit=1),
                )
            if ProviderScope.NATIVE_BACKUP_RESTORE in scopes:

                async def probe_restore() -> bool:
                    possible = await coordinator.client.native_backup_restore_possible(service_ref.service_id)
                    if not isinstance(possible, bool):
                        raise ProviderApiError("Nitrado returned invalid native-backup restore capability data")
                    return possible

                await self._async_cached_capability_probe(service_ref, "native_backup_restore", probe_restore)

    async def _async_cached_capability_probe(
        self,
        service_ref: ProviderServiceRef,
        capability: str,
        probe: Callable[[], Awaitable[Any]],
    ) -> None:
        key = (service_ref, capability)
        now = time.monotonic()
        proven_at = self._capability_probe_success.get(key)
        if proven_at is not None and now - proven_at < _CAPABILITY_PROBE_CACHE_SECONDS:
            return
        failure = self._capability_probe_failures.get(key)
        if failure is not None:
            count, failed_at = failure
            backoff = min(2 ** min(count - 1, 6), _CAPABILITY_PROBE_BACKOFF_MAX_SECONDS)
            if now - failed_at < backoff:
                raise ProviderApiError(f"Provider capability probe is temporarily backed off: {capability}")
        try:
            await probe()
        except BaseException:
            count = (failure[0] + 1) if failure is not None else 1
            self._capability_probe_failures[key] = (count, time.monotonic())
            raise
        self._capability_probe_success[key] = time.monotonic()
        self._capability_probe_failures.pop(key, None)

    async def async_native_backup_diagnostics(self, account_entry_id: str) -> dict[str, Any] | None:
        journal = self._native_backup_journals.get(str(account_entry_id))
        return await journal.async_diagnostics() if journal is not None else None

    async def async_acknowledge_native_backup_unknown(
        self,
        account_entry_id: str,
        operation_id: str,
    ) -> None:
        """Close an administrator-reviewed unknown outcome without calling it successful."""

        journal = self._native_backup_journals.get(str(account_entry_id))
        if journal is None:
            raise ProviderApiError("Native-backup tracking is not initialized for this account")
        await journal.async_acknowledge_unverified(str(operation_id))

    async def async_reset_corrupt_native_backup_journal(self, account_entry_id: str) -> None:
        """Quarantine corrupt metadata before an explicit administrator reset."""

        journal = self._native_backup_journals.get(str(account_entry_id))
        if journal is None:
            raise ProviderApiError("Native-backup tracking is not initialized for this account")
        await journal.async_quarantine_and_reset_corrupt()

    async def async_refresh_native_backup_repair(self, account_entry_id: str) -> None:
        """Refresh HA Repairs after a native restore changes durable facts."""

        coordinator = self.hass.data.get(DOMAIN, {}).get(str(account_entry_id))
        entry = self.hass.config_entries.async_get_entry(str(account_entry_id))
        if coordinator is None or entry is None:
            return
        from .repairs import async_update_repair_issues

        await async_update_repair_issues(self.hass, entry, coordinator)

    async def async_delete_grant(self, grant_id: str) -> bool:
        """Remove persistent authority, then revoke/drain every derived capability."""

        exact_grant_id = str(grant_id)
        deleted = await self.authority.async_delete_grant(exact_grant_id)
        await self.registry.async_revoke_grant(exact_grant_id)
        return deleted

    async def async_invalidate_service(self, account_entry_id: str, service_id: str) -> None:
        service_ref = ProviderServiceRef(str(account_entry_id), str(service_id))
        await self.registry.async_invalidate_service(service_ref)
        self._capability_probe_locks.pop(service_ref, None)
        self._capability_probe_success = {
            key: value for key, value in self._capability_probe_success.items() if key[0] != service_ref
        }
        self._capability_probe_failures = {
            key: value for key, value in self._capability_probe_failures.items() if key[0] != service_ref
        }

    async def async_invalidate_account(self, account_entry_id: str) -> None:
        coordinator = self.hass.data.get(DOMAIN, {}).get(str(account_entry_id))
        service_ids = tuple(getattr(coordinator, "services", {})) if coordinator is not None else ()
        for service_id in service_ids:
            await self.async_invalidate_service(str(account_entry_id), str(service_id))

    async def async_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.registry.async_close()
        finally:
            self._native_backup_journals.clear()
            self._operation_journals.clear()
            self._account_coordinators.clear()
            self._capability_probe_locks.clear()
            self._capability_probe_success.clear()
            self._capability_probe_failures.clear()
            remove_provider_connector_registry(self.hass, self.registry)
            if self.hass.data.get(_RUNTIME_DATA_KEY) is self:
                self.hass.data.pop(_RUNTIME_DATA_KEY, None)


async def async_get_or_create_provider_runtime(hass: Any) -> HomeAssistantProviderRuntime:
    existing = hass.data.get(_RUNTIME_DATA_KEY)
    if isinstance(existing, HomeAssistantProviderRuntime) and not existing._closed:
        return existing
    if isinstance(existing, HomeAssistantProviderRuntime):
        hass.data.pop(_RUNTIME_DATA_KEY, None)
    runtime = HomeAssistantProviderRuntime(hass)
    await runtime.async_initialize()
    hass.data[_RUNTIME_DATA_KEY] = runtime
    return runtime


def get_provider_runtime(hass: Any) -> HomeAssistantProviderRuntime | None:
    runtime = hass.data.get(_RUNTIME_DATA_KEY)
    return runtime if isinstance(runtime, HomeAssistantProviderRuntime) else None


async def async_delete_provider_account_grants(hass: Any, account_entry_id: str) -> None:
    """Delete grants when an account entry is permanently removed, not reloaded."""

    runtime = get_provider_runtime(hass)
    if runtime is not None:
        await runtime.authority.async_delete_account_grants(account_entry_id)
        return
    authority = HomeAssistantProviderAuthority(hass)
    await authority.async_initialize()
    await authority.async_delete_account_grants(account_entry_id)


def new_provider_grant(
    *,
    consumer_domain: str,
    consumer_entry_id: str,
    account_entry_id: str,
    service_id: str,
    scopes: frozenset[ProviderScope],
    permitted_roots: frozenset[str] = frozenset(),
) -> ProviderGrant:
    """Build a core-owned persistent exact grant with an opaque ID."""

    return ProviderGrant(
        secrets.token_urlsafe(18),
        consumer_domain,
        consumer_entry_id,
        ProviderServiceRef(account_entry_id, service_id),
        scopes,
        permitted_roots=permitted_roots,
    )


def _internal_manifest(manifest: ProviderTreeManifest) -> FileTreeManifest:
    return FileTreeManifest(
        tuple(FileManifestEntry(item.path, item.size_bytes, item.sha256) for item in manifest.entries),
        manifest.total_bytes,
    )


def _validate_grant_shape(grant: StoredProviderGrant) -> None:
    has_filesystem_scope = bool(grant.scopes & _FILESYSTEM_SCOPES)
    if has_filesystem_scope != bool(grant.permitted_roots):
        raise ProviderAuthorizationError(
            "Filesystem grants require exact permitted roots and backup-only grants cannot include roots"
        )


async def _await_despite_cancellation(awaitable: Any, *, name: str) -> Any:
    """Drain one HA Store write so memory and durable authority cannot diverge."""

    task = asyncio.ensure_future(awaitable)
    if isinstance(task, asyncio.Task):
        task.set_name(name)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if task.done():
                return task.result()
