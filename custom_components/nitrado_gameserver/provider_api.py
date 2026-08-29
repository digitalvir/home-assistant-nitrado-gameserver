"""Versioned, typed provider API for trusted companion integrations.

The API deliberately exposes capabilities rather than transports.  Consumers
never receive the Nitrado client, FTP credentials, filesystem service, or
Start/Stop controls.  Every mutation is an immutable, expiring, single-use
plan and requires an opaque approval issued and consumed by core.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import secrets
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .backups import (
    MAX_NATIVE_BACKUPS,
    NativeBackup,
    NativeBackupIdentity,
    NativeBackupInventory,
    NativeBackupRestoreRequest,
    NativeBackupRestoreResult,
)

PROVIDER_CONNECTOR_API_VERSION = 2
DEFAULT_PLAN_TTL_SECONDS = 300
MAX_PLAN_TTL_SECONDS = 900
MAX_PROVIDER_SCOPES = 16
MAX_IDENTITY_CHARS = 128
MAX_PROVIDER_FILE_BYTES = 128 * 1024 * 1024
MAX_PROVIDER_TREE_BYTES = 256 * 1024 * 1024
MAX_PROVIDER_TREE_FILES = 5_000
MAX_PROVIDER_TREE_DEPTH = 32
DEFAULT_PROVIDER_TREE_CHUNK_BYTES = 256 * 1024
MAX_PROVIDER_TREE_CHUNK_BYTES = 1024 * 1024
MAX_PLANS_PER_CONNECTOR = 64
MAX_PLANS_TOTAL = 1024
MAX_PENDING_PLAN_BYTES_PER_CONNECTOR = MAX_PROVIDER_TREE_BYTES
MAX_PENDING_PLAN_BYTES_TOTAL = 2 * MAX_PROVIDER_TREE_BYTES
MAX_PROGRESS_RECORDS = 1024
MAX_CONSUMER_LEASES = 32
MAX_CONNECTORS_TOTAL = 64
MAX_CONNECTORS_PER_LEASE = 8
MAX_CONNECTORS_PER_SERVICE = 16
MAX_CONTENT_HANDLES_TOTAL = 4
MAX_CONTENT_HANDLES_PER_CONNECTOR = 2
MAX_OPEN_SNAPSHOT_BYTES_TOTAL = 512 * 1024 * 1024
MAX_OPEN_SNAPSHOT_BYTES_PER_CONNECTOR = MAX_OPEN_SNAPSHOT_BYTES_TOTAL
MAX_INFLIGHT_FILE_READ_BYTES_TOTAL = 2 * MAX_PROVIDER_FILE_BYTES
MAX_INFLIGHT_FILE_READ_BYTES_PER_CONNECTOR = MAX_PROVIDER_FILE_BYTES
MAX_INFLIGHT_OPERATIONS_TOTAL = 32
MAX_INFLIGHT_OPERATIONS_PER_SERVICE = 8
_REGISTRY_DATA_KEY = "nitrado_gameserver_provider_connector_registry"


class ProviderApiError(Exception):
    """Base error for provider connector contract failures."""


class ProviderAuthorizationError(ProviderApiError):
    """Raised when a grant or core-issued approval is missing or invalid."""


class ProviderLeaseRevokedError(ProviderApiError):
    """Raised after consumer or service connector authority is revoked."""


class ProviderPlanError(ProviderApiError):
    """Raised when a mutation plan is stale, mismatched, or already consumed."""


class ProviderMutationExecutionError(ProviderApiError):
    """Provider mutation failed without a trustworthy final outcome."""

    provider_outcome_unknown = True

    def __init__(self, *, audit_id: str, action: str) -> None:
        self.audit_id = audit_id
        self.action = action
        super().__init__(f"Provider mutation {action} did not produce a confirmed outcome (audit {audit_id})")


class ProviderScope(StrEnum):
    """Independently grantable provider capabilities."""

    FILESYSTEM_READ = "filesystem:read"
    FILESYSTEM_SNAPSHOT = "filesystem:snapshot"
    FILESYSTEM_REPLACE = "filesystem:replace"
    NATIVE_BACKUP_READ = "native_backup:read"
    NATIVE_BACKUP_RESTORE = "native_backup:restore"


_FILESYSTEM_SCOPES = frozenset(
    {
        ProviderScope.FILESYSTEM_READ,
        ProviderScope.FILESYSTEM_SNAPSHOT,
        ProviderScope.FILESYSTEM_REPLACE,
    }
)


@dataclass(frozen=True, slots=True)
class ProviderServiceRef:
    """Account-scoped service identity; bare service IDs are forbidden."""

    account_entry_id: str
    service_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_entry_id", _identity(self.account_entry_id, "account entry ID"))
        service_id = str(self.service_id).strip()
        if not service_id.isdigit():
            raise ValueError("Nitrado service ID must contain only digits")
        object.__setattr__(self, "service_id", service_id)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Capabilities currently implemented by one service backend."""

    scopes: frozenset[ProviderScope]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", _scopes(self.scopes))


@dataclass(frozen=True, slots=True)
class ProviderGrantRequest:
    consumer_domain: str
    consumer_entry_id: str
    service_ref: ProviderServiceRef
    requested_scopes: frozenset[ProviderScope]
    requested_roots: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_roots", _roots(self.requested_roots))


@dataclass(frozen=True, slots=True)
class ProviderGrant:
    """Exact authority issued by the core-owned grant workflow."""

    grant_id: str
    consumer_domain: str
    consumer_entry_id: str
    service_ref: ProviderServiceRef
    scopes: frozenset[ProviderScope]
    expires_at: float | None = None
    permitted_roots: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "grant_id", _identity(self.grant_id, "grant ID"))
        object.__setattr__(self, "consumer_domain", _identity(self.consumer_domain, "consumer domain"))
        object.__setattr__(self, "consumer_entry_id", _identity(self.consumer_entry_id, "consumer entry ID"))
        object.__setattr__(self, "scopes", _scopes(self.scopes))
        object.__setattr__(self, "permitted_roots", _roots(self.permitted_roots))
        if self.expires_at is not None and self.expires_at <= 0:
            raise ValueError("grant expiry must be a positive timestamp")


@dataclass(frozen=True, slots=True)
class ProviderMutationApproval:
    """Opaque, core-issued one-shot approval for one exact plan.

    The token contains no companion-controlled user identity or approval bool.
    Core's authorizer must atomically validate and consume it.
    """

    plan_id: str
    payload_digest: str
    approval_token: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _identity(self.plan_id, "plan ID"))
        object.__setattr__(self, "payload_digest", _digest(self.payload_digest))
        object.__setattr__(self, "approval_token", _identity(self.approval_token, "approval token"))


class ProviderMutationState(StrEnum):
    """Sanitized lifecycle state for one long-running provider mutation."""

    PLANNED = "planned"
    AUTHORIZING = "authorizing"
    RUNNING = "running"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderMutationProgress:
    """Bounded, secret-free progress for one connector-owned mutation."""

    plan_id: str
    audit_id: str
    action: str
    state: ProviderMutationState
    updated_at: float
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderMutationPlan:
    """Immutable, expiring, single-use mutation plan."""

    plan_id: str
    audit_id: str
    action: str
    service_ref: ProviderServiceRef
    consumer_domain: str
    consumer_entry_id: str
    grant_id: str
    payload_digest: str
    summary: str
    created_at: float
    expires_at: float
    _execution_token: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _identity(self.plan_id, "plan ID"))
        object.__setattr__(self, "audit_id", _identity(self.audit_id, "audit ID"))
        object.__setattr__(self, "action", _identity(self.action, "plan action"))
        object.__setattr__(self, "payload_digest", _digest(self.payload_digest))
        if self.expires_at <= self.created_at:
            raise ValueError("mutation plan must expire after it is created")


@dataclass(frozen=True, slots=True)
class ProviderFileReadRequest:
    path: str
    max_bytes: int = MAX_PROVIDER_FILE_BYTES

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path, allow_root=False))
        if not 1 <= self.max_bytes <= MAX_PROVIDER_FILE_BYTES:
            raise ValueError("provider file read limit is outside the supported range")


@dataclass(frozen=True, slots=True)
class ProviderFileContent:
    service_ref: ProviderServiceRef
    path: str
    content: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path, allow_root=False))
        content = bytes(self.content)
        if len(content) > MAX_PROVIDER_FILE_BYTES:
            raise ValueError("provider file content exceeded its hard limit")
        object.__setattr__(self, "content", content)
        if _sha256(content) != _digest(self.sha256):
            raise ValueError("provider file content digest does not match")


@dataclass(frozen=True, slots=True, order=True)
class ProviderTreeManifestEntry:
    path: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path, allow_root=False))
        if not 0 <= self.size_bytes <= MAX_PROVIDER_FILE_BYTES:
            raise ValueError("provider tree entry size is outside the supported range")
        object.__setattr__(self, "sha256", _digest(self.sha256))


