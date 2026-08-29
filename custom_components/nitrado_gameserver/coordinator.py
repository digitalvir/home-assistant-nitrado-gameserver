"""Account coordinator/runtime for Nitrado Game Server."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from .api.nitrado import MAX_STOP_REASON_CHARS, NitradoAuthError, NitradoClient, NitradoService
from .const import (
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_FINAL_SHUTDOWN_CHECK_DELAY_SECONDS,
    DEFAULT_IDLE_MINUTES,
    DEFAULT_MISSING_SERVICE_THRESHOLD,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_STARTUP_COOLDOWN_MINUTES,
    DEFAULT_STATUS_INTERVAL,
    DISCOVERY_ASK,
    DISCOVERY_AUTO_ADD,
    FILESYSTEM_TREE_REPLACE_ENABLED,
    RUNNING_STATUS,
    STOPPED_STATUS,
    TRANSITION_STATUSES,
)
from .editable_file_journal import (
    EditableFileJournal,
    EditableFileJournalError,
    EditableFileRecoveryRecord,
    MemoryEditableFileJournalStore,
)
from .editable_files import (
    EditableFileApplyResult,
    EditableFilePreview,
    EditableFileRollbackResult,
    EditableFileSnapshot,
    async_apply_editable_file,
    async_preview_editable_file,
    async_probe_editable_file_backup,
    async_read_editable_file,
    async_rollback_editable_file,
)
from .extensions import (
    LifecycleHookResult,
    ProfileExtensionError,
    ResourceDescriptor,
    ResourceResult,
    SurfaceDescriptor,
    SurfaceResourceBundle,
    async_fetch_profile_resource,
    async_fetch_profile_surface_resources,
    async_run_lifecycle_hooks,
    async_run_profile_action,
    async_validate_profile,
    profile_entity_context,
    profile_resource_descriptors,
    profile_surface_descriptor,
    profile_surface_descriptors,
)
from .filesystem import FileRecoveryError, NitradoFilesystemService, tree_manifest_sha256
from .filesystem_journal import FilesystemTransactionJournal, UnresolvedTransactionFact
from .models import DiscoveryReconcileResult, ManagedServiceState, reconcile_services
from .operation_journal import (
    MemoryOperationJournalStore,
    OperationIdentity,
    OperationIntent,
    OperationJournal,
    OperationJournalError,
    OperationPhase,
    OperationRecord,
    OperationReservation,
)
from .plugins.base import (
    PROFILE_HANDLER_TIMEOUT_SECONDS,
    UNSET,
    CapabilityState,
    CapabilityVerdict,
    ControlContext,
    LifecycleEvent,
    ProfileManifestError,
    ProfileOptionDeclaration,
    ProfileStatus,
    ValidatorDomain,
    ValidatorTarget,
    async_invoke_profile,
    blocked,
    normalized_profile_entity_key,
    profile_read_transport,
    valid_capability_verdict,
    validate_profile_option_value,
)
from .plugins.registry import profile_registration_generation, profile_registry_generation
from .profile_logging import log_profile_failure
from .profile_workers import async_run_profile_sync
from .runtime import ServiceRuntime, evaluate_start, evaluate_stop, runtime_profile_extension_manifest
from .save_bundles import (
    OverlayTreeFiles,
    SaveBundleApplyResult,
    SaveBundleExport,
    SaveBundlePreview,
    SaveBundleProgress,
    active_save_manifest,
    async_export_save_bundle,
    async_inspect_save_bundle,
)

_LOGGER = logging.getLogger(__name__)

ProfileDispatchToken = tuple[int, str, int, int, int]


def _dispatch_generation(token: ProfileDispatchToken) -> str:
    """Return a bounded non-secret generation fingerprint for durable intent."""

    return f"{token[1]}:{token[2]}:{token[3]}:{token[4]}"


class NitradoControlError(Exception):
    """Raised when a requested Nitrado command should not be sent."""

    def __init__(self, verdict: CapabilityVerdict) -> None:
        super().__init__(verdict.reason or verdict.state.value)
        self.verdict = verdict


class NitradoServiceUnknownError(NitradoControlError):
    """Raised when a service ID is not managed by this account entry."""

    def __init__(self, service_id: str) -> None:
        super().__init__(
            CapabilityVerdict(
                CapabilityState.BLOCKED,
                reason=f"Nitrado service {service_id} is not managed by this integration entry.",
                overridable=False,
            )
        )


@dataclass(slots=True)
class AccountCoordinatorOptions:
    """Runtime options used by the account coordinator."""

    discovery_mode: str = DISCOVERY_ASK
    discovery_interval: int = DEFAULT_DISCOVERY_INTERVAL
    status_interval: int = DEFAULT_STATUS_INTERVAL
    missing_service_threshold: int = DEFAULT_MISSING_SERVICE_THRESHOLD
    settle_seconds: int = DEFAULT_SETTLE_SECONDS
    imported_service_ids: frozenset[str] = frozenset()
    ignored_service_ids: frozenset[str] = frozenset()
    service_area_ids: dict[str, str] = field(default_factory=dict)
    service_display_names: dict[str, str] = field(default_factory=dict)
    idle_shutdown_service_ids: frozenset[str] = frozenset()
    maintenance_service_ids: frozenset[str] = frozenset()
    dry_run_service_ids: frozenset[str] = frozenset()
    allow_plaintext_ftp_service_ids: frozenset[str] = frozenset()
    idle_minutes: dict[str, float] = field(default_factory=dict)
    startup_cooldown_minutes: dict[str, float] = field(default_factory=dict)
    profile_options: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    profile_option_acknowledgements: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    final_shutdown_check_delay_seconds: int = DEFAULT_FINAL_SHUTDOWN_CHECK_DELAY_SECONDS


@dataclass(slots=True)
class AccountRuntimeSnapshot:
    """Serializable account runtime state for diagnostics/tests."""

    managed_service_ids: tuple[str, ...]
    runtime_service_ids: tuple[str, ...]
    pending_service_ids: tuple[str, ...]
    missing_service_ids: tuple[str, ...]
    last_discovery_result: DiscoveryReconcileResult | None = None


@dataclass(slots=True)
class NitradoAccountCoordinator:
    """Coordinate one Nitrado account entry and its managed service runtimes.

    This class intentionally keeps the core account/service behavior independent
    from Home Assistant's DataUpdateCoordinator so it can be tested without a
    live HA install. The HA integration stores one instance per config entry.
    """

    client: NitradoClient
    options: AccountCoordinatorOptions = field(default_factory=AccountCoordinatorOptions)
    now_fn: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))
    account_entry_id: str = "standalone"
    filesystem: NitradoFilesystemService | None = None
    filesystem_journal: FilesystemTransactionJournal | None = None
    operation_journal: OperationJournal = field(default_factory=lambda: OperationJournal(MemoryOperationJournalStore()))
    editable_file_journal: EditableFileJournal = field(
        default_factory=lambda: EditableFileJournal(MemoryEditableFileJournalStore())
    )
    filesystem_recovery_facts: tuple[UnresolvedTransactionFact, ...] = ()
    known: dict[str, ManagedServiceState] = field(default_factory=dict)
    services: dict[str, ServiceRuntime] = field(default_factory=dict)
    discovered_services: dict[str, NitradoService] = field(default_factory=dict)
    last_discovery_result: DiscoveryReconcileResult | None = None
    last_refresh_error: str | None = None
    _listeners: list[Callable[[], None]] = field(default_factory=list)
    unload_callbacks: list[Callable[[], None]] = field(default_factory=list)
    active_repair_issue_ids: set[str] = field(default_factory=set)
    active_discovery_flow_ids: set[str] = field(default_factory=set)
    _shutdown_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _active_mutation_tasks: dict[str, set[asyncio.Task[Any]]] = field(default_factory=dict)
    _active_profile_mutation_tasks: dict[str, dict[asyncio.Task[Any], int]] = field(default_factory=dict)
    _active_control_tasks: dict[str, dict[asyncio.Task[Any], int]] = field(default_factory=dict)
    _active_operation_tasks: dict[str, dict[asyncio.Task[Any], int]] = field(default_factory=dict)
    _control_transport_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _service_refresh_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Start, Stop, provider restore, and every filesystem mutation share one
    # service operation gate.  Provider state and file contents are one fault
    # domain; serializing them separately permits exactly the race this layer
    # exists to prevent.
    _service_operation_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _service_exclusion_tombstones: set[str] = field(default_factory=set)
    _operation_journal_durable_bound: bool = False
    _editable_file_journal_durable_bound: bool = False
    _shutting_down: bool = False
    setup_committed: bool = False

    def __post_init__(self) -> None:
        """Create the core-owned file facade when tests do not inject one."""

        if self.filesystem is None:
            self.filesystem = NitradoFilesystemService(
                self.client,
                allow_plaintext_ftp_service_ids=set(self.options.allow_plaintext_ftp_service_ids),
            )

    @property
    def profile_transport(self) -> NitradoFilesystemService:
        """Return the capability-limited transport used by game profiles."""

        if self.filesystem is None:
            raise RuntimeError("Nitrado filesystem service was not initialized")
        return self.filesystem

    async def async_initialize_filesystem(self) -> None:
        """Load durable recovery facts before any provider mutation is allowed."""

        await self.operation_journal.async_initialize()
        await self.editable_file_journal.async_initialize()
        if self.filesystem_journal is not None:
            await self.filesystem_journal.async_initialize()
            await self.async_refresh_filesystem_recovery_facts()

    async def async_refresh_filesystem_recovery_facts(self) -> None:
        """Refresh network-free durable facts after a provider transaction."""

        if self.filesystem_journal is None:
            self.filesystem_recovery_facts = ()
        else:
            self.filesystem_recovery_facts = await self.filesystem_journal.async_unresolved_facts()
        self._notify_listeners()

    def service_operation_lock(self, service_id: str) -> asyncio.Lock:
        """Return the low-level service lock for legacy repair coordination.

        New mutation entry points must use :meth:`async_operation`, which
        durably reserves intent before acquiring this lock.
        """

        return self._service_operation_locks.setdefault(str(service_id), asyncio.Lock())

    async def async_bind_operation_journal(self, journal: OperationJournal) -> None:
        """Bind the account's HA-backed journal before controls are exposed."""

        if (
            self._active_mutation_tasks
            or self._active_profile_mutation_tasks
            or self._active_control_tasks
            or self._active_operation_tasks
        ):
            raise RuntimeError("cannot replace operation arbitration while mutations are active")
        await journal.async_initialize()
        self.operation_journal = journal
        self._operation_journal_durable_bound = True

    async def async_bind_editable_file_journal(self, journal: EditableFileJournal) -> None:
        """Bind durable editable-file history before editor mutations are exposed."""

        if self._active_mutation_tasks or self._active_operation_tasks:
            raise RuntimeError("cannot replace editable-file history while mutations are active")
        await journal.async_initialize()
        self.editable_file_journal = journal
        self._editable_file_journal_durable_bound = True

    async def async_editable_file_recovery_records(
        self,
        service_id: str,
        file_key: str,
    ) -> tuple[EditableFileRecoveryRecord, ...]:
        """Return durable recovery references for one declared file."""

        return await self.editable_file_journal.async_records(str(service_id), str(file_key))

    async def async_require_editable_file_recovery(
        self,
        service_id: str,
        file_key: str,
        recovery_id: str,
    ) -> EditableFileRecoveryRecord:
        """Resolve one opaque recovery ID within its exact service/file scope."""

        return await self.editable_file_journal.async_require(str(service_id), str(file_key), str(recovery_id))

    async def async_operation_records(self, service_id: str | None = None) -> tuple[OperationRecord, ...]:
        """Return sanitized durable facts for diagnostics and quiescence gates."""

        identity = OperationIdentity(str(self.account_entry_id), str(service_id)) if service_id is not None else None
        return await self.operation_journal.async_records(identity)

    async def async_acknowledge_unknown_operation(self, service_id: str, operation_id: str) -> None:
        """Acknowledge uncertainty without rewriting it as success or failure."""

        identity = OperationIdentity(str(self.account_entry_id), str(service_id))
        await self.operation_journal.async_acknowledge_unknown(identity, str(operation_id))
        self._notify_listeners()

    @asynccontextmanager
    async def async_operation(
        self,
        service_id: str,
        intent: OperationIntent,
        *,
        operation_token: OperationReservation | None = None,
    ) -> AsyncIterator[OperationReservation]:
        """Reserve durable intent, then enter the sole service mutation lock.

        Lock order is invariant: service queue, journal write, profile lease,
        then any transport-specific journal. A task may pass its exact
        token to nested core code; that path never reacquires either outer
        lock and therefore cannot self-deadlock.
        """

        self._ensure_active()
        service_id = str(service_id)
        identity = OperationIdentity(str(self.account_entry_id), service_id)
        if operation_token is not None:
            operation_token.assert_owner(identity)
            yield operation_token
            return
        if self.account_entry_id != "standalone" and not self._operation_journal_durable_bound:
            raise NitradoControlError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="Durable operation arbitration is not initialized; the command was not sent.",
                    overridable=False,
                )
            )
        await self.operation_journal.async_initialize()
        task = asyncio.current_task()
        self._register_operation_task(service_id, task)
        try:
            async with self.service_operation_lock(service_id):
                # A waiter may have entered before unload raised the barrier.
                # Recheck after acquiring the queue and before durable reserve.
                self._ensure_active()
                try:
                    reservation = await self.operation_journal.async_reserve(identity, intent)
                except OperationJournalError as err:
                    raise NitradoControlError(
                        CapabilityVerdict(CapabilityState.BLOCKED, reason=str(err), overridable=False)
                    ) from err
                try:
                    yield reservation
                except BaseException as err:
                    try:
                        await reservation.async_fail(cancelled=isinstance(err, asyncio.CancelledError))
                    except BaseException:
                        _LOGGER.exception("Could not persist terminal operation truth for %s", reservation.operation_id)
                    raise
                else:
                    if reservation.phase is OperationPhase.RESERVED:
                        await reservation.async_complete("completed_without_dispatch")
                    elif reservation.phase is OperationPhase.DISPATCHED:
                        await reservation.async_fail()
                        raise OperationJournalError(
                            f"Operation {reservation.operation_id} returned without verification evidence"
                        )
                    elif reservation.phase is OperationPhase.VERIFYING and intent.kind not in {"start", "stop"}:
                        # Callers mark VERIFYING only after their operation-specific
                        # evidence has succeeded. Start/Stop deliberately remain
                        # nonterminal until a later fresh provider status observes
                        # the requested transition.
                        await reservation.async_complete()
        finally:
            self._unregister_operation_task(service_id, task)

    def _register_operation_task(self, service_id: str, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        active = self._active_operation_tasks.setdefault(str(service_id), {})
        active[task] = active.get(task, 0) + 1

    def _unregister_operation_task(self, service_id: str, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        service_id = str(service_id)
        active = self._active_operation_tasks.get(service_id)
        if active is None:
            return
        remaining = active.get(task, 1) - 1
        if remaining > 0:
            active[task] = remaining
        else:
            active.pop(task, None)
        if not active:
            self._active_operation_tasks.pop(service_id, None)

    async def _async_reconcile_lifecycle_operations(self, service_id: str, runtime: ServiceRuntime) -> None:
        """Resolve Start/Stop records only from a fresh provider observation."""

        if runtime.server is None or not runtime.status_fresh or runtime.using_cached_data:
            return
        identity = OperationIdentity(str(self.account_entry_id), str(service_id))
        try:
            records = await self.operation_journal.async_records(identity)
        except OperationJournalError:
            return
        status = runtime.server.raw_status
        for record in records:
            if record.phase not in {OperationPhase.VERIFYING, OperationPhase.UNKNOWN}:
                continue
            observed = (
                record.intent.kind == "start" and (status == RUNNING_STATUS or status in TRANSITION_STATUSES)
            ) or (record.intent.kind == "stop" and status == STOPPED_STATUS)
            if not observed:
                continue
            try:
                await self.operation_journal.async_reconcile(
                    identity,
                    record.operation_id,
                    "provider_status_observed",
                )
            except OperationJournalError:
                continue

    def ensure_filesystem_mutation_allowed(self, service_id: str) -> None:
        """Fail closed when durable recovery for this service is unresolved."""

        service_id = str(service_id)
        for fact in self.filesystem_recovery_facts:
            if fact.service is None or fact.service.service_id == service_id:
                raise NitradoControlError(
                    CapabilityVerdict(
                        CapabilityState.BLOCKED,
                        reason=(
                            "A previous provider file operation requires recovery; "
                            "resolve the Home Assistant repair before changing this server."
                        ),
                        overridable=False,
                    )
                )

    async def async_recover_filesystem_transactions(self, service_id: str) -> tuple[str, ...]:
        """Recover exact pre-mutation state while the service is stably stopped."""

        service_id = str(service_id)

        async def require_stopped() -> None:
            runtime = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
            if runtime.server is None or runtime.server.raw_status != STOPPED_STATUS:
                raise FileRecoveryError("Provider-file recovery requires the game server to be stably stopped")

        intent = OperationIntent(
            "filesystem-recover",
            "filesystem-journal",
            expected_evidence="filesystem recovery journal has no unresolved transaction",
        )
        async with self.async_operation(service_id, intent) as reservation:
            await reservation.async_mark_dispatched()
            completed = await self.profile_transport.recover_transactions(
                service_id,
                require_stopped=require_stopped,
            )
            await reservation.async_mark_verifying()
            await self.async_refresh_filesystem_recovery_facts()
            return completed

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register an entity state update callback."""

        self._listeners.append(update_callback)

        def remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener

    def _notify_listeners(self) -> None:
        """Notify registered entities that coordinator state changed."""

        for update_callback in tuple(self._listeners):
            update_callback()

    async def async_refresh_discovery(self) -> DiscoveryReconcileResult:
        """Refresh account service discovery and reconcile managed services."""

        if self._shutting_down:
            return self.last_discovery_result or DiscoveryReconcileResult(managed=dict(self.known))
        try:
            async with self._discovery_lock:
                services = await self.client.service_list()
                return await self.apply_discovered_services(services)
        except NitradoAuthError:
            await self._mark_authentication_stale()
            raise

    async def apply_discovered_services(self, services: Iterable[NitradoService]) -> DiscoveryReconcileResult:
        """Apply an already fetched service list.

        This is split from ``async_refresh_discovery`` so tests and future HA
        flows can reconcile a service-list fixture without a network call.
        """

        if self._shutting_down:
            return self.last_discovery_result or DiscoveryReconcileResult(managed=dict(self.known))
        services_by_id = {
            service.service_id: service
            for service in services
            if service.service_id not in self._service_exclusion_tombstones
        }
        auto_add = self.options.discovery_mode == DISCOVERY_AUTO_ADD
        result = reconcile_services(
            known=self.known,
            discovered_service_ids=services_by_id.keys(),
            missing_threshold=max(1, self.options.missing_service_threshold),
            auto_add=auto_add,
            imported_service_ids=self.options.imported_service_ids,
            ignored_service_ids=self.options.ignored_service_ids,
        )

        self.discovered_services = services_by_id
        self.known = result.managed
        for state in self.known.values():
            state.account_entry_id = str(self.account_entry_id)
        self.last_discovery_result = result
        self.last_refresh_error = None

        for service_id, service in services_by_id.items():
            state = self.known[service_id]
            if state.pending_discovery or state.ignored:
                self.services.pop(service_id, None)
                continue
            runtime = self.services.get(service_id)
            if runtime is None:
                runtime = ServiceRuntime(state)
                self.services[service_id] = runtime
            else:
                runtime.state = state
            runtime.update_service(service, select_profile_now=False)
            await runtime.async_select_profile(service, runtime.server)

        for service_id, state in self.known.items():
            if state.pending_discovery or state.ignored:
                self.services.pop(service_id, None)
                continue
            runtime = self.services.get(service_id)
            if runtime is None:
                runtime = ServiceRuntime(state)
                self.services[service_id] = runtime
            else:
                runtime.state = state

        self._notify_listeners()
        return result

    async def import_service(self, service_id: str) -> ServiceRuntime:
        """Import a pending discovered service into managed runtime state."""

        service_id = str(service_id)
        service = self.discovered_services.get(service_id)
        state = self.known.get(service_id)
        if service is None or state is None:
            raise NitradoServiceUnknownError(service_id)

        state.pending_discovery = False
        state.ignored = False
        state.available = True
        state.missing_count = 0
        runtime = self.services.get(service_id) or ServiceRuntime(state)
        self._service_exclusion_tombstones.discard(service_id)
        runtime.state = state
        runtime.update_service(service, select_profile_now=False)
        await runtime.async_select_profile(service, runtime.server)
        if (
            self._shutting_down
            or service_id in self._service_exclusion_tombstones
            or self.known.get(service_id) is not state
            or state.ignored
            or state.pending_discovery
        ):
            raise NitradoServiceUnknownError(service_id)
        self.services[service_id] = runtime
        self._notify_listeners()
        return runtime

    def ignore_service(self, service_id: str) -> tuple[asyncio.Task[Any], ...]:
        """Mark a pending discovered service ignored."""

        service_id = str(service_id)
        state = self.known.get(service_id)
        if state is None:
            raise NitradoServiceUnknownError(service_id)
        state.ignored = True
        state.pending_discovery = False
        state.available = False
        self._service_exclusion_tombstones.add(service_id)
        cancelled: list[asyncio.Task[Any]] = []
        shutdown_task = self.revoke_pending_shutdown(service_id, "Service ignored")
        if shutdown_task is not None:
            cancelled.append(shutdown_task)
        cancelled.extend(self.revoke_service_mutations(service_id))
        cancelled.extend(self.revoke_profile_mutations(service_id))
        cancelled.extend(self.revoke_control_operations(service_id))
        cancelled.extend(self.revoke_operation_tasks(service_id))
        # Let attached HA entities observe unavailable while the runtime still
        # exists. Notifying after removal makes their state callback raise
        # NitradoServiceUnknownError during the options-flow reload boundary.
        self._notify_listeners()
        self.services.pop(service_id, None)
        return tuple(cancelled)

    def remove_service(self, service_id: str) -> tuple[asyncio.Task[Any], ...]:
        """Remove an integration-managed service from local account state."""

        service_id = str(service_id)
        self._service_exclusion_tombstones.add(service_id)
        cancelled: list[asyncio.Task[Any]] = []
        shutdown_task = self.revoke_pending_shutdown(service_id, "Service removed")
        if shutdown_task is not None:
            cancelled.append(shutdown_task)
        cancelled.extend(self.revoke_service_mutations(service_id))
        cancelled.extend(self.revoke_profile_mutations(service_id))
        cancelled.extend(self.revoke_control_operations(service_id))
        cancelled.extend(self.revoke_operation_tasks(service_id))
        state = self.known.get(service_id)
        if state is not None:
            state.available = False
        self._notify_listeners()
        self.known.pop(service_id, None)
        self.services.pop(service_id, None)
        return tuple(cancelled)

    async def async_refresh_service(
        self,
        service_id: str,
        *,
        status_fresh: bool = True,
        handle_idle_shutdown: bool = True,
    ) -> ServiceRuntime:
        """Refresh one managed service's generic Nitrado gameserver status."""

        service_id = str(service_id)
        if self._shutting_down:
            return self.get_runtime(service_id)
        try:
            return await self._async_refresh_service_checked(
                service_id,
                status_fresh=status_fresh,
                handle_idle_shutdown=handle_idle_shutdown,
            )
        except NitradoAuthError:
            await self._mark_authentication_stale()
            raise
        except Exception as err:
            await self._mark_service_refresh_stale(service_id, err)
            raise

    async def _async_refresh_service_checked(
        self,
        service_id: str,
        *,
        status_fresh: bool,
        handle_idle_shutdown: bool,
    ) -> ServiceRuntime:
        """Run one refresh; the public wrapper owns fail-closed handling."""

        lock = self._service_refresh_locks.setdefault(service_id, asyncio.Lock())
        async with lock:
            runtime = await self._async_refresh_service_unlocked(
                service_id,
                status_fresh=status_fresh,
            )
        # An API request may have been in flight when entry unload began.  Do
        # not let its completion create fresh lifecycle, entity, or automatic
        # Stop work after ``async_shutdown`` has already drained owned tasks.
        if (
            self._shutting_down
            or service_id in self._service_exclusion_tombstones
            or self.services.get(service_id) is not runtime
        ):
            return runtime
        await self._async_run_nonblocking_lifecycle(
            runtime,
            LifecycleEvent.STATUS_REFRESH,
            payload={
                "service_id": service_id,
                "status": runtime.last_status,
                "status_fresh": runtime.status_fresh,
                "using_cached_data": runtime.using_cached_data,
                "profile_error": runtime.last_profile_refresh_error,
            },
        )
        # Final idle-shutdown checks perform their own fresh service polls. Run
        # the shutdown state machine only after releasing the refresh lock so
        # those polls cannot deadlock waiting on the lock held by their parent.
        if handle_idle_shutdown:
            await self._async_handle_idle_shutdown(runtime)
        self._notify_listeners()
        return runtime

    async def _async_refresh_service_unlocked(
        self,
        service_id: str,
        *,
        status_fresh: bool,
    ) -> ServiceRuntime:
        """Refresh one service while its per-service refresh lock is held."""

        runtime = self.get_runtime(service_id)
        self._sync_profile_options(runtime)
        server = await self.client.fetch_server(str(service_id))
        runtime.update_server(
            server,
            observed_at=self.now_fn(),
            status_fresh=status_fresh,
            select_profile_now=False,
        )
        if runtime.service is not None:
            await runtime.async_select_profile(runtime.service, server)
        # Profile replacement invalidates any prior profile-derived snapshot.
        # This fetch is the fresh generic baseline for the newly selected
        # profile, so restore its freshness before enrichment.
        runtime.server = server
        runtime.status_fresh = status_fresh
        runtime.using_cached_data = False
        self._sync_profile_options(runtime)
        if runtime.service and runtime.profile:
            async with runtime.profile_operation_lock:
                profile = runtime.profile
                try:
                    profile_status = await async_invoke_profile(
                        profile.enrich_status,
                        profile_read_transport(
                            self.client,
                            service_id,
                            file_transport=self.profile_transport,
                        ),
                        runtime.service,
                        server,
                        ControlContext(
                            service=runtime.service,
                            server=server,
                            status_fresh=runtime.status_fresh,
                            using_cached_data=runtime.using_cached_data,
                            extra=runtime.profile_extra(),
                            options=dict(runtime.extra.get("_persisted_profile_options", {})),
                        ),
                    )
                    profile_status = await async_run_profile_sync(
                        (
                            "profile_status_normalize",
                            getattr(profile, "profile_id", "unknown"),
                            runtime.profile_generation,
                            runtime.profile_registration_generation,
                            runtime.profile_registry_generation_seen,
                        ),
                        _normalize_profile_status,
                        profile_status,
                        timeout=PROFILE_HANDLER_TIMEOUT_SECONDS,
                    )
                except Exception as err:
                    log_profile_failure(
                        _LOGGER,
                        "enrich_status",
                        err,
                        profile_id=getattr(profile, "profile_id", "unknown"),
                    )
                    runtime.last_profile_refresh_error = "Profile enrichment failed; details were logged."
                    runtime.extra.pop("profile_status", None)
                    _fail_player_data_closed(runtime)
                else:
                    runtime.last_profile_refresh_error = None
                    if profile_status is not None and not _valid_profile_status(profile_status):
                        runtime.last_profile_refresh_error = "Profile returned an invalid status enrichment."
                        runtime.extra.pop("profile_status", None)
                        _fail_player_data_closed(runtime)
                    elif profile_status:
                        runtime.extra["profile_status"] = profile_status
                        server_updates = _server_updates_from_profile_status(profile_status)
                        if server_updates:
                            runtime.server = replace(runtime.server, **server_updates)
        # A fresh, stable stopped provider state is authoritative proof that
        # nobody is connected even though the game-native query endpoint is
        # offline. Keep query_valid false: this derived zero is display/state
        # truth, never running-server authority for automatic shutdown.
        if runtime.server is not None and runtime.server.raw_status == STOPPED_STATUS:
            runtime.server = replace(
                runtime.server,
                player_count=0,
                player_names=(),
                player_source="server_status",
                query_valid=False,
            )
        await self._async_refresh_profile_entity_snapshot(runtime)
        await self._refresh_control_verdicts(runtime)
        await self._async_reconcile_lifecycle_operations(service_id, runtime)
        return runtime

    def _sync_profile_options(self, runtime: ServiceRuntime) -> None:
        """Expose declared defaults plus this service's persisted overrides."""

        profile_id = getattr(runtime.profile, "profile_id", None)
        service_id = runtime.state.identity.service_id
        values: dict[str, Any] = {}
        if profile_id and runtime.profile is not None:
            try:
                manifest = runtime_profile_extension_manifest(runtime)
                declarations = manifest.profile_options
            except ProfileManifestError:
                manifest = None
                declarations = ()
            persisted = self.options.profile_options.get(profile_id, {})
            declared_by_key = {item.key: item for item in declarations}
            hosted_by_key = {
                str(entity.attributes["option_key"]): entity
                for entity in (() if manifest is None else manifest.entities)
                if entity.attributes.get("option_key") is not None
            }
            for declaration in declarations:
                values[declaration.key] = declaration.default
            for option_key, service_values in persisted.items():
                if service_id not in service_values:
                    continue
                declaration = declared_by_key.get(option_key)
                try:
                    if declaration is not None:
                        values[option_key] = validate_profile_option_value(declaration, service_values[service_id])
                    elif option_key in hosted_by_key and _valid_hosted_entity_option(
                        hosted_by_key[option_key], service_values[service_id]
                    ):
                        values[option_key] = service_values[service_id]
                    else:
                        raise ValueError("undeclared or malformed hosted entity option")
                except ValueError:
                    _LOGGER.warning(
                        "Ignoring malformed persisted profile option %s.%s for service %s",
                        profile_id,
                        option_key,
                        service_id,
                    )
        runtime.extra["_persisted_profile_options"] = values

    async def _async_refresh_profile_entity_snapshot(self, runtime: ServiceRuntime) -> None:
        """Evaluate passive extension callbacks off-loop and atomically cache them."""

        service_id = runtime.state.identity.service_id
        if self.services.get(service_id) is not runtime:
            return
        if runtime.profile is None:
            runtime.extra.pop("_profile_entity_snapshot", None)
            return
        try:
            manifest = runtime_profile_extension_manifest(runtime)
        except ProfileManifestError as err:
            runtime.extra.pop("_profile_entity_snapshot", None)
            log_profile_failure(
                _LOGGER,
                "entity_manifest",
                err,
                profile_id=getattr(runtime.profile, "profile_id", "unknown"),
            )
            return
        token = self.profile_dispatch_token(service_id)
        context = profile_entity_context(runtime)
        gate = asyncio.Semaphore(4)

        async def evaluate(entity: Any, value_field: str, callback: Any) -> tuple[str, str, Any, str | None]:
            async with gate:
                try:
                    value = await async_invoke_profile(
                        callback,
                        context,
                        worker_identity=("passive_entity", *token[1:], entity.key, value_field),
                    )
                    value = _safe_profile_entity_value(entity, value_field, value)
                except Exception as err:
                    log_profile_failure(
                        _LOGGER,
                        f"entity_{value_field}",
                        err,
                        profile_id=manifest.profile_id,
                        key=entity.key,
                    )
                    return entity.key, value_field, None, err.__class__.__name__
                return entity.key, value_field, value, None

        jobs = []
        for entity in manifest.entities:
            for value_field, callback in (
                ("available", entity.available_fn),
                ("value", entity.value_fn),
                ("options", entity.options_fn),
            ):
                if callback is not None:
                    jobs.append(evaluate(entity, value_field, callback))
        results = await asyncio.gather(*jobs) if jobs else ()
        if self.services.get(service_id) is not runtime:
            return
        if token != self.profile_dispatch_token(service_id):
            return
        values: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, str]] = {}
        for key, value_field, value, error_type in results:
            cache_key = normalized_profile_entity_key(manifest.profile_id, key)
            if error_type is None:
                values.setdefault(cache_key, {})[value_field] = value
            else:
                errors.setdefault(cache_key, {})[value_field] = error_type
                runtime.last_profile_entity_errors[cache_key] = {
                    "phase": value_field,
                    "error": "Profile entity callback failed safely; details were logged.",
                    "type": error_type,
                }
        successful_keys = set(values) - set(errors)
        for key in successful_keys:
            runtime.last_profile_entity_errors.pop(key, None)
        runtime.extra["_profile_entity_snapshot"] = {
            "token": token,
            "values": MappingProxyType({key: MappingProxyType(dict(fields)) for key, fields in values.items()}),
            "errors": MappingProxyType({key: MappingProxyType(dict(fields)) for key, fields in errors.items()}),
        }

    async def async_refresh_profile_entity_snapshot(self, service_id: str) -> None:
        """Refresh cached passive profile values without running code in HA properties."""

        runtime = self.get_runtime(service_id)
        await self._async_refresh_profile_entity_snapshot(runtime)
        self._notify_listeners()

    async def async_profile_options_changed(self, runtime: ServiceRuntime, reason: str) -> None:
        """Apply profile options and immediately revoke profile-derived truth.

        Profile options can change transport consent, credentials, endpoints,
        or validation policy. The previous profile snapshot therefore cannot
        remain automation-authoritative until a later status refresh proves it
        under the new options.
        """

        self._sync_profile_options(runtime)
        runtime.extra.pop("profile_status", None)
        _fail_player_data_closed(runtime)
        runtime.last_idle_shutdown_verdict = CapabilityVerdict(
            CapabilityState.BLOCKED,
            reason="Profile options changed; fresh trusted player status is required.",
            overridable=False,
        )
        self.revoke_pending_shutdown(runtime.state.identity.service_id, reason)
        await self._refresh_control_verdicts(runtime)
        await self._async_refresh_profile_entity_snapshot(runtime)
        self._notify_listeners()

    async def async_refresh_managed_services(self) -> None:
        """Refresh status for every non-pending managed service."""

        errors: list[str] = []
        for service_id in tuple(self.services):
            try:
                await self.async_refresh_service(service_id)
            except NitradoAuthError:
                raise
            except Exception as err:
                errors.append(f"{service_id}: {err.__class__.__name__}: {err}")
        self.last_refresh_error = "; ".join(errors) if errors else None
        self._notify_listeners()

    async def _mark_authentication_stale(self) -> None:
        """Fail closed before Home Assistant starts account reauthentication."""

        self.last_refresh_error = "Nitrado authentication failed; reauthentication is required."
        for service_id, runtime in tuple(self.services.items()):
            runtime.status_fresh = False
            runtime.using_cached_data = runtime.server is not None
            self.revoke_pending_shutdown(service_id, "Nitrado authentication failed; idle shutdown revoked")
            self._cache_stale_control_verdicts(runtime, "Nitrado authentication failed; reauthentication is required.")
        self._notify_listeners()

    async def _mark_service_refresh_stale(self, service_id: str, err: Exception) -> None:
        """Fail one service closed after any non-authentication refresh error."""

        runtime = self.services.get(str(service_id))
        if runtime is None:
            return
        runtime.status_fresh = False
        runtime.using_cached_data = runtime.server is not None
        self.revoke_pending_shutdown(
            str(service_id),
            "Service refresh failed; idle shutdown revoked and timer reset",
        )
        self._cache_stale_control_verdicts(runtime, "Service status refresh failed; controls are unavailable.")
        self.last_refresh_error = f"{service_id}: {err.__class__.__name__}"
        self._notify_listeners()

    @staticmethod
    def _cache_stale_control_verdicts(runtime: ServiceRuntime, reason: str) -> None:
        """Fail controls closed without invoking potentially broken profile code."""

        verdict = CapabilityVerdict(CapabilityState.BLOCKED, reason=reason, overridable=False)
        runtime.last_start_verdict = verdict
        runtime.last_stop_verdict = verdict
        runtime.last_idle_shutdown_verdict = verdict

    async def async_refresh_all(self) -> None:
        """Refresh discovery and then status for all managed services."""

        await self.async_refresh_discovery()
        await self.async_refresh_managed_services()

    async def async_run_scheduled_validations(self) -> None:
        """Dispatch non-blocking scheduled profile validation hooks."""

        if self._shutting_down:
            return
        for service_id, runtime in tuple(self.services.items()):
            await self._async_run_nonblocking_lifecycle(
                runtime,
                LifecycleEvent.SCHEDULED_VALIDATION,
                payload={"service_id": service_id, "scheduled_at": self.now_fn()},
            )

    async def async_start_service(self, service_id: str, *, force: bool = False) -> CapabilityVerdict:
        """Safely send Start for a managed service if all checks pass."""

        self._ensure_active()
        service_id = str(service_id)
        original_runtime = self.get_runtime(service_id)

        async def operation() -> CapabilityVerdict:
            intent = OperationIntent(
                "start",
                "server-control",
                generation=_dispatch_generation(self.profile_dispatch_token(service_id)),
                expected_evidence="Nitrado accepted Start and a later status refresh observes the transition",
            )
            async with self.async_operation(service_id, intent) as reservation:
                return await self._async_start_service_locked(
                    service_id,
                    original_runtime=original_runtime,
                    force=force,
                    operation=reservation,
                )

        return await self.async_track_control_operation(service_id, operation)

    async def _async_start_service_locked(
        self,
        service_id: str,
        *,
        original_runtime: ServiceRuntime,
        force: bool,
        operation: OperationReservation,
    ) -> CapabilityVerdict:
        """Run one serialized Start operation through its transport boundary."""

        self._ensure_active()
        self.ensure_filesystem_mutation_allowed(service_id)
        runtime = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
        self._raise_if_recent_command(runtime.last_start_time, "Start")
        expected_profile = self.profile_dispatch_token(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            verdict = await evaluate_start(
                runtime,
                now=self.now_fn(),
                settle_seconds=self.options.settle_seconds,
                force=force,
            )
        if not verdict.allowed:
            raise NitradoControlError(verdict)
        payload = {"command": "start", "force": force, "service_id": str(service_id)}
        await self._async_run_control_hooks(
            runtime,
            LifecycleEvent.BEFORE_START,
            payload=payload,
            force=force,
            expected_profile=expected_profile,
        )
        # Hooks may wait on external work. Revalidate against a fresh Nitrado
        # snapshot immediately before crossing the Start transport boundary.
        runtime = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
        self._raise_if_recent_command(runtime.last_start_time, "Start")
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            verdict = await evaluate_start(
                runtime,
                now=self.now_fn(),
                settle_seconds=self.options.settle_seconds,
                force=force,
            )
            if not verdict.allowed:
                raise NitradoControlError(verdict)
            self._ensure_service_runtime_active(service_id, original_runtime)
            self.ensure_filesystem_mutation_allowed(service_id)
            game_short = self._start_game_short(runtime)
            await operation.async_mark_dispatched()
            await self._async_run_control_transport(self.client.start_server(str(service_id), game_short))
            await operation.async_mark_verifying()
        runtime.last_start_time = self.now_fn()
        await self._async_run_control_hooks(
            runtime,
            LifecycleEvent.AFTER_START,
            payload=payload,
            force=force,
            raise_blocking=False,
            expected_profile=expected_profile,
        )
        return verdict

    async def async_stop_service(
        self,
        service_id: str,
        *,
        reason: str = "",
        force: bool = False,
        _pre_send_guard: Callable[[], bool] | None = None,
    ) -> CapabilityVerdict:
        """Safely send Stop for a managed service if all checks pass."""

        self._ensure_active()
        reason = str(reason or "Stopped from Home Assistant")[:MAX_STOP_REASON_CHARS]
        service_id = str(service_id)
        original_runtime = self.get_runtime(service_id)

        async def operation() -> CapabilityVerdict:
            intent = OperationIntent(
                "stop",
                "server-control",
                generation=_dispatch_generation(self.profile_dispatch_token(service_id)),
                expected_evidence="Nitrado accepted Stop and a later status refresh observes the transition",
            )
            async with self.async_operation(service_id, intent) as reservation:
                return await self._async_stop_service_locked(
                    service_id,
                    original_runtime=original_runtime,
                    reason=reason,
                    force=force,
                    pre_send_guard=_pre_send_guard,
                    operation=reservation,
                )

        return await self.async_track_control_operation(service_id, operation)

    async def _async_stop_service_locked(
        self,
        service_id: str,
        *,
        original_runtime: ServiceRuntime,
        reason: str,
        force: bool,
        pre_send_guard: Callable[[], bool] | None,
        operation: OperationReservation,
    ) -> CapabilityVerdict:
        """Run one serialized Stop operation through its transport boundary."""

        self._ensure_active()
        runtime = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
        self._raise_if_recent_command(runtime.last_stop_time, "Stop")
        expected_profile = self.profile_dispatch_token(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            verdict = await evaluate_stop(runtime, force=force)
        if not verdict.allowed:
            raise NitradoControlError(verdict)
        if pre_send_guard is not None and not pre_send_guard():
            raise NitradoControlError(self._shutdown_revoked_verdict())
        payload = {
            "command": "stop",
            "force": force,
            "reason": reason,
            "service_id": str(service_id),
        }
        await self._async_run_control_hooks(
            runtime,
            LifecycleEvent.BEFORE_STOP,
            payload=payload,
            force=force,
            expected_profile=expected_profile,
        )
        # A profile hook may wait or alter external state. Reacquire a fresh
        # snapshot and rerun the stop verdict before the transport boundary so
        # automatic shutdown cannot use a pre-hook zero-player observation.
        runtime = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
        self._raise_if_recent_command(runtime.last_stop_time, "Stop")
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            verdict = await evaluate_stop(runtime, force=force)
            if not verdict.allowed:
                raise NitradoControlError(verdict)
            if pre_send_guard is not None and not pre_send_guard():
                raise NitradoControlError(self._shutdown_revoked_verdict())
            self._ensure_service_runtime_active(service_id, original_runtime)
            await operation.async_mark_dispatched()
            await self._async_run_control_transport(self.client.stop_server(str(service_id), reason))
            await operation.async_mark_verifying()
        runtime.last_stop_time = self.now_fn()
        await self._async_run_control_hooks(
            runtime,
            LifecycleEvent.AFTER_STOP,
            payload=payload,
            force=force,
            raise_blocking=False,
            expected_profile=expected_profile,
        )
        return verdict

    def _raise_if_recent_command(self, command_at: int | None, command: str) -> None:
        """Suppress duplicate transport while Nitrado may still report stale state."""

        if command_at is None:
            return
        age = max(0, self.now_fn() - command_at)
        if age < max(1, self.options.settle_seconds):
            raise NitradoControlError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason=f"{command} was already sent {age}s ago; duplicate command suppressed.",
                    overridable=False,
                )
            )

    async def async_cancel_pending_shutdown(self, service_id: str) -> None:
        """Cancel pending automatic shutdown state for a service."""

        task = self.revoke_pending_shutdown(service_id, "Pending shutdown canceled manually")
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    def revoke_pending_shutdown(self, service_id: str, reason: str) -> asyncio.Task[None] | None:
        """Invalidate and cancel any automatic shutdown intent for a service."""

        service_id = str(service_id)
        runtime = self.services.get(service_id)
        if runtime is None:
            return None
        runtime.shutdown_generation += 1
        task = self._shutdown_tasks.pop(service_id, None)
        current = asyncio.current_task()
        if task is not None and task is not current and task not in self._control_transport_tasks and not task.done():
            task.cancel()
        runtime.reset_idle_timer(reason)
        self._notify_listeners()
        return task

    async def async_shutdown(self) -> None:
        """Quiesce and drain integration-owned work before entry unload.

        This phase is intentionally reversible because Home Assistant can
        abort a platform unload.  Irreversible provider-worker closure belongs
        to :meth:`async_finalize_shutdown` after HA confirms the unload.
        """

        self._shutting_down = True
        tasks: list[asyncio.Task[Any]] = []
        service_ids = (
            set(self.services)
            | set(self._active_mutation_tasks)
            | set(self._active_profile_mutation_tasks)
            | set(self._active_control_tasks)
            | set(self._active_operation_tasks)
        )
        for service_id in tuple(service_ids):
            task = self.revoke_pending_shutdown(service_id, "Integration entry unloading")
            if task is not None:
                tasks.append(task)
            tasks.extend(self.revoke_service_mutations(service_id))
            tasks.extend(self.revoke_profile_mutations(service_id))
            tasks.extend(self.revoke_control_operations(service_id))
            tasks.extend(self.revoke_operation_tasks(service_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def async_finalize_shutdown(self) -> None:
        """Permanently close provider resources after a successful unload."""

        await self.async_shutdown()
        filesystem = getattr(self, "filesystem", None)
        closer = getattr(filesystem, "async_close", None)
        if callable(closer):
            await closer()

    async def async_quiesce_profile_registry_change(self) -> None:
        """Drain every profile execution lease before changing the global registry."""

        await self.async_shutdown()
        # Read-only resources and status enrichment are not mutation tasks, but
        # they still execute profile code under this lease. New work is blocked
        # by ``_shutting_down``; cycling each current lease therefore proves no
        # old callback can resume after the registry generation changes.
        for runtime in tuple(self.services.values()):
            async with runtime.profile_operation_lock:
                pass

    def ensure_profile_registry_transition_allowed(self) -> None:
        """Reject registry mutation from code executing under a profile lease."""

        current = asyncio.current_task()
        if any(runtime.profile_operation_lock.owned_by(current) for runtime in self.services.values()):
            raise RuntimeError("A game profile cannot change the profile registry from inside its own callback")
        if current is not None and any(current in active for active in self._active_profile_mutation_tasks.values()):
            raise RuntimeError("A game profile cannot change the profile registry from inside its own callback")
        if current is not None and any(current in active for active in self._active_mutation_tasks.values()):
            raise RuntimeError("A game profile cannot change the profile registry from inside its own callback")

    def resume_after_failed_shutdown(self) -> None:
        """Resume coordinator work when Home Assistant aborts entry unload."""

        self._shutting_down = False

    def revoke_service_mutations(self, service_id: str) -> tuple[asyncio.Task[Any], ...]:
        """Return active mutations so unload/removal can drain them safely."""

        service_id = str(service_id)
        current = asyncio.current_task()
        tasks = tuple(self._active_mutation_tasks.get(service_id, set()))
        return tuple(task for task in tasks if task is not current)

    def revoke_profile_mutations(self, service_id: str) -> tuple[asyncio.Task[Any], ...]:
        """Cancel and return active cooperative profile mutations."""

        service_id = str(service_id)
        current = asyncio.current_task()
        tasks = tuple(self._active_profile_mutation_tasks.get(service_id, {}))
        pending = tuple(task for task in tasks if task is not current)
        for task in pending:
            if not task.done():
                task.cancel()
        return pending

    def revoke_control_operations(self, service_id: str) -> tuple[asyncio.Task[Any], ...]:
        """Return active controls so teardown can drain their transport boundary."""

        service_id = str(service_id)
        current = asyncio.current_task()
        tasks = tuple(self._active_control_tasks.get(service_id, {}))
        return tuple(task for task in tasks if task is not current)

    def revoke_operation_tasks(self, service_id: str) -> tuple[asyncio.Task[Any], ...]:
        """Return every authoritative operation so unload can prove quiescence."""

        current = asyncio.current_task()
        tasks = tuple(self._active_operation_tasks.get(str(service_id), {}))
        return tuple(task for task in tasks if task is not current)

    async def async_track_control_operation(
        self,
        service_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Own one complete Start/Stop operation through transport and after-hooks."""

        self._ensure_active()
        service_id = str(service_id)
        task = asyncio.current_task()
        if task is not None:
            active = self._active_control_tasks.setdefault(service_id, {})
            active[task] = active.get(task, 0) + 1
        try:
            return await operation()
        finally:
            await self.async_refresh_filesystem_recovery_facts()
            if task is not None:
                active = self._active_control_tasks.get(service_id)
                if active is not None:
                    remaining = active.get(task, 1) - 1
                    if remaining > 0:
                        active[task] = remaining
                    else:
                        active.pop(task, None)
                    if not active:
                        self._active_control_tasks.pop(service_id, None)
                        self._control_transport_tasks.discard(task)
                else:
                    self._control_transport_tasks.discard(task)

    async def _async_run_control_transport(self, operation: Awaitable[Any]) -> Any:
        """Mark an irreversible control request so teardown drains rather than cancels it."""

        task = asyncio.current_task()
        if task is not None:
            self._control_transport_tasks.add(task)
        return await operation

    async def async_track_profile_mutation(
        self,
        service_id: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Own one cooperative profile mutation through unload/removal."""

        self._ensure_active()
        service_id = str(service_id)
        self._require_profile_dispatch_token(self.get_runtime(service_id), None)
        task = asyncio.current_task()
        self._register_profile_task(service_id, task)
        try:
            return await operation()
        finally:
            self._unregister_profile_task(service_id, task)

    def _register_profile_task(self, service_id: str, task: asyncio.Task[Any] | None) -> None:
        """Track a task that may execute profile-owned code."""

        if task is None:
            return
        active = self._active_profile_mutation_tasks.setdefault(str(service_id), {})
        active[task] = active.get(task, 0) + 1

    def _unregister_profile_task(self, service_id: str, task: asyncio.Task[Any] | None) -> None:
        """Release one nested profile-code task lease."""

        if task is None:
            return
        service_id = str(service_id)
        active = self._active_profile_mutation_tasks.get(service_id)
        if active is None:
            return
        remaining = active.get(task, 1) - 1
        if remaining > 0:
            active[task] = remaining
        else:
            active.pop(task, None)
        if not active:
            self._active_profile_mutation_tasks.pop(service_id, None)

    def idle_shutdown_supported(self, service_id: str) -> bool:
        """Return whether the selected game profile supports idle shutdown."""

        runtime = self.get_runtime(service_id)
        verdict = runtime.last_idle_shutdown_verdict
        return bool(verdict and verdict.allowed)

    def idle_shutdown_configurable(self, service_id: str) -> bool:
        """Return whether idle shutdown can truthfully be configured."""

        runtime = self.get_runtime(service_id)
        return bool(
            runtime.profile
            and runtime.profile.idle_shutdown_supported
            and not self.unmet_idle_shutdown_profile_options(service_id)
        )

    def unmet_idle_shutdown_profile_options(self, service_id: str) -> tuple[ProfileOptionDeclaration, ...]:
        """Return profile settings that currently prevent idle shutdown."""

        runtime = self.get_runtime(service_id)
        if getattr(runtime, "profile", None) is None:
            return ()
        manifest = runtime_profile_extension_manifest(runtime)
        values = runtime.extra.get("_persisted_profile_options", {})
        return tuple(
            declaration
            for declaration in manifest.profile_options
            if declaration.idle_shutdown_required and values.get(declaration.key, declaration.default) is not True
        )

    def unacknowledged_profile_options(self, service_id: str) -> tuple[ProfileOptionDeclaration, ...]:
        """Return administrator decisions whose current revision is unresolved."""

        runtime = self.get_runtime(service_id)
        if getattr(runtime, "profile", None) is None:
            return ()
        profile_id = str(runtime.profile.profile_id)
        manifest = runtime_profile_extension_manifest(runtime)
        persisted = self.options.profile_options.get(profile_id, {})
        acknowledgements = self.options.profile_option_acknowledgements.get(profile_id, {})
        unresolved: list[ProfileOptionDeclaration] = []
        for declaration in manifest.profile_options:
            revision = declaration.acknowledgement_revision
            if not declaration.repair_if_unacknowledged or revision is None:
                continue
            acknowledged = acknowledgements.get(declaration.key, {}).get(str(service_id), 0)
            # Explicit revision-one values from pre-contract builds are already
            # an administrator choice and migrate without disabling behavior.
            legacy_explicit = revision == 1 and str(service_id) in persisted.get(declaration.key, {})
            if acknowledged < revision and not legacy_explicit:
                unresolved.append(declaration)
        return tuple(unresolved)

    async def async_run_profile_action(
        self,
        service_id: str,
        action_key: str,
        *,
        payload: Any | None = None,
        confirmed: bool = False,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> Any:
        """Run a profile-declared action through generic dispatch."""

        self._ensure_active()
        service_id = str(service_id)
        runtime = self.get_runtime(service_id)

        async def operation() -> Any:
            dispatch = expected_profile or self.profile_dispatch_token(service_id)
            intent = OperationIntent(
                "profile-action",
                str(action_key),
                # Profile actions are full-service writes unless a future
                # validated declaration introduces narrower conflict metadata.
                scopes=("service",),
                generation=_dispatch_generation(dispatch),
                expected_evidence="profile action completed without a stale dispatch token",
            )
            async with (
                self.async_operation(service_id, intent) as reservation,
                runtime.profile_operation_lock,
            ):
                self._ensure_active()
                self._require_profile_dispatch_token(runtime, expected_profile)
                await reservation.async_mark_dispatched()
                result = await async_run_profile_action(
                    runtime,
                    self.profile_transport,
                    action_key,
                    payload=payload,
                    confirmed=confirmed,
                    now=self.now_fn(),
                )
                await reservation.async_mark_verifying()
                return result

        return await self.async_track_profile_mutation(service_id, operation)

    async def async_fetch_profile_resource(
        self,
        service_id: str,
        resource_key: str,
        *,
        payload: Any | None = None,
        use_cache: bool = True,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> ResourceResult:
        """Fetch a profile-declared resource through generic dispatch."""

        service_id = str(service_id)
        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            result = await async_fetch_profile_resource(
                runtime,
                self.profile_transport,
                resource_key,
                payload=payload,
                use_cache=use_cache,
                now=self.now_fn(),
            )
            stream_token = expected_profile or self.profile_dispatch_token(service_id)
            self._require_profile_dispatch_token(runtime, stream_token)
        if hasattr(result.data, "__aiter__"):
            return replace(
                result,
                data=self._guard_profile_stream(service_id, runtime, stream_token, result.data),
            )
        return result

    async def _guard_profile_stream(
        self,
        service_id: str,
        runtime: ServiceRuntime,
        expected_profile: ProfileDispatchToken,
        stream: Any,
    ) -> AsyncIterator[Any]:
        """Own a profile stream through completion, cancellation, and close."""

        task = asyncio.current_task()
        self._register_profile_task(service_id, task)
        if task is not None:
            runtime.active_profile_stream_tasks.add(task)
        iterator = None
        try:
            iterator = stream.__aiter__()
            while True:
                async with runtime.profile_operation_lock:
                    self._ensure_active()
                    if self.services.get(service_id) is not runtime:
                        raise ProfileExtensionError(
                            CapabilityVerdict(
                                CapabilityState.BLOCKED,
                                reason="The managed service changed; the profile stream was closed.",
                                overridable=False,
                            )
                        )
                    self._require_profile_dispatch_token(runtime, expected_profile)
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                async with runtime.profile_operation_lock:
                    self._require_profile_dispatch_token(runtime, expected_profile)
                yield chunk
        finally:
            try:
                if iterator is not None:
                    await self._async_close_profile_stream(iterator)
            finally:
                if task is not None:
                    runtime.active_profile_stream_tasks.discard(task)
                self._unregister_profile_task(service_id, task)

    @staticmethod
    async def _async_close_profile_stream(stream: Any) -> None:
        """Close a profile iterator within a bound despite repeated cancellation."""

        closer = getattr(stream, "aclose", None)
        if not callable(closer):
            return

        async def close() -> None:
            async with asyncio.timeout(min(PROFILE_HANDLER_TIMEOUT_SECONDS, 5.0)):
                await closer()

        cleanup_task = asyncio.create_task(close(), name="nitrado-profile-stream-close")
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if cleanup_task.done():
                    cleanup_task.result()
                    return

    async def async_fetch_profile_surface_resources(
        self,
        service_id: str,
        surface_key: str,
        *,
        payload: Any | None = None,
        use_cache: bool = True,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> SurfaceResourceBundle:
        """Fetch all resources composed by a profile-declared surface."""

        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            return await async_fetch_profile_surface_resources(
                runtime,
                self.profile_transport,
                surface_key,
                payload=payload,
                use_cache=use_cache,
                now=self.now_fn(),
            )

    def profile_resource_descriptors(self, service_id: str) -> tuple[ResourceDescriptor, ...]:
        """Return generic metadata for profile-declared resources."""

        runtime = self.get_runtime(service_id)
        self._require_profile_dispatch_token(runtime, None)
        return profile_resource_descriptors(runtime)

    def profile_surface_descriptors(self, service_id: str) -> tuple[SurfaceDescriptor, ...]:
        """Return generic metadata for profile-declared surfaces."""

        runtime = self.get_runtime(service_id)
        self._require_profile_dispatch_token(runtime, None)
        return profile_surface_descriptors(runtime)

    def profile_surface_descriptor(self, service_id: str, surface_key: str) -> SurfaceDescriptor:
        """Return generic metadata for one profile-declared surface."""

        runtime = self.get_runtime(service_id)
        self._require_profile_dispatch_token(runtime, None)
        return profile_surface_descriptor(runtime, surface_key)

    async def async_validate_profile(
        self,
        service_id: str,
        target: ValidatorTarget,
        *,
        validator_keys: tuple[str, ...] | None = None,
        domains: tuple[ValidatorDomain, ...] | None = None,
        payload: Any | None = None,
    ) -> CapabilityVerdict:
        """Run profile-declared validators through generic dispatch."""

        runtime = self.get_runtime(service_id)
        self._require_profile_dispatch_token(runtime, None)
        return await async_validate_profile(
            runtime,
            self.profile_transport,
            target=target,
            validator_keys=validator_keys,
            domains=domains,
            payload=payload,
            now=self.now_fn(),
        )

    async def async_run_lifecycle_hooks(
        self,
        service_id: str,
        event: LifecycleEvent,
        *,
        payload: Any | None = None,
        force: bool = False,
        raise_blocking: bool = True,
    ) -> tuple[LifecycleHookResult, ...]:
        """Run profile-declared lifecycle hooks through generic dispatch."""

        runtime = self.get_runtime(service_id)

        async def operation() -> tuple[LifecycleHookResult, ...]:
            async with runtime.profile_operation_lock:
                return await async_run_lifecycle_hooks(
                    runtime,
                    self.profile_transport,
                    event,
                    payload=payload,
                    force=force,
                    raise_blocking=raise_blocking,
                    now=self.now_fn(),
                )

        return await self.async_track_profile_mutation(str(service_id), operation)

    async def async_read_editable_file(
        self,
        service_id: str,
        file_key: str,
        *,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> EditableFileSnapshot:
        """Read a profile-declared editable file."""

        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            return await async_read_editable_file(runtime, self.profile_transport, file_key, now=self.now_fn())

    async def async_preview_editable_file(
        self,
        service_id: str,
        file_key: str,
        proposed_value: Any,
        *,
        value_is_parsed: bool = False,
        expected_source_revision: str | None = None,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> EditableFilePreview:
        """Preview a profile-declared editable-file change."""

        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            return await async_preview_editable_file(
                runtime,
                self.profile_transport,
                file_key,
                proposed_value,
                value_is_parsed=value_is_parsed,
                expected_source_revision=expected_source_revision,
                now=self.now_fn(),
            )

    async def async_export_save_bundle(
        self,
        service_id: str,
        bundle_key: str,
        *,
        expected_profile: ProfileDispatchToken | None = None,
        progress: SaveBundleProgress | None = None,
    ) -> SaveBundleExport:
        """Build a portable archive from one profile-declared save tree."""

        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            if progress is not None:
                progress("confirming_stop", "Confirming the game server is still stopped before reading saves…", {})
            self._require_profile_dispatch_token(runtime, expected_profile)
            await self._async_require_fresh_save_bundle_stop(service_id)
            exported = await async_export_save_bundle(
                runtime,
                self.profile_transport,
                bundle_key,
                now=self.now_fn(),
                progress=progress,
            )
            try:
                if progress is not None:
                    progress("confirming_stop", "Reconfirming the server stayed stopped throughout the snapshot…", {})
                await self._async_require_fresh_save_bundle_stop(service_id)
            except BaseException:
                exported.close()
                raise
            return exported

    async def async_inspect_save_bundle(
        self,
        service_id: str,
        bundle_key: str,
        archive_stream: Any,
        *,
        expected_profile: ProfileDispatchToken | None = None,
        progress: SaveBundleProgress | None = None,
    ) -> SaveBundlePreview:
        """Inspect one uploaded save ZIP against exact stopped-server truth."""

        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            if progress is not None:
                progress("confirming_stop", "Confirming the game server is stopped before comparing saves…", {})
            self._require_profile_dispatch_token(runtime, expected_profile)
            await self._async_require_fresh_save_bundle_stop(service_id)
            preview = await async_inspect_save_bundle(
                runtime,
                self.profile_transport,
                bundle_key,
                archive_stream,
                now=self.now_fn(),
                progress=progress,
            )
            try:
                await self._async_require_fresh_save_bundle_stop(service_id)
            except BaseException:
                preview.close()
                raise
            return preview

    async def _async_require_fresh_save_bundle_stop(self, service_id: str) -> None:
        """Prove the server stayed stopped around an authoritative save read."""

        server = await self.client.fetch_server(str(service_id))
        if server.raw_status != STOPPED_STATUS:
            raise ProfileExtensionError(blocked("Save-game bundles require a freshly confirmed stopped server."))

    async def async_apply_save_bundle(
        self,
        service_id: str,
        preview: SaveBundlePreview,
        *,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> SaveBundleApplyResult:
        """Apply one exact reviewed save overlay with journaled rollback."""

        if not FILESYSTEM_TREE_REPLACE_ENABLED:
            raise ProfileExtensionError(blocked("Save-game restore is disabled pending controlled live acceptance."))
        self._ensure_active()
        runtime = self.get_runtime(service_id)
        dispatch = expected_profile or self.profile_dispatch_token(service_id)
        intent = OperationIntent(
            "save-bundle-restore",
            preview.key,
            scopes=("service",),
            generation=_dispatch_generation(dispatch),
            expected_evidence="filesystem journal verified the exact merged save tree or exact rollback",
        )
        task = asyncio.current_task()
        if task is not None:
            self._active_mutation_tasks.setdefault(str(service_id), set()).add(task)
        try:
            async with self.async_operation(service_id, intent) as reservation:
                self._require_profile_dispatch_token(runtime, expected_profile)
                self.ensure_filesystem_mutation_allowed(service_id)

                async def require_stopped() -> None:
                    self._require_profile_dispatch_token(runtime, expected_profile)
                    # This callback also runs while the filesystem mutation
                    # lock is held. A full profile refresh may read settings
                    # through the same lock, so use the provider's fresh
                    # status endpoint as the narrow write precondition.
                    server = await self.client.fetch_server(str(service_id))
                    if server.raw_status != STOPPED_STATUS:
                        raise ProfileExtensionError(
                            blocked("Save-game restore requires a freshly confirmed stopped server.")
                        )

                await require_stopped()
                async with self.profile_transport.authoritative_read(service_id):
                    current_snapshot = await self.profile_transport.snapshot_tree(
                        service_id,
                        preview.target_root,
                    )
                try:
                    active_current = active_save_manifest(current_snapshot.manifest, preview.excluded_paths)
                    if active_current != preview.current:
                        raise ProfileExtensionError(
                            blocked("The server save changed after review; upload and review the ZIP again.")
                        )
                    active_files = {item.path: current_snapshot.files[item.path] for item in active_current.entries}
                    active_proposed = OverlayTreeFiles(active_files, preview.upload.files)
                    if tree_manifest_sha256(preview.proposed) != tree_manifest_sha256(
                        type(preview.proposed).from_files(active_proposed)
                    ):
                        raise ProfileExtensionError(
                            blocked("The reviewed save overlay no longer matches its proposed manifest.")
                        )
                    proposed = OverlayTreeFiles(current_snapshot.files, preview.upload.files)
                    await reservation.async_mark_dispatched()
                    result = await self.profile_transport.replace_tree(
                        service_id,
                        preview.target_root,
                        proposed,
                        expected_current=current_snapshot.manifest,
                        require_stopped=require_stopped,
                    )
                    await reservation.async_mark_verifying()
                    return SaveBundleApplyResult(result.transaction_id, reservation.operation_id, preview.proposed)
                finally:
                    current_snapshot.close()
        finally:
            await self.async_refresh_filesystem_recovery_facts()
            if task is not None:
                active = self._active_mutation_tasks.get(str(service_id))
                if active is not None:
                    active.discard(task)
                    if not active:
                        self._active_mutation_tasks.pop(str(service_id), None)

    async def async_probe_editable_file_backup(
        self,
        service_id: str,
        file_key: str,
        backup_path: str,
        *,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> CapabilityVerdict:
        """Verify one durable recovery reference against the remote backup."""

        runtime = self.get_runtime(service_id)
        async with runtime.profile_operation_lock:
            self._require_profile_dispatch_token(runtime, expected_profile)
            return await async_probe_editable_file_backup(
                runtime,
                self.profile_transport,
                file_key,
                backup_path,
                now=self.now_fn(),
            )

    async def async_apply_editable_file(
        self,
        service_id: str,
        file_key: str,
        proposed_value: Any,
        *,
        value_is_parsed: bool = False,
        expected_profile: ProfileDispatchToken | None = None,
        expected_source_revision: str | None = None,
        expected_proposed_revision: str | None = None,
        expected_path: str | None = None,
    ) -> EditableFileApplyResult:
        """Apply a profile-declared editable-file change with backup."""

        self._ensure_active()
        runtime = self.get_runtime(service_id)
        if self.account_entry_id != "standalone" and not self._editable_file_journal_durable_bound:
            raise EditableFileJournalError("Durable editable-file recovery history is not initialized")
        await self.editable_file_journal.async_initialize()

        async def refresh_status() -> None:
            await self.async_refresh_service(service_id, handle_idle_shutdown=False)

        task = asyncio.current_task()
        if task is not None:
            self._active_mutation_tasks.setdefault(str(service_id), set()).add(task)
        try:
            dispatch = expected_profile or self.profile_dispatch_token(service_id)
            intent = OperationIntent(
                "editable-apply",
                str(file_key),
                scopes=("service",),
                generation=_dispatch_generation(dispatch),
                expected_evidence="editable-file journal verified the applied content or exact rollback",
            )
            async with self.async_operation(service_id, intent) as reservation:
                self._require_profile_dispatch_token(runtime, expected_profile)
                await reservation.async_mark_dispatched()
                try:
                    result = await async_apply_editable_file(
                        runtime,
                        self.profile_transport,
                        file_key,
                        proposed_value,
                        value_is_parsed=value_is_parsed,
                        now=self.now_fn(),
                        refresh_status=refresh_status,
                        expected_source_revision=expected_source_revision,
                        expected_proposed_revision=expected_proposed_revision,
                        expected_path=expected_path,
                    )
                except asyncio.CancelledError:
                    # Editable-file cancellation returns only after its exact
                    # compare-and-swap rollback has been verified. Preserve
                    # that stronger operation-specific evidence.
                    await reservation.async_complete("cancelled_rolled_back")
                    raise
                recovery = None
                if result.wrote and result.backup_path is not None:
                    recovery = await self.editable_file_journal.async_record(
                        account_entry_id=self.account_entry_id,
                        service_id=str(service_id),
                        file_key=str(file_key),
                        declared_path=result.preview.path,
                        backup_path=result.backup_path,
                        backup_kind="apply",
                        source_revision=result.preview.source_revision,
                        resulting_revision=result.preview.proposed_revision,
                        operation_id=reservation.operation_id,
                        created_at=self.now_fn(),
                    )
                await reservation.async_mark_verifying()
                return replace(
                    result,
                    operation_id=reservation.operation_id,
                    recovery_id=None if recovery is None else recovery.recovery_id,
                )
        finally:
            await self.async_refresh_filesystem_recovery_facts()
            if task is not None:
                active = self._active_mutation_tasks.get(str(service_id))
                if active is not None:
                    active.discard(task)
                    if not active:
                        self._active_mutation_tasks.pop(str(service_id), None)

    async def async_rollback_editable_file(
        self,
        service_id: str,
        file_key: str,
        backup_path: str,
        *,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> EditableFileRollbackResult:
        """Roll back a profile-declared editable file from a backup path."""

        self._ensure_active()
        runtime = self.get_runtime(service_id)
        if self.account_entry_id != "standalone" and not self._editable_file_journal_durable_bound:
            raise EditableFileJournalError("Durable editable-file recovery history is not initialized")
        await self.editable_file_journal.async_initialize()

        async def refresh_status() -> None:
            await self.async_refresh_service(service_id, handle_idle_shutdown=False)

        task = asyncio.current_task()
        if task is not None:
            self._active_mutation_tasks.setdefault(str(service_id), set()).add(task)
        try:
            dispatch = expected_profile or self.profile_dispatch_token(service_id)
            intent = OperationIntent(
                "editable-rollback",
                str(file_key),
                scopes=("service",),
                generation=_dispatch_generation(dispatch),
                expected_evidence="editable-file journal verified the restored content and pre-rollback backup",
            )
            async with self.async_operation(service_id, intent) as reservation:
                self._require_profile_dispatch_token(runtime, expected_profile)
                await reservation.async_mark_dispatched()
                try:
                    result = await async_rollback_editable_file(
                        runtime,
                        self.profile_transport,
                        file_key,
                        backup_path,
                        now=self.now_fn(),
                        refresh_status=refresh_status,
                    )
                except asyncio.CancelledError:
                    # The editable rollback path restores and verifies the
                    # original target before it propagates cancellation.
                    await reservation.async_complete("cancelled_rolled_back")
                    raise
                recovery = None
                if result.wrote and result.pre_rollback_backup_path is not None:
                    recovery = await self.editable_file_journal.async_record(
                        account_entry_id=self.account_entry_id,
                        service_id=str(service_id),
                        file_key=str(file_key),
                        declared_path=result.path,
                        backup_path=result.pre_rollback_backup_path,
                        backup_kind="pre_rollback",
                        source_revision=result.source_revision,
                        resulting_revision=result.resulting_revision,
                        operation_id=reservation.operation_id,
                        created_at=self.now_fn(),
                    )
                await reservation.async_mark_verifying()
                return replace(
                    result,
                    operation_id=reservation.operation_id,
                    recovery_id=None if recovery is None else recovery.recovery_id,
                )
        finally:
            await self.async_refresh_filesystem_recovery_facts()
            if task is not None:
                active = self._active_mutation_tasks.get(str(service_id))
                if active is not None:
                    active.discard(task)
                    if not active:
                        self._active_mutation_tasks.pop(str(service_id), None)

    def idle_shutdown_enabled(self, service_id: str) -> bool:
        """Return whether automatic idle shutdown is enabled for a service."""

        return str(service_id) in self.options.idle_shutdown_service_ids

    def maintenance_mode(self, service_id: str) -> bool:
        """Return whether maintenance mode is enabled for a service."""

        return str(service_id) in self.options.maintenance_service_ids

    def dry_run(self, service_id: str) -> bool:
        """Return whether dry-run mode is enabled for a service."""

        return str(service_id) in self.options.dry_run_service_ids

    def idle_limit_minutes(self, service_id: str) -> float:
        """Return configured idle shutdown minutes for a service."""

        return float(self.options.idle_minutes.get(str(service_id), DEFAULT_IDLE_MINUTES))

    def auto_shutdown_status(self, service_id: str) -> str | None:
        """Return a truthful human-readable Auto Shutdown runtime state."""

        runtime = self.get_runtime(service_id)
        server = runtime.server
        if server is None or not runtime.status_fresh or runtime.using_cached_data:
            return None

        status = server.raw_status
        if status == STOPPED_STATUS:
            return "Inactive — server stopped"
        if status in TRANSITION_STATUSES:
            return "Waiting for stable server status"
        if status != RUNNING_STATUS:
            return f"Inactive — server {status}"

        profile = runtime.profile
        if profile is None:
            return None
        if not getattr(profile, "idle_shutdown_supported", False):
            return "Unsupported for this game"
        if not self.idle_shutdown_enabled(service_id):
            return "Disabled"
        if self.maintenance_mode(service_id):
            return "Paused"
        if runtime.in_startup_cooldown(
            self.now_fn(),
            self.startup_cooldown_limit_minutes(service_id),
        ):
            return "Startup cooldown"
        if not self.idle_shutdown_supported(service_id):
            return None
        if not server.query_valid or server.player_count is None or server.player_count < 0:
            return None
        if runtime.shutdown_pending:
            return "Final checks"
        if server.player_count > 0:
            return "Waiting for zero players"
        if server.player_count != 0:
            return None
        if runtime.idle_started_at is None:
            return "Starting countdown"

        remaining = runtime.idle_remaining_minutes(
            runtime.refreshed_at or self.now_fn(),
            self.idle_limit_minutes(service_id),
        )
        return f"{remaining:.1f} min remaining"

    def startup_cooldown_limit_minutes(self, service_id: str) -> float:
        """Return configured startup cooldown minutes for a service."""

        return float(self.options.startup_cooldown_minutes.get(str(service_id), DEFAULT_STARTUP_COOLDOWN_MINUTES))

    def get_runtime(self, service_id: str) -> ServiceRuntime:
        """Return runtime for a managed service."""

        runtime = self.services.get(str(service_id))
        if runtime is None:
            raise NitradoServiceUnknownError(str(service_id))
        return runtime

    def profile_dispatch_token(self, service_id: str) -> ProfileDispatchToken:
        """Return the exact runtime/profile generation authorized by a caller."""

        runtime = self.get_runtime(service_id)
        return (
            id(runtime),
            str(getattr(runtime.profile, "profile_id", "")),
            runtime.profile_generation,
            runtime.profile_registration_generation,
            runtime.profile_registry_generation_seen,
        )

    def require_profile_dispatch_token(
        self,
        service_id: str,
        expected: ProfileDispatchToken,
    ) -> ServiceRuntime:
        """Return the runtime only if it still matches an authorized snapshot."""

        runtime = self.get_runtime(service_id)
        self._require_profile_dispatch_token(runtime, expected)
        return runtime

    def _require_profile_dispatch_token(
        self,
        runtime: ServiceRuntime,
        expected: ProfileDispatchToken | None,
    ) -> None:
        """Fail closed when authorization and dispatch see different profile code."""

        if self._shutting_down:
            raise ProfileExtensionError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="The integration entry is reloading; retry after it finishes.",
                    overridable=False,
                )
            )
        profile_id = str(getattr(runtime.profile, "profile_id", ""))
        if runtime.profile_registry_generation_seen != profile_registry_generation():
            raise ProfileExtensionError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="The game profile registry changed; retry after the integration reloads.",
                    overridable=False,
                )
            )
        if runtime.profile_registration_generation != profile_registration_generation(profile_id):
            raise ProfileExtensionError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="The selected game profile registration changed; retry after the integration reloads.",
                    overridable=False,
                )
            )
        if expected is None:
            return
        current = (
            id(runtime),
            str(getattr(runtime.profile, "profile_id", "")),
            runtime.profile_generation,
            runtime.profile_registration_generation,
            runtime.profile_registry_generation_seen,
        )
        if current != expected:
            raise ProfileExtensionError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="The selected game profile changed after authorization; retry the request.",
                    overridable=False,
                )
            )

    def _ensure_active(self) -> None:
        """Reject new destructive work once config-entry unload has begun."""

        if self._shutting_down:
            raise NitradoControlError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="The integration entry is unloading; the command was not sent.",
                    overridable=False,
                )
            )

    @property
    def shutting_down(self) -> bool:
        """Return whether config-entry teardown has begun."""

        return self._shutting_down

    def _ensure_service_runtime_active(self, service_id: str, runtime: ServiceRuntime) -> None:
        """Reject transport when unload/removal/re-import changed service ownership."""

        self._ensure_active()
        if self.services.get(str(service_id)) is not runtime:
            raise NitradoControlError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="The managed service changed while the command was pending; the command was not sent.",
                    overridable=False,
                )
            )

    def snapshot(self) -> AccountRuntimeSnapshot:
        """Return a compact account runtime snapshot."""

        return AccountRuntimeSnapshot(
            managed_service_ids=tuple(sorted(self.known)),
            runtime_service_ids=tuple(sorted(self.services)),
            pending_service_ids=tuple(
                sorted(service_id for service_id, state in self.known.items() if state.pending_discovery)
            ),
            missing_service_ids=tuple(
                sorted(service_id for service_id, state in self.known.items() if not state.available)
            ),
            last_discovery_result=self.last_discovery_result,
        )

    def _start_game_short(self, runtime: ServiceRuntime) -> str:
        game_short = None
        if runtime.server:
            game_short = runtime.server.game_short
        if not game_short and runtime.service:
            game_short = runtime.service.game or runtime.service.folder_short
        if not game_short:
            raise NitradoControlError(
                CapabilityVerdict(
                    CapabilityState.BLOCKED,
                    reason="Nitrado game identifier is unavailable; start was not sent.",
                    overridable=False,
                )
            )
        return game_short

    async def _refresh_control_verdicts(self, runtime: ServiceRuntime) -> None:
        """Refresh cached safe-control verdicts for UI/entity state."""

        async with runtime.profile_operation_lock:
            now = self.now_fn()
            runtime.last_start_verdict = await evaluate_start(
                runtime,
                now=now,
                settle_seconds=self.options.settle_seconds,
            )
            runtime.last_stop_verdict = await evaluate_stop(runtime)
            runtime.last_idle_shutdown_verdict = await self._async_idle_shutdown_capability(runtime)

    async def _async_run_control_hooks(
        self,
        runtime: ServiceRuntime,
        event: LifecycleEvent,
        *,
        payload: Any | None = None,
        force: bool = False,
        raise_blocking: bool = True,
        expected_profile: ProfileDispatchToken | None = None,
    ) -> tuple[LifecycleHookResult, ...]:
        """Run lifecycle hooks for a control action and translate failures."""

        service_id = runtime.state.identity.service_id
        if not raise_blocking and (self._shutting_down or self.services.get(str(service_id)) is not runtime):
            return ()

        try:

            async def operation() -> tuple[LifecycleHookResult, ...]:
                async with runtime.profile_operation_lock:
                    try:
                        self._require_profile_dispatch_token(runtime, expected_profile)
                    except ProfileExtensionError:
                        if raise_blocking:
                            raise
                        _LOGGER.info(
                            "Skipping stale nonblocking %s lifecycle hook after profile replacement",
                            event.value,
                        )
                        return ()
                    return await async_run_lifecycle_hooks(
                        runtime,
                        self.profile_transport,
                        event,
                        payload=payload,
                        force=force,
                        raise_blocking=raise_blocking,
                        now=self.now_fn(),
                    )

            # The complete Start/Stop task is already owned by
            # ``async_track_control_operation``. Registering the same task as
            # a generic profile mutation would let unload cancel a command
            # after transport succeeded while its after-hook was running.
            return await operation()
        except ProfileExtensionError as err:
            raise NitradoControlError(err.verdict) from err

    async def _async_run_nonblocking_lifecycle(
        self,
        runtime: ServiceRuntime,
        event: LifecycleEvent,
        *,
        payload: Any | None = None,
    ) -> tuple[LifecycleHookResult, ...]:
        """Dispatch an observational lifecycle event without poisoning refresh."""

        service_id = runtime.state.identity.service_id
        if (
            self._shutting_down
            or service_id in self._service_exclusion_tombstones
            or self.services.get(service_id) is not runtime
        ):
            return ()
        try:

            async def operation() -> tuple[LifecycleHookResult, ...]:
                async with runtime.profile_operation_lock:
                    if (
                        self._shutting_down
                        or service_id in self._service_exclusion_tombstones
                        or self.services.get(service_id) is not runtime
                    ):
                        return ()
                    return await async_run_lifecycle_hooks(
                        runtime,
                        self.profile_transport,
                        event,
                        payload=payload,
                        raise_blocking=False,
                        now=self.now_fn(),
                    )

            results = await self.async_track_profile_mutation(service_id, operation)
        except Exception:
            _LOGGER.exception(
                "Profile %s lifecycle event %s failed safely",
                getattr(runtime.profile, "profile_id", "unknown"),
                event.value,
            )
            runtime.last_lifecycle_dispatch_errors[event.value] = (
                "Lifecycle dispatch failed safely; details were logged."
            )
            return ()
        runtime.last_lifecycle_dispatch_errors.pop(event.value, None)
        return results

    async def _async_handle_idle_shutdown(self, runtime: ServiceRuntime) -> None:
        """Update idle shutdown state and send Stop only after safe final checks."""

        if self._shutting_down:
            return
        service_id = runtime.state.identity.service_id
        if runtime.server is None:
            self.revoke_pending_shutdown(service_id, "No server status is available")
            return
        if runtime.using_cached_data or not runtime.status_fresh:
            self.revoke_pending_shutdown(service_id, "Status is cached or stale")
            return
        if runtime.server.raw_status != RUNNING_STATUS:
            self.revoke_pending_shutdown(
                service_id,
                f"Server status is {runtime.server.raw_status}; automatic shutdown suppressed",
            )
            return
        capability = runtime.last_idle_shutdown_verdict or CapabilityVerdict(
            CapabilityState.BLOCKED,
            reason="Idle-shutdown capability has not been evaluated against fresh runtime state.",
        )
        if not capability.allowed:
            self.revoke_pending_shutdown(
                service_id,
                capability.reason or "Selected game profile does not support automatic idle shutdown",
            )
            return
        if runtime.in_startup_cooldown(self.now_fn(), self.startup_cooldown_limit_minutes(service_id)):
            self.revoke_pending_shutdown(service_id, "Startup cooldown active")
            return
        if self.maintenance_mode(service_id):
            self.revoke_pending_shutdown(service_id, "Maintenance mode enabled")
            return
        if not self.idle_shutdown_enabled(service_id):
            self.revoke_pending_shutdown(service_id, "Automatic shutdown disabled")
            return
        if not runtime.server.query_valid or runtime.server.player_count is None:
            self.revoke_pending_shutdown(service_id, "Player count missing, malformed, or stale")
            return
        if runtime.server.player_count > 0:
            self.revoke_pending_shutdown(service_id, "Players online")
            return
        if runtime.server.player_count != 0:
            self.revoke_pending_shutdown(service_id, "Player count was not explicit zero")
            return
        now = self.now_fn()
        if runtime.idle_started_at is None:
            runtime.idle_started_at = now
            runtime.last_reset_reason = "Idle timer started after valid zero-player poll"

        if now - runtime.idle_started_at < self.idle_limit_minutes(service_id) * 60:
            return
        await self._async_run_final_shutdown_check(runtime, "zero players for configured idle period")

    async def _async_run_final_shutdown_check(self, runtime: ServiceRuntime, reason: str) -> None:
        """Run the one tracked final-check task for a service."""

        if self._shutting_down:
            return
        service_id = runtime.state.identity.service_id
        existing = self._shutdown_tasks.get(service_id)
        if existing is not None and not existing.done():
            return
        generation = runtime.shutdown_generation
        task = asyncio.create_task(
            self._async_final_shutdown_check(runtime, reason, generation),
            name=f"nitrado-idle-shutdown-{service_id}",
        )
        self._shutdown_tasks[service_id] = task
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            if self._shutdown_tasks.get(service_id) is task:
                self._shutdown_tasks.pop(service_id, None)

    async def _async_final_shutdown_check(
        self,
        runtime: ServiceRuntime,
        reason: str,
        generation: int,
    ) -> None:
        """Run two fresh zero-player checks before automatic shutdown."""

        if self._shutting_down:
            return
        if runtime.shutdown_pending:
            return
        runtime.shutdown_pending = True
        self._notify_listeners()
        service_id = runtime.state.identity.service_id
        try:
            first = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
            if not self._shutdown_intent_valid(first, generation):
                if first.shutdown_generation == generation:
                    first.reset_idle_timer("Final shutdown check #1 was not valid zero")
                return
            delay = max(0, int(self.options.final_shutdown_check_delay_seconds))
            if delay:
                await asyncio.sleep(delay)
            second = await self.async_refresh_service(service_id, handle_idle_shutdown=False)
            if not self._shutdown_intent_valid(second, generation):
                if second.shutdown_generation == generation:
                    second.reset_idle_timer("Final shutdown check #2 was not valid zero")
                return
            if self.dry_run(service_id):
                second.last_shutdown_reason = (
                    f"Dry-run: would stop after {second.idle_minutes(self.now_fn()):.1f} idle minutes"
                )
                second.reset_idle_timer("Dry-run shutdown simulation completed")
                return
            try:
                await self.async_stop_service(
                    service_id,
                    reason=reason,
                    _pre_send_guard=lambda: self._shutdown_intent_valid(second, generation),
                )
            except NitradoControlError as err:
                second.reset_idle_timer(f"Automatic shutdown aborted: {err}")
                return
            second.last_shutdown_reason = reason
            second.reset_idle_timer("Stop confirmed")
        finally:
            runtime.shutdown_pending = False
            self._notify_listeners()

    def _is_safe_zero_for_shutdown(self, runtime: ServiceRuntime) -> bool:
        """Return true when runtime has fresh, profile-supported zero-player data."""

        service_id = runtime.state.identity.service_id
        return (
            self.idle_shutdown_supported(service_id)
            and self.idle_shutdown_enabled(service_id)
            and not self.maintenance_mode(service_id)
            and not runtime.in_startup_cooldown(
                self.now_fn(),
                self.startup_cooldown_limit_minutes(service_id),
            )
            and runtime.server is not None
            and runtime.server.raw_status == RUNNING_STATUS
            and runtime.status_fresh
            and not runtime.using_cached_data
            and runtime.server.query_valid
            and runtime.server.player_count == 0
        )

    def _shutdown_intent_valid(self, runtime: ServiceRuntime, generation: int) -> bool:
        """Return whether the original automatic-shutdown intent is still current."""

        profile_id = str(getattr(runtime.profile, "profile_id", ""))
        return (
            not self._shutting_down
            and runtime.shutdown_generation == generation
            and runtime.profile_registry_generation_seen == profile_registry_generation()
            and runtime.profile_registration_generation == profile_registration_generation(profile_id)
            and self._is_safe_zero_for_shutdown(runtime)
        )

    @staticmethod
    def _shutdown_revoked_verdict() -> CapabilityVerdict:
        """Return a verdict for an automatic shutdown revoked before transport."""

        return CapabilityVerdict(
            CapabilityState.BLOCKED,
            reason="Automatic shutdown intent was revoked before Stop was sent.",
            overridable=False,
        )

    async def _async_idle_shutdown_capability(self, runtime: ServiceRuntime) -> CapabilityVerdict:
        """Evaluate the selected profile's idle-shutdown capability off-loop."""

        if runtime.profile is None or not getattr(runtime.profile, "idle_shutdown_supported", False):
            return CapabilityVerdict(
                CapabilityState.UNSUPPORTED,
                reason="Selected game profile does not support automatic idle shutdown",
            )
        # Player-source capability is actionable only while the provider says
        # the server is freshly and stably running. Stopped and transitional
        # states are normal; stale/cached status has its own core-level reason.
        if (
            runtime.server is None
            or not runtime.status_fresh
            or runtime.using_cached_data
            or runtime.server.raw_status != RUNNING_STATUS
        ):
            return CapabilityVerdict(CapabilityState.SUPPORTED)
        capability = getattr(runtime.profile, "idle_shutdown_capability", None)
        if capability is None:
            return CapabilityVerdict(CapabilityState.SUPPORTED)
        try:
            verdict = await async_invoke_profile(
                capability,
                ControlContext(
                    service=runtime.service,
                    server=runtime.server,
                    status_fresh=runtime.status_fresh,
                    using_cached_data=runtime.using_cached_data,
                    extra=runtime.profile_extra(),
                    options=dict(runtime.extra.get("_persisted_profile_options", {})),
                ),
            )
        except Exception:
            return CapabilityVerdict(
                CapabilityState.BLOCKED,
                reason="Selected game profile failed its idle-shutdown capability check.",
            )
        if not valid_capability_verdict(verdict):
            return CapabilityVerdict(
                CapabilityState.BLOCKED,
                reason="Selected game profile returned an invalid idle-shutdown capability verdict.",
            )
        return verdict