@dataclass(frozen=True, slots=True)
class ProviderTreeManifest:
    entries: tuple[ProviderTreeManifestEntry, ...]
    total_bytes: int
    digest: str

    def __post_init__(self) -> None:
        entries = tuple(sorted(self.entries))
        paths = {item.path for item in entries}
        aliases = {item.path.casefold() for item in entries}
        if len(entries) > MAX_PROVIDER_TREE_FILES or len(paths) != len(entries) or len(aliases) != len(entries):
            raise ValueError("provider tree manifest has invalid or duplicate entries")
        total = sum(item.size_bytes for item in entries)
        if total != self.total_bytes or total > MAX_PROVIDER_TREE_BYTES:
            raise ValueError("provider tree manifest byte total is invalid")
        object.__setattr__(self, "entries", entries)
        if _manifest_digest(entries) != _digest(self.digest):
            raise ValueError("provider tree manifest digest does not match")

    @classmethod
    def from_files(cls, files: Iterable[ProviderTreeFile]) -> ProviderTreeManifest:
        items = tuple(files)
        entries = tuple(ProviderTreeManifestEntry(item.path, len(item.content), item.sha256) for item in items)
        return cls(entries, sum(item.size_bytes for item in entries), _manifest_digest(entries))

    @classmethod
    def from_entries(cls, entries: Iterable[ProviderTreeManifestEntry]) -> ProviderTreeManifest:
        """Build an exact manifest without materializing file content."""

        items = tuple(entries)
        return cls(items, sum(item.size_bytes for item in items), _manifest_digest(items))


@dataclass(frozen=True, slots=True)
class ProviderTreeFile:
    path: str
    content: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path, allow_root=False))
        content = bytes(self.content)
        if len(content) > MAX_PROVIDER_FILE_BYTES:
            raise ValueError("provider tree file exceeded its hard limit")
        object.__setattr__(self, "content", content)
        if _sha256(content) != _digest(self.sha256):
            raise ValueError("provider tree file digest does not match")

    @classmethod
    def from_bytes(cls, path: str, content: bytes) -> ProviderTreeFile:
        data = bytes(content)
        return cls(path, data, _sha256(data))


@dataclass(frozen=True, slots=True)
class ProviderTreeContentChunk:
    """One bounded logical-file chunk from a provider tree snapshot."""

    path: str
    offset: int
    content: bytes = field(repr=False)
    eof: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_path(self.path, allow_root=False))
        if self.offset < 0:
            raise ValueError("provider tree chunk offset must not be negative")
        content = bytes(self.content)
        if len(content) > MAX_PROVIDER_TREE_CHUNK_BYTES:
            raise ValueError("provider tree chunk exceeded its hard limit")
        if not content and self.eof is not True:
            raise ValueError("an empty provider tree chunk must end its file")
        object.__setattr__(self, "content", content)


ProviderTreeChunkSource = Callable[[int], AsyncIterator[ProviderTreeContentChunk]]


class ProviderTreeContentHandle:
    """Revocable, one-consumer, exactly verified tree-content stream.

    The handle deliberately exposes no local file descriptor or temporary
    path.  Core validates every logical path, offset, size, and digest while
    the consumer iterates.  Closing, lease revocation, cancellation, or normal
    exhaustion deterministically releases the backing snapshot.
    """

    def __init__(
        self,
        manifest: ProviderTreeManifest,
        source: ProviderTreeChunkSource,
        close: Callable[[], None],
    ) -> None:
        if not isinstance(manifest, ProviderTreeManifest) or not callable(source) or not callable(close):
            raise TypeError("provider tree content handle construction is invalid")
        self.manifest = manifest
        self._source = source
        self._close = close
        self._started = False
        self._closed = False
        self._revoked = False
        self._owner_closed: Callable[[ProviderTreeContentHandle], None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def consumed(self) -> bool:
        return self._started

    async def async_chunks(
        self,
        *,
        chunk_bytes: int = DEFAULT_PROVIDER_TREE_CHUNK_BYTES,
    ) -> AsyncIterator[ProviderTreeContentChunk]:
        """Yield one exactly validated stream; a second consumer is refused."""

        if not 1 <= chunk_bytes <= MAX_PROVIDER_TREE_CHUNK_BYTES:
            raise ValueError("provider tree chunk size is outside the supported range")
        if self._revoked:
            raise ProviderLeaseRevokedError("Provider tree content authority is revoked")
        if self._closed:
            raise ProviderApiError("Provider tree content is closed")
        if self._started:
            raise ProviderApiError("Provider tree content permits exactly one consumer")
        self._started = True
        entry_index = 0
        offset = 0
        digest = hashlib.sha256()
        try:
            async for chunk in self._source(chunk_bytes):
                if self._revoked:
                    raise ProviderLeaseRevokedError("Provider tree content authority is revoked")
                if not isinstance(chunk, ProviderTreeContentChunk):
                    raise ProviderApiError("Provider tree source returned an invalid chunk")
                if len(chunk.content) > chunk_bytes:
                    raise ProviderApiError("Provider tree source exceeded the requested chunk size")
                if entry_index >= len(self.manifest.entries):
                    raise ProviderApiError("Provider tree source returned unexpected content")
                expected = self.manifest.entries[entry_index]
                if chunk.path != expected.path or chunk.offset != offset:
                    raise ProviderApiError("Provider tree source path or offset does not match its manifest")
                offset += len(chunk.content)
                if offset > expected.size_bytes:
                    raise ProviderApiError("Provider tree source exceeded its declared file size")
                digest.update(chunk.content)
                if chunk.eof:
                    if offset != expected.size_bytes or digest.hexdigest() != expected.sha256:
                        raise ProviderApiError("Provider tree source failed exact file verification")
                    entry_index += 1
                    offset = 0
                    digest = hashlib.sha256()
                elif offset >= expected.size_bytes:
                    raise ProviderApiError("Provider tree source omitted the required file terminator")
                yield chunk
            if entry_index != len(self.manifest.entries) or offset != 0:
                raise ProviderApiError("Provider tree source ended before its manifest was satisfied")
        finally:
            self._close_now()

    async def async_close(self) -> None:
        """Release unconsumed or partially consumed content."""

        self._close_now()

    def _bind_owner(self, closed: Callable[[ProviderTreeContentHandle], None]) -> None:
        if self._owner_closed is not None:
            raise ProviderApiError("Provider tree content already belongs to a connector")
        self._owner_closed = closed

    def _revoke(self) -> None:
        self._revoked = True
        self._close_now()

    def _close_now(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._close()
        finally:
            if self._owner_closed is not None:
                self._owner_closed(self)
                self._owner_closed = None


@dataclass(frozen=True, slots=True)
class ProviderTreeRequest:
    root: str
    max_files: int = MAX_PROVIDER_TREE_FILES
    max_bytes: int = MAX_PROVIDER_TREE_BYTES
    max_depth: int = MAX_PROVIDER_TREE_DEPTH

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _relative_path(self.root, allow_root=True))
        if not 1 <= self.max_files <= MAX_PROVIDER_TREE_FILES:
            raise ValueError("provider tree file limit is outside the supported range")
        if not 1 <= self.max_bytes <= MAX_PROVIDER_TREE_BYTES:
            raise ValueError("provider tree byte limit is outside the supported range")
        if not 0 <= self.max_depth <= MAX_PROVIDER_TREE_DEPTH:
            raise ValueError("provider tree depth limit is outside the supported range")


@dataclass(frozen=True, slots=True)
class ProviderTreeSnapshot:
    service_ref: ProviderServiceRef
    root: str
    manifest: ProviderTreeManifest
    content: ProviderTreeContentHandle = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _relative_path(self.root, allow_root=True))
        if not isinstance(self.content, ProviderTreeContentHandle) or self.content.manifest != self.manifest:
            raise ValueError("provider tree snapshot content does not match its manifest")


@dataclass(frozen=True, slots=True)
class ProviderTreeVerifyRequest:
    root: str
    expected: ProviderTreeManifest

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _relative_path(self.root, allow_root=True))


@dataclass(frozen=True, slots=True)
class ProviderTreeVerifyResult:
    service_ref: ProviderServiceRef
    root: str
    manifest: ProviderTreeManifest

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _relative_path(self.root, allow_root=True))


@dataclass(frozen=True, slots=True)
class ProviderTreeReplaceRequest:
    root: str
    files: tuple[ProviderTreeFile, ...] = field(repr=False)
    expected_current: ProviderTreeManifest

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _relative_path(self.root, allow_root=False))
        files = tuple(sorted(self.files, key=lambda item: item.path))
        if not files:
            raise ValueError("provider tree replacement cannot be empty")
        ProviderTreeManifest.from_files(files)
        object.__setattr__(self, "files", files)

    @property
    def proposed(self) -> ProviderTreeManifest:
        return ProviderTreeManifest.from_files(self.files)


@dataclass(frozen=True, slots=True)
class ProviderTreeReplaceResult:
    service_ref: ProviderServiceRef
    root: str
    manifest: ProviderTreeManifest
    operation_id: str
    verified: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _relative_path(self.root, allow_root=False))
        object.__setattr__(self, "operation_id", _identity(self.operation_id, "operation ID"))
        if self.verified is not True:
            raise ValueError("provider tree replacement result must be exactly verified")


@dataclass(frozen=True, slots=True)
class ProviderNativeBackupInventory:
    """Account-scoped native-backup facts."""

    service_ref: ProviderServiceRef
    inventory: NativeBackupInventory

    def __post_init__(self) -> None:
        if self.inventory.service_id != self.service_ref.service_id:
            raise ValueError("native-backup inventory service does not match its account-scoped reference")

    @property
    def backups(self) -> tuple[NativeBackup, ...]:
        return self.inventory.backups

    @property
    def truncated(self) -> bool:
        return self.inventory.truncated


@dataclass(frozen=True, slots=True)
class ProviderNativeBackupRestoreResult:
    """Account-scoped provider restore acknowledgement."""

    service_ref: ProviderServiceRef
    result: NativeBackupRestoreResult

    def __post_init__(self) -> None:
        if self.result.service_id != self.service_ref.service_id:
            raise ValueError("native-backup restore service does not match its account-scoped reference")

    @property
    def identity(self) -> NativeBackupIdentity:
        return self.result.identity

    @property
    def provider_accepted(self) -> bool:
        return self.result.provider_accepted

    @property
    def provider_returned_data(self) -> bool:
        return self.result.provider_returned_data

    @property
    def game_health_verified(self) -> bool:
        return self.result.game_health_verified


class _ProviderBackend(Protocol):
    @property
    def service_ref(self) -> ProviderServiceRef: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def async_validate_scopes(self, scopes: frozenset[ProviderScope]) -> None: ...

    async def async_read_file(self, request: ProviderFileReadRequest) -> ProviderFileContent: ...

    async def async_snapshot_tree(self, request: ProviderTreeRequest) -> ProviderTreeSnapshot: ...

    async def async_verify_tree(self, request: ProviderTreeVerifyRequest) -> ProviderTreeVerifyResult: ...

    async def async_replace_tree(
        self,
        request: ProviderTreeReplaceRequest,
        *,
        pre_mutation_check: Callable[[], None],
        mutation_started: Callable[[], None],
    ) -> ProviderTreeReplaceResult: ...

    async def async_list_native_backups(self, *, limit: int) -> NativeBackupInventory: ...

    async def async_restore_native_backup(
        self,
        request: NativeBackupRestoreRequest,
        *,
        pre_mutation_check: Callable[[], None],
        mutation_started: Callable[[], None],
    ) -> NativeBackupRestoreResult: ...


BackendResolver = Callable[[ProviderServiceRef], _ProviderBackend | Awaitable[_ProviderBackend]]
GrantAuthorizer = Callable[[ProviderGrantRequest], ProviderGrant | Awaitable[ProviderGrant]]
ApprovalAuthorizer = Callable[[ProviderMutationPlan, ProviderMutationApproval], bool | Awaitable[bool]]
type MutationRequest = NativeBackupRestoreRequest | ProviderTreeReplaceRequest
type MutationResult = NativeBackupRestoreResult | ProviderTreeReplaceResult


@dataclass(slots=True)
class _StoredPlan:
    plan: ProviderMutationPlan
    request: MutationRequest
    connector_id: str
    retained_bytes: int


@dataclass(slots=True)
class _StoredProgress:
    progress: ProviderMutationProgress
    connector_id: str