def coordinator_from_hass(hass: Any, entry: Any) -> NitradoAccountCoordinator:
    """Return this integration's account coordinator from Home Assistant data."""

    from .const import DOMAIN

    return hass.data[DOMAIN][entry.entry_id]


def _server_updates_from_profile_status(profile_status: Any) -> dict[str, Any]:
    """Return ParsedServer fields explicitly overridden by profile enrichment."""

    server_updates: dict[str, Any] = {}
    if profile_status.player_count is not UNSET:
        server_updates["player_count"] = profile_status.player_count
    if profile_status.player_max is not UNSET:
        server_updates["player_max"] = profile_status.player_max
    if profile_status.player_names is not UNSET:
        server_updates["player_names"] = profile_status.player_names
    if profile_status.query_valid is not UNSET:
        server_updates["query_valid"] = profile_status.query_valid
    if profile_status.player_source is not UNSET:
        server_updates["player_source"] = profile_status.player_source
    return server_updates


def _fail_player_data_closed(runtime: ServiceRuntime) -> None:
    """Remove automation-authoritative player data after profile failure."""

    if runtime.server is None:
        return
    runtime.server = replace(
        runtime.server,
        player_count=None,
        player_names=(),
        query_valid=False,
        player_source=None,
    )


def _valid_profile_status(value: Any) -> bool:
    """Return whether profile enrichment is safe to merge into core status."""

    try:
        _normalize_profile_status(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return True


def _normalize_profile_status(value: Any) -> ProfileStatus | None:
    """Detach one bounded profile enrichment result outside HA's event loop."""

    if value is None:
        return None
    if type(value) is not ProfileStatus or type(value.extra) is not dict:
        raise TypeError("profile status must use the exact public value type")
    encoded_extra = json.dumps(value.extra, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded_extra) > 65_536:
        raise ValueError("profile status extra data exceeds the supported size")
    detached_extra = json.loads(encoded_extra)
    if type(detached_extra) is not dict:
        raise TypeError("profile status extra data must be an object")
    for number in (value.player_count, value.player_max):
        if number is not UNSET and number is not None and (type(number) is not int or number < 0):
            raise TypeError("profile status counts must be non-negative integers")
    if value.player_names is not UNSET and (
        type(value.player_names) is not tuple
        or len(value.player_names) > 10_000
        or any(type(name) is not str or len(name) > 512 for name in value.player_names)
    ):
        raise TypeError("profile status player names are invalid")
    if (
        value.player_source is not UNSET
        and value.player_source is not None
        and (type(value.player_source) is not str or len(value.player_source) > 128)
    ):
        raise TypeError("profile status player source is invalid")
    if value.query_valid is not UNSET and value.query_valid is not None and type(value.query_valid) is not bool:
        raise TypeError("profile status query validity is invalid")
    if (
        value.display_name is not UNSET
        and value.display_name is not None
        and (type(value.display_name) is not str or len(value.display_name) > 512)
    ):
        raise TypeError("profile status display name is invalid")
    return ProfileStatus(
        player_count=value.player_count,
        player_max=value.player_max,
        player_names=value.player_names,
        player_source=value.player_source,
        query_valid=value.query_valid,
        display_name=value.display_name,
        extra=detached_extra,
    )


def _valid_hosted_entity_option(entity: Any, value: Any) -> bool:
    """Validate one option persisted by a profile-hosted HA control."""

    if entity.platform == "switch":
        return type(value) is bool
    if entity.platform == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return False
        minimum = entity.attributes.get("native_min_value")
        maximum = entity.attributes.get("native_max_value")
        return not ((minimum is not None and value < minimum) or (maximum is not None and value > maximum))
    if entity.platform == "select":
        options = entity.attributes.get("options")
        if not isinstance(value, str):
            return False
        if isinstance(options, (tuple, list)):
            return value in options
        return entity.options_fn is not None
    return False


def _safe_profile_entity_value(entity: Any, field: str, value: Any) -> Any:
    """Detach one passive callback result into event-loop-safe primitives."""

    if field == "available":
        if type(value) is not bool:
            raise TypeError("profile entity availability must be boolean")
        return value
    if field == "options":
        if type(value) not in {list, tuple} or len(value) > 256:
            raise TypeError("profile select options must be a bounded list")
        if any(type(item) is not str or not item or len(item) > 512 for item in value):
            raise TypeError("profile select options must contain bounded strings")
        return tuple(value)
    platform = str(entity.platform)
    if value is None:
        return None
    if platform in {"binary_sensor", "switch"}:
        if type(value) is not bool:
            raise TypeError("profile boolean entity value must be boolean")
        return value
    if platform == "number":
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise TypeError("profile number entity value must be finite")
        return value
    if platform == "select":
        if type(value) is not str or len(value) > 512:
            raise TypeError("profile select value must be a bounded string")
        return value
    if platform == "sensor":
        if type(value) is str:
            if len(value) > 4096:
                raise TypeError("profile sensor value is too long")
            return value
        if type(value) in {bool, int, float}:
            if type(value) is float and not math.isfinite(value):
                raise TypeError("profile sensor value must be finite")
            return value
    if platform == "button" and value is None:
        return None
    raise TypeError("profile entity returned an unsupported passive value")