class ProviderConnectorRegistry:
    """Core-owned registry for revocable leases and provider plans."""

    def __init__(
        self,
        *,
        backend_resolver: BackendResolver,
        grant_authorizer: GrantAuthorizer,
        approval_authorizer: ApprovalAuthorizer,
        disabled_scopes: frozenset[ProviderScope] = frozenset(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._backend_resolver = backend_resolver
        self._grant_authorizer = grant_authorizer
        self._approval_authorizer = approval_authorizer
        self._disabled_scopes = frozenset(disabled_scopes)
        self._clock = clock
        self._leases: dict[str, ProviderConsumerLease] = {}
        self._connectors: dict[str, ProviderConnector] = {}
        self._plans: dict[str, _StoredPlan] = {}
        self._progress: dict[str, _StoredProgress] = {}
        self._published_approvals: dict[str, ProviderMutationApproval] = {}
        self._approval_waiters: dict[str, asyncio.Future[ProviderMutationApproval]] = {}
        self._pending_plan_bytes = 0
        self._pending_plan_bytes_by_connector: dict[str, int] = {}
        self._snapshot_reservations = 0
        self._snapshot_reservations_by_connector: dict[str, int] = {}
        self._snapshot_reserved_bytes = 0
        self._snapshot_reserved_bytes_by_connector: dict[str, int] = {}
        self._file_read_reserved_bytes = 0
        self._file_read_reserved_bytes_by_connector: dict[str, int] = {}
        self._inflight = 0
        self._inflight_by_service: dict[ProviderServiceRef, int] = {}
        self._inflight_by_connector: dict[str, int] = {}
        self._service_drained_events: dict[ProviderServiceRef, asyncio.Event] = {}
        self._connector_drained_events: dict[str, asyncio.Event] = {}
        self._service_generations: dict[ProviderServiceRef, int] = {}
        self._grant_generation = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._accepting = True
        self._closed = False

    def register_consumer(
        self, *, api_version: int, consumer_domain: str, consumer_entry_id: str
    ) -> ProviderConsumerLease:
        self._require_accepting()
        if len(self._leases) >= MAX_CONSUMER_LEASES:
            raise ProviderApiError("Provider consumer lease limit reached")
        if api_version != PROVIDER_CONNECTOR_API_VERSION:
            raise ProviderApiError(
                f"Unsupported provider connector API {api_version}; expected {PROVIDER_CONNECTOR_API_VERSION}"
            )
        lease = ProviderConsumerLease(
            registry=self,
            lease_id=secrets.token_urlsafe(24),
            consumer_domain=_identity(consumer_domain, "consumer domain"),
            consumer_entry_id=_identity(consumer_entry_id, "consumer entry ID"),
        )
        self._leases[lease.lease_id] = lease
        return lease

    def get_pending_plan(
        self,
        plan_id: str,
        payload_digest: str,
        *,
        require_enabled_scope: bool = True,
    ) -> ProviderMutationPlan:
        """Return one exact active plan for the trusted core approval route.

        This is an integration-owned inspection surface, not exported to
        companion integrations.  It prevents an HTTP request from supplying
        its own summary, service identity, or mutation facts for approval.
        """

        self._require_accepting()
        plan_id = _identity(plan_id, "plan ID")
        payload_digest = _digest(payload_digest)
        self._prune_plans()
        stored = self._plans.get(plan_id)
        if stored is None or stored.plan.payload_digest != payload_digest:
            raise ProviderPlanError("Mutation plan is unknown, expired, or does not match")
        connector = self._connectors.get(stored.connector_id)
        scope = (
            ProviderScope.NATIVE_BACKUP_RESTORE
            if stored.plan.action == "native_backup_restore"
            else ProviderScope.FILESYSTEM_REPLACE
        )
        if connector is None:
            raise ProviderPlanError("Mutation plan connector is no longer active")
        if require_enabled_scope:
            self._require_connector(connector, scope)
        else:
            self._require_connector_identity(connector, scope)
        return stored.plan

    def pending_plans(self) -> tuple[ProviderMutationPlan, ...]:
        """Return secret-free active plans for the core administrator surface."""

        self._require_accepting()
        self._prune_plans()
        return tuple(sorted((item.plan for item in self._plans.values()), key=lambda plan: plan.created_at))

    def recent_progress(self, *, limit: int = 20) -> tuple[ProviderMutationProgress, ...]:
        """Return bounded secret-free mutation progress for administrator review."""

        if not 1 <= limit <= 100:
            raise ValueError("provider mutation progress limit must be between 1 and 100")
        return tuple(
            sorted(
                (item.progress for item in self._progress.values()),
                key=lambda progress: progress.updated_at,
                reverse=True,
            )[:limit]
        )

    def publish_approval(self, approval: ProviderMutationApproval) -> None:
        """Deliver a core-issued approval to the exact companion plan waiter."""

        if not isinstance(approval, ProviderMutationApproval):
            raise TypeError("published approval must be a ProviderMutationApproval")
        plan = self.get_pending_plan(approval.plan_id, approval.payload_digest)
        if plan.plan_id in self._published_approvals:
            raise ProviderAuthorizationError("The exact mutation plan is already approved")
        self._published_approvals[plan.plan_id] = approval
        waiter = self._approval_waiters.pop(plan.plan_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(approval)

    def reject_pending_plan(self, plan_id: str, payload_digest: str) -> ProviderMutationPlan:
        """Reject one exact plan before any provider mutation can begin."""

        plan = self.get_pending_plan(plan_id, payload_digest, require_enabled_scope=False)
        stored = self._pop_plan(plan.plan_id)
        if stored is None:
            raise ProviderPlanError("Mutation plan is unknown, expired, or does not match")
        self._published_approvals.pop(plan.plan_id, None)
        waiter = self._approval_waiters.pop(plan.plan_id, None)
        error = ProviderPlanError("The administrator rejected the provider mutation plan")
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)
        self._set_progress(plan, stored.connector_id, ProviderMutationState.REFUSED, error_code="AdministratorRejected")
        return plan

    async def async_invalidate_service(self, service_ref: ProviderServiceRef) -> None:
        self._service_generations[service_ref] = self._service_generations.get(service_ref, 0) + 1
        for connector in tuple(self._connectors.values()):
            if connector.service_ref == service_ref:
                self._revoke_connector(connector)
        await _await_despite_cancellation(
            self._async_wait_service_drained(service_ref),
            name=f"nitrado-provider-invalidate-{service_ref.service_id}",
        )

    async def async_revoke_grant(self, grant_id: str) -> None:
        """Revoke every live capability derived from one persistent grant."""

        exact_grant_id = _identity(grant_id, "grant ID")
        # Invalidate every acquisition already awaiting the authorizer,
        # backend resolver, or capability probe. The persistent authority is
        # deleted before this method is called, so a later acquisition cannot
        # obtain the revoked grant again.
        self._grant_generation += 1
        affected: set[str] = set()
        for connector in tuple(self._connectors.values()):
            if connector._grant.grant_id == exact_grant_id:
                affected.add(connector.connector_id)
                self._revoke_connector(connector)
        for connector_id in affected:
            await _await_despite_cancellation(
                self._async_wait_connector_drained(connector_id),
                name=f"nitrado-provider-revoke-{connector_id}",
            )

    async def async_quiesce(self) -> None:
        """Reversibly stop new authority, revoke it, and drain started work."""

        self._accepting = False
        self._revoke_all()
        await _await_despite_cancellation(self._drained.wait(), name="nitrado-provider-quiesce")

    def async_resume(self) -> None:
        """Resume registration after a reversible failed unload."""

        if self._closed:
            raise ProviderLeaseRevokedError("Provider connector registry is closed")
        self._accepting = True

    async def async_close(self) -> None:
        """Irreversibly revoke authority and drain every started operation."""

        self._closed = True
        await self.async_quiesce()
        self._leases.clear()
        self._connectors.clear()
        self._plans.clear()
        self._pending_plan_bytes = 0
        self._pending_plan_bytes_by_connector.clear()
        self._progress.clear()
        for waiter in tuple(self._approval_waiters.values()):
            if not waiter.done():
                waiter.set_exception(ProviderLeaseRevokedError("Provider connector registry is closed"))
        self._approval_waiters.clear()
        self._published_approvals.clear()

    async def _async_acquire_connector(
        self,
        lease: ProviderConsumerLease,
        service_ref: ProviderServiceRef,
        requested_scopes: frozenset[ProviderScope],
        requested_roots: frozenset[str],
    ) -> ProviderConnector:
        self._require_lease(lease)
        lease_generation = lease._generation
        service_generation = self._service_generations.get(service_ref, 0)
        grant_generation = self._grant_generation
        requested_scopes = _scopes(requested_scopes)
        requested_roots = _roots(requested_roots)
        has_filesystem_scope = bool(requested_scopes & _FILESYSTEM_SCOPES)
        if has_filesystem_scope != bool(requested_roots):
            raise ProviderAuthorizationError(
                "Filesystem scopes require exact roots and backup-only grants cannot request roots"
            )
        request = ProviderGrantRequest(
            lease.consumer_domain,
            lease.consumer_entry_id,
            service_ref,
            requested_scopes,
            requested_roots,
        )
        grant = await _resolve(self._grant_authorizer(request))
        self._require_lease_generation(lease, lease_generation)
        self._require_service_generation(service_ref, service_generation)
        self._require_grant_generation(grant_generation)
        self._validate_grant(request, grant)
        backend = await _resolve(self._backend_resolver(service_ref))
        self._require_lease_generation(lease, lease_generation)
        self._require_service_generation(service_ref, service_generation)
        self._require_grant_generation(grant_generation)
        self._validate_grant(request, grant)
        if getattr(backend, "service_ref", None) != service_ref:
            raise ProviderApiError("Provider backend is bound to a different account-scoped service")
        if not isinstance(backend.capabilities, ProviderCapabilities):
            raise ProviderApiError("Provider backend returned invalid capabilities")
        if not requested_scopes <= backend.capabilities.scopes:
            raise ProviderApiError("Provider service does not support every requested scope")
        await backend.async_validate_scopes(requested_scopes)
        self._require_lease_generation(lease, lease_generation)
        self._require_service_generation(service_ref, service_generation)
        self._require_grant_generation(grant_generation)
        self._validate_grant(request, grant)
        if len(self._connectors) >= MAX_CONNECTORS_TOTAL:
            raise ProviderApiError("Provider connector limit reached")
        if len(lease._connector_ids) >= MAX_CONNECTORS_PER_LEASE:
            raise ProviderApiError("Provider connector limit for this companion reached")
        service_connectors = sum(connector.service_ref == service_ref for connector in self._connectors.values())
        if service_connectors >= MAX_CONNECTORS_PER_SERVICE:
            raise ProviderApiError("Provider connector limit for this service reached")
        connector = ProviderConnector(
            registry=self,
            connector_id=secrets.token_urlsafe(24),
            lease=lease,
            service_ref=service_ref,
            scopes=requested_scopes,
            roots=requested_roots,
            grant=grant,
            backend=backend,
        )
        self._connectors[connector.connector_id] = connector
        lease._connector_ids.add(connector.connector_id)
        return connector

    def _new_plan(
        self,
        connector: ProviderConnector,
        request: MutationRequest,
        *,
        scope: ProviderScope,
        action: str,
        payload: dict[str, Any],
        summary: str,
        ttl_seconds: int,
    ) -> ProviderMutationPlan:
        self._require_connector(connector, scope)
        if not 1 <= ttl_seconds <= MAX_PLAN_TTL_SECONDS:
            raise ValueError(f"plan TTL must be between 1 and {MAX_PLAN_TTL_SECONDS} seconds")
        self._prune_plans()
        connector_plans = sum(stored.connector_id == connector.connector_id for stored in self._plans.values())
        if connector_plans >= MAX_PLANS_PER_CONNECTOR or len(self._plans) >= MAX_PLANS_TOTAL:
            raise ProviderPlanError("Outstanding provider mutation plan limit reached")
        retained_bytes = request.proposed.total_bytes if isinstance(request, ProviderTreeReplaceRequest) else 0
        connector_retained = self._pending_plan_bytes_by_connector.get(connector.connector_id, 0)
        if (
            connector_retained + retained_bytes > MAX_PENDING_PLAN_BYTES_PER_CONNECTOR
            or self._pending_plan_bytes + retained_bytes > MAX_PENDING_PLAN_BYTES_TOTAL
        ):
            raise ProviderPlanError("Outstanding provider mutation plan byte limit reached")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        created_at = self._clock()
        plan = ProviderMutationPlan(
            plan_id=secrets.token_urlsafe(24),
            audit_id=secrets.token_hex(16),
            action=action,
            service_ref=connector.service_ref,
            consumer_domain=connector._lease.consumer_domain,
            consumer_entry_id=connector._lease.consumer_entry_id,
            grant_id=connector._grant.grant_id,
            payload_digest=digest,
            summary=summary,
            created_at=created_at,
            expires_at=created_at + ttl_seconds,
            _execution_token=secrets.token_urlsafe(32),
        )
        self._plans[plan.plan_id] = _StoredPlan(plan, request, connector.connector_id, retained_bytes)
        self._pending_plan_bytes += retained_bytes
        self._pending_plan_bytes_by_connector[connector.connector_id] = connector_retained + retained_bytes
        self._set_progress(plan, connector.connector_id, ProviderMutationState.PLANNED)
        return plan

    async def _async_execute_plan(
        self,
        connector: ProviderConnector,
        plan: ProviderMutationPlan,
        approval: ProviderMutationApproval,
        *,
        scope: ProviderScope,
    ) -> MutationResult:
        generation = self._begin_operation(connector, scope)
        mutation_started = False
        progress_plan: ProviderMutationPlan | None = None
        retained_plan: _StoredPlan | None = None
        try:
            stored = self._plans.get(plan.plan_id)
            if (
                stored is None
                or stored.connector_id != connector.connector_id
                or stored.plan != plan
                or stored.plan._execution_token != plan._execution_token
            ):
                raise ProviderPlanError("Mutation plan is unknown, replaced, or already consumed")
            progress_plan = stored.plan
            if self._clock() >= plan.expires_at:
                self._pop_plan(plan.plan_id)
                raise ProviderPlanError("Mutation plan has expired")
            if approval.plan_id != plan.plan_id or approval.payload_digest != plan.payload_digest:
                raise ProviderAuthorizationError("Mutation approval does not match the exact plan")
            self._set_progress(progress_plan, connector.connector_id, ProviderMutationState.AUTHORIZING)

            # Reserve before awaiting the core hook. The opaque token is
            # consumed by that hook; every execution attempt is one-shot.
            retained_plan = self._pop_plan(plan.plan_id, release_bytes=False)
            self._published_approvals.pop(plan.plan_id, None)
            waiter = self._approval_waiters.pop(plan.plan_id, None)
            if waiter is not None and not waiter.done():
                waiter.cancel()
            authorized = await _resolve(self._approval_authorizer(plan, approval))
            self._require_connector_generation(connector, generation, scope)
            if self._clock() >= plan.expires_at:
                raise ProviderPlanError("Mutation plan expired while core approval was pending")
            if authorized is not True:
                raise ProviderAuthorizationError("Core did not authorize the exact mutation plan")

            def require_current_authority() -> None:
                self._require_connector_generation(connector, generation, scope)

            def record_mutation_started() -> None:
                nonlocal mutation_started
                require_current_authority()
                mutation_started = True
                self._set_progress(progress_plan, connector.connector_id, ProviderMutationState.RUNNING)

            try:
                if isinstance(stored.request, NativeBackupRestoreRequest):
                    result = await _await_despite_cancellation(
                        connector._backend.async_restore_native_backup(
                            stored.request,
                            pre_mutation_check=require_current_authority,
                            mutation_started=record_mutation_started,
                        ),
                        name=f"nitrado-provider-{plan.audit_id}",
                    )
                else:
                    result = await _await_despite_cancellation(
                        connector._backend.async_replace_tree(
                            stored.request,
                            pre_mutation_check=require_current_authority,
                            mutation_started=record_mutation_started,
                        ),
                        name=f"nitrado-provider-{plan.audit_id}",
                    )
                self._validate_mutation_result(connector, stored.request, result)
            except Exception as err:
                if not mutation_started:
                    raise
                raise ProviderMutationExecutionError(audit_id=plan.audit_id, action=plan.action) from err
            self._set_progress(progress_plan, connector.connector_id, ProviderMutationState.COMPLETED)
            return result
        except BaseException as err:
            if progress_plan is not None:
                self._set_progress(
                    progress_plan,
                    connector.connector_id,
                    ProviderMutationState.FAILED if mutation_started else ProviderMutationState.REFUSED,
                    error_code=type(err).__name__,
                )
            raise
        finally:
            # Once mutation_started is true, revocation cannot interrupt the
            # provider work; quiesce/final-close waits here for exact drain.
            _ = mutation_started
            if retained_plan is not None:
                self._release_plan_bytes(retained_plan)
            self._end_operation(connector)

    def _mutation_progress(
        self,
        connector: ProviderConnector,
        plan: ProviderMutationPlan,
    ) -> ProviderMutationProgress:
        self._require_connector(connector, _scope_for_action(plan.action))
        stored = self._progress.get(plan.plan_id)
        if (
            stored is None
            or stored.connector_id != connector.connector_id
            or stored.progress.audit_id != plan.audit_id
            or stored.progress.action != plan.action
        ):
            raise ProviderPlanError("Mutation progress is unknown or belongs to another connector")
        return stored.progress

    def _set_progress(
        self,
        plan: ProviderMutationPlan,
        connector_id: str,
        state: ProviderMutationState,
        *,
        error_code: str | None = None,
    ) -> None:
        self._progress[plan.plan_id] = _StoredProgress(
            ProviderMutationProgress(
                plan.plan_id,
                plan.audit_id,
                plan.action,
                state,
                self._clock(),
                error_code,
            ),
            connector_id,
        )
        if len(self._progress) > MAX_PROGRESS_RECORDS:
            oldest = min(self._progress, key=lambda item: self._progress[item].progress.updated_at)
            self._progress.pop(oldest, None)

    async def _async_read[T](
        self,
        connector: ProviderConnector,
        scope: ProviderScope,
        operation: Callable[[], Awaitable[T]],
        *,
        cleanup: Callable[[T], None] | None = None,
    ) -> T:
        generation = self._begin_operation(connector, scope)
        result: T | None = None
        try:
            result = await operation()
            self._require_connector_generation(connector, generation, scope)
            return result
        except BaseException:
            if result is not None and cleanup is not None:
                cleanup(result)
            raise
        finally:
            self._end_operation(connector)

    def _reserve_file_read(self, connector: ProviderConnector, requested_bytes: int) -> None:
        connector_bytes = self._file_read_reserved_bytes_by_connector.get(connector.connector_id, 0)
        if (
            connector_bytes + requested_bytes > MAX_INFLIGHT_FILE_READ_BYTES_PER_CONNECTOR
            or self._file_read_reserved_bytes + requested_bytes > MAX_INFLIGHT_FILE_READ_BYTES_TOTAL
        ):
            raise ProviderApiError("Provider file-read byte reservation limit reached")
        self._file_read_reserved_bytes += requested_bytes
        self._file_read_reserved_bytes_by_connector[connector.connector_id] = connector_bytes + requested_bytes

    def _release_file_read(self, connector: ProviderConnector, requested_bytes: int) -> None:
        self._file_read_reserved_bytes -= requested_bytes
        connector_bytes = self._file_read_reserved_bytes_by_connector[connector.connector_id] - requested_bytes
        if connector_bytes:
            self._file_read_reserved_bytes_by_connector[connector.connector_id] = connector_bytes
        else:
            self._file_read_reserved_bytes_by_connector.pop(connector.connector_id, None)

    def _reserve_snapshot(self, connector: ProviderConnector, requested_bytes: int) -> None:
        open_handles = tuple(handle for item in self._connectors.values() for handle in item._content_handles)
        connector_open_bytes = sum(handle.manifest.total_bytes for handle in connector._content_handles)
        connector_reserved = self._snapshot_reserved_bytes_by_connector.get(connector.connector_id, 0)
        connector_reservations = self._snapshot_reservations_by_connector.get(connector.connector_id, 0)
        if len(open_handles) + self._snapshot_reservations >= MAX_CONTENT_HANDLES_TOTAL:
            raise ProviderApiError("Provider snapshot content-handle reservation limit reached")
        if len(connector._content_handles) + connector_reservations >= MAX_CONTENT_HANDLES_PER_CONNECTOR:
            raise ProviderApiError("Provider snapshot handle limit for this connector reached")
        if (
            sum(handle.manifest.total_bytes for handle in open_handles)
            + self._snapshot_reserved_bytes
            + requested_bytes
            > MAX_OPEN_SNAPSHOT_BYTES_TOTAL
        ):
            raise ProviderApiError("Provider snapshot byte reservation limit reached")
        if connector_open_bytes + connector_reserved + requested_bytes > MAX_OPEN_SNAPSHOT_BYTES_PER_CONNECTOR:
            raise ProviderApiError("Provider snapshot byte reservation limit for this connector reached")
        self._snapshot_reservations += 1
        self._snapshot_reservations_by_connector[connector.connector_id] = connector_reservations + 1
        self._snapshot_reserved_bytes += requested_bytes
        self._snapshot_reserved_bytes_by_connector[connector.connector_id] = connector_reserved + requested_bytes

    def _release_snapshot_reservation(self, connector: ProviderConnector, requested_bytes: int) -> None:
        self._snapshot_reservations -= 1
        connector_reservations = self._snapshot_reservations_by_connector[connector.connector_id] - 1
        if connector_reservations:
            self._snapshot_reservations_by_connector[connector.connector_id] = connector_reservations
        else:
            self._snapshot_reservations_by_connector.pop(connector.connector_id, None)
        self._snapshot_reserved_bytes -= requested_bytes
        connector_bytes = self._snapshot_reserved_bytes_by_connector[connector.connector_id] - requested_bytes
        if connector_bytes:
            self._snapshot_reserved_bytes_by_connector[connector.connector_id] = connector_bytes
        else:
            self._snapshot_reserved_bytes_by_connector.pop(connector.connector_id, None)

    def _begin_operation(self, connector: ProviderConnector, scope: ProviderScope) -> int:
        self._require_connector(connector, scope)
        if self._inflight >= MAX_INFLIGHT_OPERATIONS_TOTAL:
            raise ProviderApiError("Provider operation limit reached")
        if self._inflight_by_service.get(connector.service_ref, 0) >= MAX_INFLIGHT_OPERATIONS_PER_SERVICE:
            raise ProviderApiError("Provider operation limit for this service reached")
        self._inflight += 1
        self._inflight_by_service[connector.service_ref] = self._inflight_by_service.get(connector.service_ref, 0) + 1
        self._inflight_by_connector[connector.connector_id] = (
            self._inflight_by_connector.get(connector.connector_id, 0) + 1
        )
        event = self._service_drained_events.setdefault(connector.service_ref, asyncio.Event())
        event.clear()
        connector_event = self._connector_drained_events.setdefault(connector.connector_id, asyncio.Event())
        connector_event.clear()
        self._drained.clear()
        return connector._generation

    def _end_operation(self, connector: ProviderConnector) -> None:
        self._inflight -= 1
        service_ref = connector.service_ref
        count = self._inflight_by_service[service_ref] - 1
        if count:
            self._inflight_by_service[service_ref] = count
        else:
            self._inflight_by_service.pop(service_ref, None)
            self._service_drained_events.setdefault(service_ref, asyncio.Event()).set()
        connector_count = self._inflight_by_connector[connector.connector_id] - 1
        if connector_count:
            self._inflight_by_connector[connector.connector_id] = connector_count
        else:
            self._inflight_by_connector.pop(connector.connector_id, None)
            self._connector_drained_events.setdefault(connector.connector_id, asyncio.Event()).set()
        if self._inflight == 0:
            self._drained.set()

    async def _async_wait_service_drained(self, service_ref: ProviderServiceRef) -> None:
        if not self._inflight_by_service.get(service_ref, 0):
            return
        event = self._service_drained_events.setdefault(service_ref, asyncio.Event())
        await event.wait()

    async def _async_wait_connector_drained(self, connector_id: str) -> None:
        if not self._inflight_by_connector.get(connector_id, 0):
            self._connector_drained_events.pop(connector_id, None)
            return
        event = self._connector_drained_events.setdefault(connector_id, asyncio.Event())
        await event.wait()
        if (
            not self._inflight_by_connector.get(connector_id, 0)
            and self._connector_drained_events.get(connector_id) is event
        ):
            self._connector_drained_events.pop(connector_id, None)

    def _validate_mutation_result(
        self, connector: ProviderConnector, request: MutationRequest, result: MutationResult
    ) -> None:
        if isinstance(request, NativeBackupRestoreRequest):
            if not isinstance(result, NativeBackupRestoreResult):
                raise ProviderApiError("Provider backend returned an invalid restore result")
            if result.service_id != connector.service_ref.service_id or result.identity != request.identity:
                raise ProviderApiError("Provider backend returned a restore result for a different target")
            return
        if not isinstance(result, ProviderTreeReplaceResult):
            raise ProviderApiError("Provider backend returned an invalid tree replacement result")
        if (
            result.service_ref != connector.service_ref
            or result.root != request.root
            or result.manifest != request.proposed
        ):
            raise ProviderApiError("Provider backend returned a tree result for a different target")

    def _validate_grant(self, request: ProviderGrantRequest, grant: ProviderGrant) -> None:
        if not isinstance(grant, ProviderGrant):
            raise ProviderAuthorizationError("Provider grant hook returned an invalid grant")
        if (
            grant.consumer_domain != request.consumer_domain
            or grant.consumer_entry_id != request.consumer_entry_id
            or grant.service_ref != request.service_ref
            or not request.requested_scopes <= grant.scopes
            or not request.requested_roots <= grant.permitted_roots
        ):
            raise ProviderAuthorizationError("Provider grant does not match the exact consumer request")
        if grant.expires_at is not None and self._clock() >= grant.expires_at:
            raise ProviderAuthorizationError("Provider grant has expired")

    def _require_grant_generation(self, generation: int) -> None:
        if self._grant_generation != generation:
            raise ProviderLeaseRevokedError("Provider grants changed during connector acquisition")

    def _require_accepting(self) -> None:
        if self._closed or not self._accepting:
            raise ProviderLeaseRevokedError("Provider connector registry is unavailable")

    def _require_lease(self, lease: ProviderConsumerLease) -> None:
        self._require_accepting()
        if not lease._active or self._leases.get(lease.lease_id) is not lease:
            raise ProviderLeaseRevokedError("Provider consumer lease is revoked")

    def _require_lease_generation(self, lease: ProviderConsumerLease, generation: int) -> None:
        self._require_lease(lease)
        if lease._generation != generation:
            raise ProviderLeaseRevokedError("Provider consumer lease changed during acquisition")

    def _require_service_generation(self, service_ref: ProviderServiceRef, generation: int) -> None:
        self._require_accepting()
        if self._service_generations.get(service_ref, 0) != generation:
            raise ProviderLeaseRevokedError("Provider service changed during connector acquisition")

    def _require_connector(self, connector: ProviderConnector, scope: ProviderScope) -> None:
        self._require_connector_identity(connector, scope)
        if scope in self._disabled_scopes:
            raise ProviderAuthorizationError(f"Provider capability is disabled in this release: {scope.value}")

    def _require_connector_identity(self, connector: ProviderConnector, scope: ProviderScope) -> None:
        """Validate connector identity and grant without applying release gates."""

        self._require_lease(connector._lease)
        if not connector._active or self._connectors.get(connector.connector_id) is not connector:
            raise ProviderLeaseRevokedError("Provider service connector is revoked")
        if connector._grant.expires_at is not None and self._clock() >= connector._grant.expires_at:
            self._revoke_connector(connector)
            raise ProviderLeaseRevokedError("Provider service grant has expired")
        if scope not in connector.scopes:
            raise ProviderAuthorizationError(f"Provider scope is not granted: {scope.value}")

    def _require_connector_generation(
        self, connector: ProviderConnector, generation: int, scope: ProviderScope
    ) -> None:
        self._require_connector(connector, scope)
        if connector._generation != generation:
            raise ProviderLeaseRevokedError("Provider connector changed during operation")

    def _revoke_connector(self, connector: ProviderConnector) -> None:
        for content in tuple(connector._content_handles):
            content._revoke()
        connector._active = False
        connector._generation += 1
        self._connectors.pop(connector.connector_id, None)
        connector._lease._connector_ids.discard(connector.connector_id)
        for plan_id, stored in tuple(self._plans.items()):
            if stored.connector_id == connector.connector_id:
                self._discard_plan(plan_id, ProviderLeaseRevokedError("Provider connector is revoked"))

    def _revoke_lease(self, lease: ProviderConsumerLease) -> None:
        if self._leases.get(lease.lease_id) is not lease:
            lease._active = False
            lease._generation += 1
            return
        for connector_id in tuple(lease._connector_ids):
            connector = self._connectors.get(connector_id)
            if connector is not None:
                self._revoke_connector(connector)
        lease._active = False
        lease._generation += 1
        self._leases.pop(lease.lease_id, None)

    async def _async_revoke_lease_and_drain(self, lease: ProviderConsumerLease) -> None:
        connector_ids = {
            connector.connector_id
            for connector_id in lease._connector_ids
            if (connector := self._connectors.get(connector_id)) is not None
        }
        self._revoke_lease(lease)
        if connector_ids:
            await _await_despite_cancellation(
                asyncio.gather(*(self._async_wait_connector_drained(connector_id) for connector_id in connector_ids)),
                name=f"nitrado-provider-lease-close-{lease.lease_id}",
            )

    def _revoke_all(self) -> None:
        for lease in tuple(self._leases.values()):
            self._revoke_lease(lease)

    def _prune_plans(self) -> None:
        now = self._clock()
        for plan_id, stored in tuple(self._plans.items()):
            connector = self._connectors.get(stored.connector_id)
            if connector is None or now >= stored.plan.expires_at:
                self._discard_plan(plan_id, ProviderPlanError("Mutation plan expired or lost its connector"))

    def _discard_plan(self, plan_id: str, error: Exception) -> None:
        stored = self._pop_plan(plan_id)
        self._published_approvals.pop(plan_id, None)
        waiter = self._approval_waiters.pop(plan_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)
        if stored is not None:
            self._set_progress(
                stored.plan,
                stored.connector_id,
                ProviderMutationState.REFUSED,
                error_code=type(error).__name__,
            )

    def _pop_plan(self, plan_id: str, *, release_bytes: bool = True) -> _StoredPlan | None:
        """Remove a retained plan and release its admission-time byte reservation."""

        stored = self._plans.pop(plan_id, None)
        if stored is None:
            return None
        if release_bytes:
            self._release_plan_bytes(stored)
        return stored

    def _release_plan_bytes(self, stored: _StoredPlan) -> None:
        """Release one pending or executing plan's retained-content reservation."""

        self._pending_plan_bytes -= stored.retained_bytes
        connector_bytes = self._pending_plan_bytes_by_connector.get(stored.connector_id, 0) - stored.retained_bytes
        if connector_bytes:
            self._pending_plan_bytes_by_connector[stored.connector_id] = connector_bytes
        else:
            self._pending_plan_bytes_by_connector.pop(stored.connector_id, None)

    async def _async_wait_for_approval(
        self,
        connector: ProviderConnector,
        plan: ProviderMutationPlan,
    ) -> ProviderMutationApproval:
        """Wait for a core administrator decision without exposing transport authority."""

        stored_plan = self.get_pending_plan(plan.plan_id, plan.payload_digest)
        stored = self._plans.get(plan.plan_id)
        if stored is None or stored.connector_id != connector.connector_id or stored_plan != plan:
            raise ProviderPlanError("Mutation plan does not belong to this connector")
        published = self._published_approvals.pop(plan.plan_id, None)
        if published is not None:
            return published
        existing = self._approval_waiters.get(plan.plan_id)
        if existing is not None:
            raise ProviderPlanError("Mutation plan already has an approval waiter")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ProviderMutationApproval] = loop.create_future()
        self._approval_waiters[plan.plan_id] = waiter
        try:
            remaining = max(0.0, plan.expires_at - self._clock())
            if remaining <= 0:
                raise ProviderPlanError("Mutation plan has expired")
            async with asyncio.timeout(remaining):
                return await asyncio.shield(waiter)
        except TimeoutError as err:
            self._discard_plan(plan.plan_id, ProviderPlanError("Mutation plan expired awaiting approval"))
            raise ProviderPlanError("Mutation plan expired awaiting approval") from err
        finally:
            if self._approval_waiters.get(plan.plan_id) is waiter:
                self._approval_waiters.pop(plan.plan_id, None)


class ProviderConsumerLease:
    def __init__(
        self,
        *,
        registry: ProviderConnectorRegistry,
        lease_id: str,
        consumer_domain: str,
        consumer_entry_id: str,
    ) -> None:
        self._registry = registry
        self.lease_id = lease_id
        self.consumer_domain = consumer_domain
        self.consumer_entry_id = consumer_entry_id
        self._connector_ids: set[str] = set()
        self._active = True
        self._generation = 0

    @property
    def active(self) -> bool:
        return self._active

    async def async_get_connector(
        self,
        service_ref: ProviderServiceRef,
        *,
        scopes: Iterable[ProviderScope],
        roots: Iterable[str] = (),
    ) -> ProviderConnector:
        return await self._registry._async_acquire_connector(
            self,
            service_ref,
            _scopes(scopes),
            _roots(roots),
        )

    async def async_close(self) -> None:
        await self._registry._async_revoke_lease_and_drain(self)


class ProviderConnector:
    """Service-bound, scope-limited provider operations."""

    def __init__(
        self,
        *,
        registry: ProviderConnectorRegistry,
        connector_id: str,
        lease: ProviderConsumerLease,
        service_ref: ProviderServiceRef,
        scopes: frozenset[ProviderScope],
        roots: frozenset[str],
        grant: ProviderGrant,
        backend: _ProviderBackend,
    ) -> None:
        self._registry = registry
        self.connector_id = connector_id
        self._lease = lease
        self.service_ref = service_ref
        self.scopes = scopes
        self.roots = roots
        self._grant = grant
        self._backend = backend
        self._active = True
        self._generation = 0
        self._content_handles: set[ProviderTreeContentHandle] = set()

    @property
    def active(self) -> bool:
        return self._active and self._lease.active

    async def async_read_file(self, request: ProviderFileReadRequest) -> ProviderFileContent:
        if not isinstance(request, ProviderFileReadRequest):
            raise TypeError("file read request must be ProviderFileReadRequest")
        self._require_permitted_path(request.path)
        self._registry._reserve_file_read(self, request.max_bytes)
        try:
            result = await self._registry._async_read(
                self,
                ProviderScope.FILESYSTEM_READ,
                lambda: self._backend.async_read_file(request),
            )
        finally:
            self._registry._release_file_read(self, request.max_bytes)
        if not isinstance(result, ProviderFileContent) or result.service_ref != self.service_ref:
            raise ProviderApiError("Provider backend returned file content for a different service")
        if result.path != request.path or len(result.content) > request.max_bytes:
            raise ProviderApiError("Provider backend returned file content outside the exact request")
        return result

    async def async_snapshot_tree(self, request: ProviderTreeRequest) -> ProviderTreeSnapshot:
        if not isinstance(request, ProviderTreeRequest):
            raise TypeError("tree snapshot request must be ProviderTreeRequest")
        self._require_permitted_path(request.root)
        self._registry._reserve_snapshot(self, request.max_bytes)
        result: ProviderTreeSnapshot | None = None
        committed = False
        try:
            result = await self._registry._async_read(
                self,
                ProviderScope.FILESYSTEM_SNAPSHOT,
                lambda: self._backend.async_snapshot_tree(request),
                cleanup=lambda item: item.content._revoke() if isinstance(item, ProviderTreeSnapshot) else None,
            )
            if not isinstance(result, ProviderTreeSnapshot):
                raise ProviderApiError("Provider backend returned a snapshot for a different service")
            if result.service_ref != self.service_ref:
                raise ProviderApiError("Provider backend returned a snapshot for a different service")
            if result.root != request.root:
                raise ProviderApiError("Provider backend returned a snapshot for a different root")
            if len(result.manifest.entries) > request.max_files or result.manifest.total_bytes > request.max_bytes:
                raise ProviderApiError("Provider backend exceeded the requested snapshot limits")
            if any(item.path.count("/") > request.max_depth for item in result.manifest.entries):
                raise ProviderApiError("Provider backend exceeded the requested snapshot depth")
            result.content._bind_owner(self._content_handles.discard)
            self._content_handles.add(result.content)
            committed = True
            return result
        finally:
            try:
                if isinstance(result, ProviderTreeSnapshot) and not committed:
                    result.content._revoke()
            finally:
                self._registry._release_snapshot_reservation(self, request.max_bytes)

    async def async_verify_tree(self, request: ProviderTreeVerifyRequest) -> ProviderTreeVerifyResult:
        if not isinstance(request, ProviderTreeVerifyRequest):
            raise TypeError("tree verify request must be ProviderTreeVerifyRequest")
        self._require_permitted_path(request.root)
        result = await self._registry._async_read(
            self,
            ProviderScope.FILESYSTEM_SNAPSHOT,
            lambda: self._backend.async_verify_tree(request),
        )
        if (
            not isinstance(result, ProviderTreeVerifyResult)
            or result.service_ref != self.service_ref
            or result.root != request.root
            or result.manifest != request.expected
        ):
            raise ProviderApiError("Provider backend did not verify the exact requested tree")
        return result

    def plan_tree_replace(
        self,
        request: ProviderTreeReplaceRequest,
        *,
        ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
    ) -> ProviderMutationPlan:
        if not isinstance(request, ProviderTreeReplaceRequest):
            raise TypeError("tree replace request must be ProviderTreeReplaceRequest")
        self._require_permitted_path(request.root)
        payload = {
            "action": "filesystem_tree_replace",
            "account_entry_id": self.service_ref.account_entry_id,
            "service_id": self.service_ref.service_id,
            "root": request.root,
            "expected_current": request.expected_current.digest,
            "proposed": request.proposed.digest,
        }
        return self._registry._new_plan(
            self,
            request,
            scope=ProviderScope.FILESYSTEM_REPLACE,
            action="filesystem_tree_replace",
            payload=payload,
            summary=f"Replace provider tree {request.root} for service {self.service_ref.service_id}",
            ttl_seconds=ttl_seconds,
        )

    async def async_execute_tree_replace(
        self, plan: ProviderMutationPlan, approval: ProviderMutationApproval
    ) -> ProviderTreeReplaceResult:
        result = await self._registry._async_execute_plan(self, plan, approval, scope=ProviderScope.FILESYSTEM_REPLACE)
        if not isinstance(result, ProviderTreeReplaceResult):
            raise ProviderApiError("Provider backend returned the wrong mutation result type")
        return result

    async def async_list_native_backups(self, *, limit: int = MAX_NATIVE_BACKUPS) -> ProviderNativeBackupInventory:
        if not 1 <= limit <= MAX_NATIVE_BACKUPS:
            raise ValueError(f"backup inventory limit must be between 1 and {MAX_NATIVE_BACKUPS}")
        inventory = await self._registry._async_read(
            self,
            ProviderScope.NATIVE_BACKUP_READ,
            lambda: self._backend.async_list_native_backups(limit=limit),
        )
        if not isinstance(inventory, NativeBackupInventory):
            raise ProviderApiError("Provider backend returned an invalid backup inventory")
        if inventory.service_id != self.service_ref.service_id:
            raise ProviderApiError("Provider backend returned inventory for a different service")
        if len(inventory.backups) > limit:
            raise ProviderApiError("Provider backend exceeded the requested backup inventory limit")
        return ProviderNativeBackupInventory(self.service_ref, inventory)

    def plan_native_backup_restore(
        self,
        request: NativeBackupRestoreRequest,
        *,
        ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS,
    ) -> ProviderMutationPlan:
        if not isinstance(request, NativeBackupRestoreRequest):
            raise TypeError("restore request must be NativeBackupRestoreRequest")
        payload = {
            "action": "native_backup_restore",
            "account_entry_id": self.service_ref.account_entry_id,
            "service_id": self.service_ref.service_id,
            "folder": request.identity.folder,
            "backup_id": request.identity.backup_id,
            "expected_size_bytes": request.expected_size_bytes,
            "expected_file_size_bytes": request.expected_file_size_bytes,
            "expected_created_at": request.expected_created_at,
            "expected_status": request.expected_status,
            "expected_backup_type": request.expected_backup_type,
        }
        return self._registry._new_plan(
            self,
            request,
            scope=ProviderScope.NATIVE_BACKUP_RESTORE,
            action="native_backup_restore",
            payload=payload,
            summary=(
                f"Restore native backup {request.identity.folder}/{request.identity.backup_id} "
                f"for service {self.service_ref.service_id}"
            ),
            ttl_seconds=ttl_seconds,
        )

    async def async_execute_native_backup_restore(
        self, plan: ProviderMutationPlan, approval: ProviderMutationApproval
    ) -> ProviderNativeBackupRestoreResult:
        if not isinstance(plan, ProviderMutationPlan) or not isinstance(approval, ProviderMutationApproval):
            raise TypeError("restore execution requires a typed plan and approval")
        result = await self._registry._async_execute_plan(
            self, plan, approval, scope=ProviderScope.NATIVE_BACKUP_RESTORE
        )
        if not isinstance(result, NativeBackupRestoreResult):
            raise ProviderApiError("Provider backend returned the wrong mutation result type")
        return ProviderNativeBackupRestoreResult(self.service_ref, result)

    def mutation_progress(self, plan: ProviderMutationPlan) -> ProviderMutationProgress:
        """Return current secret-free progress for this connector's exact plan."""

        if not isinstance(plan, ProviderMutationPlan):
            raise TypeError("mutation progress requires a typed plan")
        return self._registry._mutation_progress(self, plan)

    async def async_wait_for_approval(self, plan: ProviderMutationPlan) -> ProviderMutationApproval:
        """Wait for the core administrator surface to approve this exact plan."""

        if not isinstance(plan, ProviderMutationPlan):
            raise TypeError("approval wait requires a typed mutation plan")
        return await self._registry._async_wait_for_approval(self, plan)

    def _require_permitted_path(self, path: str) -> None:
        if not any(_path_within(path, root) for root in self.roots):
            raise ProviderAuthorizationError("Provider filesystem path is outside the granted roots")


async def async_register_provider_consumer(
    hass: Any,
    *,
    api_version: int,
    consumer_domain: str,
    consumer_entry_id: str,
) -> ProviderConsumerLease:
    data = getattr(hass, "data", None)
    registry = data.get(_REGISTRY_DATA_KEY) if isinstance(data, dict) else None
    if not isinstance(registry, ProviderConnectorRegistry):
        raise ProviderApiError("Nitrado provider connector registry is unavailable")
    _require_matching_consumer_entry(hass, consumer_domain, consumer_entry_id)
    return registry.register_consumer(
        api_version=api_version,
        consumer_domain=consumer_domain,
        consumer_entry_id=consumer_entry_id,
    )


def install_provider_connector_registry(hass: Any, registry: ProviderConnectorRegistry) -> None:
    data = getattr(hass, "data", None)
    if not isinstance(data, dict) or not isinstance(registry, ProviderConnectorRegistry):
        raise TypeError("Home Assistant provider connector registry installation is invalid")
    existing = data.get(_REGISTRY_DATA_KEY)
    if existing is not None and existing is not registry:
        raise ProviderApiError("A different Nitrado provider connector registry is already installed")
    data[_REGISTRY_DATA_KEY] = registry


def remove_provider_connector_registry(hass: Any, registry: ProviderConnectorRegistry) -> None:
    data = getattr(hass, "data", None)
    if isinstance(data, dict) and data.get(_REGISTRY_DATA_KEY) is registry:
        data.pop(_REGISTRY_DATA_KEY, None)


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _await_despite_cancellation[T](awaitable: Awaitable[T], *, name: str) -> T:
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


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_IDENTITY_CHARS:
        raise ValueError(f"{label} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _relative_path(value: Any, *, allow_root: bool) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096:
        raise ValueError("provider filesystem path is invalid")
    if "\\" in value or value.startswith("/") or "\x00" in value:
        raise ValueError("provider filesystem path is invalid")
    if value == "":
        if allow_root:
            return value
        raise ValueError("provider filesystem path cannot be root")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or len(part.encode("utf-8")) > 255 for part in parts):
        raise ValueError("provider filesystem path is invalid")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in part) for part in parts):
        raise ValueError("provider filesystem path is invalid")
    if any(unicodedata.normalize("NFC", part) != part for part in parts):
        raise ValueError("provider filesystem path must use NFC-normalized Unicode")
    return "/".join(parts)


def _require_matching_consumer_entry(hass: Any, domain: str, entry_id: str) -> None:
    domain = _identity(domain, "consumer domain")
    entry_id = _identity(entry_id, "consumer entry ID")
    manager = getattr(hass, "config_entries", None)
    getter = getattr(manager, "async_get_entry", None)
    entry = getter(entry_id) if callable(getter) else None
    if entry is None or getattr(entry, "domain", None) != domain:
        raise ProviderAuthorizationError("Provider consumer identity does not match a loaded config entry")


def _digest(value: Any) -> str:
    normalized = str(value).lower().strip()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("payload digest must be a SHA-256 hex digest")
    return normalized


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _manifest_digest(entries: Iterable[ProviderTreeManifestEntry]) -> str:
    payload = [{"path": item.path, "size_bytes": item.size_bytes, "sha256": item.sha256} for item in sorted(entries)]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _scopes(value: Iterable[ProviderScope]) -> frozenset[ProviderScope]:
    try:
        scopes = frozenset(value)
    except TypeError as err:
        raise ValueError("provider scopes are invalid") from err
    if not scopes or len(scopes) > MAX_PROVIDER_SCOPES:
        raise ValueError("at least one bounded provider scope is required")
    if any(not isinstance(scope, ProviderScope) for scope in scopes):
        raise ValueError("provider scopes must use ProviderScope values")
    return scopes


def _roots(value: Iterable[str]) -> frozenset[str]:
    try:
        raw_roots = tuple(value)
    except TypeError as err:
        raise ValueError("provider filesystem roots are invalid") from err
    if len(raw_roots) > 64:
        raise ValueError("provider filesystem root limit exceeded")
    return frozenset(_relative_path(root, allow_root=True) for root in raw_roots)


def _path_within(path: str, root: str) -> bool:
    return root == "" or path == root or path.startswith(f"{root}/")


def _scope_for_action(action: str) -> ProviderScope:
    if action == "filesystem_tree_replace":
        return ProviderScope.FILESYSTEM_REPLACE
    if action == "native_backup_restore":
        return ProviderScope.NATIVE_BACKUP_RESTORE
    raise ProviderPlanError("Mutation plan action is unsupported")


__all__ = (
    "DEFAULT_PLAN_TTL_SECONDS",
    "DEFAULT_PROVIDER_TREE_CHUNK_BYTES",
    "PROVIDER_CONNECTOR_API_VERSION",
    "NativeBackup",
    "NativeBackupIdentity",
    "NativeBackupInventory",
    "NativeBackupRestoreRequest",
    "NativeBackupRestoreResult",
    "ProviderApiError",
    "ProviderAuthorizationError",
    "ProviderCapabilities",
    "ProviderConnector",
    "ProviderConsumerLease",
    "ProviderFileContent",
    "ProviderFileReadRequest",
    "ProviderGrant",
    "ProviderGrantRequest",
    "ProviderLeaseRevokedError",
    "ProviderMutationApproval",
    "ProviderMutationExecutionError",
    "ProviderMutationPlan",
    "ProviderMutationProgress",
    "ProviderMutationState",
    "ProviderNativeBackupInventory",
    "ProviderNativeBackupRestoreResult",
    "ProviderPlanError",
    "ProviderScope",
    "ProviderServiceRef",
    "ProviderTreeContentChunk",
    "ProviderTreeContentHandle",
    "ProviderTreeFile",
    "ProviderTreeManifest",
    "ProviderTreeManifestEntry",
    "ProviderTreeReplaceRequest",
    "ProviderTreeReplaceResult",
    "ProviderTreeRequest",
    "ProviderTreeSnapshot",
    "ProviderTreeVerifyRequest",
    "ProviderTreeVerifyResult",
    "async_register_provider_consumer",
)
