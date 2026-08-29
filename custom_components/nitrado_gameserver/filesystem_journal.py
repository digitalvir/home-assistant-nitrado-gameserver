"""Durable, provider-agnostic journal for recoverable filesystem mutations.

The journal stores only bounded transaction metadata. Recovery bytes live in a
separate injected blob store so Home Assistant's JSON ``Store`` never contains
game files, credentials, or other binary material. A transaction can reach the
terminal ``completed`` state only after the caller supplies exact verification
evidence; a provider acknowledgement is intentionally not representable as
completion evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

JOURNAL_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 128
DEFAULT_MAX_RECOVERY_BLOB_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_RECOVERY_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_COMPLETED_RETENTION = timedelta(days=7)
MAX_TARGET_PATH_LENGTH = 1024
MAX_IDENTIFIER_LENGTH = 128
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FilesystemJournalError(RuntimeError):
    """Base error for durable filesystem journal operations."""


class JournalIntegrityError(FilesystemJournalError):
    """Raised when corrupt durable metadata makes safe mutation impossible."""


class JournalLimitError(FilesystemJournalError):
    """Raised when a configured journal or recovery-store bound is exceeded."""


class JournalTransitionError(FilesystemJournalError):
    """Raised for an invalid transaction state transition."""


class RecoveryBlobError(FilesystemJournalError):
    """Raised when recovery content is missing or fails exact verification."""

    def __init__(self, issue: JournalIssue, message: str) -> None:
        super().__init__(message)
        self.issue = issue


class TransactionState(StrEnum):
    """Durable transaction states."""

    PREPARED = "prepared"
    MUTATING = "mutating"
    VERIFYING = "verifying"
    ROLLBACK_REQUIRED = "rollback_required"
    COMPLETED = "completed"


class FileTransactionOperation(StrEnum):
    """Provider-generic filesystem mutation classes."""

    WRITE_FILE = "write_file"
    DELETE_PATH = "delete_path"
    REPLACE_TREE = "replace_tree"
    RESTORE_TREE = "restore_tree"


class VerificationPurpose(StrEnum):
    """Content whose exact identity is being verified."""

    COMMIT = "commit"
    ROLLBACK = "rollback"


class VerificationKind(StrEnum):
    """Accepted exact verification mechanisms.

    Provider acknowledgements are deliberately absent.
    """

    EXACT_READBACK_SHA256 = "exact_readback_sha256"
    EXACT_MANIFEST_SHA256 = "exact_manifest_sha256"
    INDEPENDENT_WITNESS_SHA256 = "independent_witness_sha256"


class CompletionDisposition(StrEnum):
    """Terminal outcome after exact verification."""

    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class JournalFailureCode(StrEnum):
    """Secret-safe failure classifications persisted for recovery."""

    CANCELLED = "cancelled"
    CONNECTION_LOST = "connection_lost"
    CREDENTIALS_REJECTED = "credentials_rejected"
    PROVIDER_REJECTED = "provider_rejected"
    SERVER_STATE_CHANGED = "server_state_changed"
    SOURCE_CHANGED = "source_changed"
    TRANSFER_INCOMPLETE = "transfer_incomplete"
    VERIFICATION_FAILED = "verification_failed"
    ROLLBACK_FAILED = "rollback_failed"
    UNKNOWN = "unknown"


class JournalIssue(StrEnum):
    """Facts suitable for creating a Home Assistant Repairs issue."""

    RECOVERY_REQUIRED = "recovery_required"
    RECOVERY_BLOB_MISSING = "recovery_blob_missing"
    RECOVERY_BLOB_CORRUPT = "recovery_blob_corrupt"
    CORRUPT_METADATA = "corrupt_metadata"


class AsyncJournalMetadataStore(Protocol):
    """Injectable JSON-compatible durable metadata persistence."""

    async def async_load(self) -> Mapping[str, Any] | None:
        """Load the last atomically committed metadata document."""

    async def async_save(self, payload: Mapping[str, Any]) -> None:
        """Atomically replace the durable metadata document."""


class AsyncRecoveryBlobStore(Protocol):
    """Injectable private binary recovery storage."""

    async def async_write(self, blob_id: str, content: bytes) -> None:
        """Atomically and durably write one blob, replacing no existing blob."""

    async def async_read(self, blob_id: str) -> bytes:
        """Read one recovery blob."""

    async def async_delete(self, blob_id: str) -> None:
        """Delete one recovery blob if present."""


@dataclass(frozen=True, slots=True)
class ServiceReference:
    """Collision-safe reference to one Nitrado service."""

    account_entry_id: str
    service_id: str

    def __post_init__(self) -> None:
        _validate_identifier(self.account_entry_id, "account entry id")
        _validate_identifier(self.service_id, "service id")


@dataclass(frozen=True, slots=True)
class RecoveryBlobReference:
    """Integrity metadata for separately stored recovery bytes."""

    blob_id: str = field(repr=False)
    size: int
    sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_identifier(self.blob_id, "recovery blob id")
        if self.size < 0:
            raise ValueError("recovery blob size must not be negative")
        _validate_sha256(self.sha256)


@dataclass(frozen=True, slots=True)
class VerificationExpectation:
    """Exact identity required before commit or rollback can complete."""

    purpose: VerificationPurpose
    sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_sha256(self.sha256)


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """Exact, caller-observed verification evidence."""

    kind: VerificationKind
    expected_sha256: str = field(repr=False)
    observed_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_sha256(self.expected_sha256)
        _validate_sha256(self.observed_sha256)


@dataclass(frozen=True, slots=True)
class FilesystemTransactionRecord:
    """Secret-safe durable transaction metadata."""

    transaction_id: str
    service: ServiceReference
    operation: FileTransactionOperation
    target_path: str = field(repr=False)
    state: TransactionState
    created_at: datetime
    updated_at: datetime
    recovery: RecoveryBlobReference | None = field(default=None, repr=False)
    verification: VerificationExpectation | None = field(default=None, repr=False)
    failure_code: JournalFailureCode | None = None
    disposition: CompletionDisposition | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.transaction_id, "transaction id")
        _validate_target_path(self.target_path)
        _validate_utc(self.created_at)
        _validate_utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("transaction update time precedes creation time")
        if self.state is TransactionState.COMPLETED and self.disposition is None:
            raise ValueError("completed transaction requires a disposition")
        if self.state is not TransactionState.COMPLETED and self.disposition is not None:
            raise ValueError("only completed transactions may have a disposition")
        if self.state in {TransactionState.VERIFYING, TransactionState.COMPLETED} and self.verification is None:
            raise ValueError("verifying and completed transactions require an exact expectation")
        if self.state not in {TransactionState.VERIFYING, TransactionState.COMPLETED} and self.verification is not None:
            raise ValueError("verification expectation is only valid while verifying or completed")


@dataclass(frozen=True, slots=True)
class UnresolvedTransactionFact:
    """Sanitized fact ready for Repairs and diagnostics surfaces."""

    issue: JournalIssue
    transaction_id: str
    service: ServiceReference | None
    operation: FileTransactionOperation | None
    state: TransactionState | None
    recovery_available: bool
    created_at: datetime | None
    failure_code: JournalFailureCode | None


class FilesystemTransactionJournal:
    """Bounded durable journal for verified provider mutations."""

    def __init__(
        self,
        metadata_store: AsyncJournalMetadataStore,
        blob_store: AsyncRecoveryBlobStore,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_blob_bytes: int = DEFAULT_MAX_RECOVERY_BLOB_BYTES,
        max_total_blob_bytes: int = DEFAULT_MAX_RECOVERY_TOTAL_BYTES,
        completed_retention: timedelta = DEFAULT_COMPLETED_RETENTION,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_records < 1 or max_blob_bytes < 1 or max_total_blob_bytes < 1:
            raise ValueError("journal limits must be positive")
        if max_blob_bytes > max_total_blob_bytes:
            raise ValueError("single recovery blob limit exceeds total blob limit")
        if completed_retention < timedelta(0):
            raise ValueError("completed retention must not be negative")
        self._metadata_store = metadata_store
        self._blob_store = blob_store
        self._max_records = max_records
        self._max_blob_bytes = max_blob_bytes
        self._max_total_blob_bytes = max_total_blob_bytes
        self._completed_retention = completed_retention
        self._clock = clock
        self._records: dict[str, FilesystemTransactionRecord] = {}
        self._load_issues: tuple[UnresolvedTransactionFact, ...] = ()
        self._initialized = False
        self._integrity_blocked = False
        self._lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Load durable state and prune only expired completed transactions."""

        async with self._lock:
            if self._initialized:
                return
            payload = await self._metadata_store.async_load()
            records, issues, blocked = _deserialize_document(payload)
            if not blocked:
                blob_sizes = [record.recovery.size for record in records.values() if record.recovery is not None]
                if (
                    any(size > self._max_blob_bytes for size in blob_sizes)
                    or sum(blob_sizes) > self._max_total_blob_bytes
                ):
                    issues = (_corrupt_metadata_fact(),)
                    blocked = True
            self._records = records
            self._load_issues = issues
            self._integrity_blocked = blocked
            self._initialized = True
            if not blocked:
                await self._async_reconcile_orphan_blobs_locked()
                cancelled = await self._async_prune_locked(self._clock())
                _raise_deferred_cancellation(cancelled)

    async def async_prepare(
        self,
        service: ServiceReference,
        operation: FileTransactionOperation,
        target_path: str,
        *,
        recovery_content: bytes | None = None,
    ) -> FilesystemTransactionRecord:
        """Create a durable prepared transaction and optional recovery blob."""

        await self._ensure_initialized()
        target_path = _validate_target_path(target_path)
        recovery_data = bytes(recovery_content) if recovery_content is not None else None
        if recovery_data is not None and len(recovery_data) > self._max_blob_bytes:
            raise JournalLimitError("recovery blob exceeds the per-transaction limit")

        async with self._lock:
            self._ensure_writable()
            now = self._clock()
            cancelled = await self._async_prune_locked(now)
            _raise_deferred_cancellation(cancelled)
            if recovery_data is not None:
                cancelled = await self._async_prune_for_blob_space_locked(len(recovery_data))
                _raise_deferred_cancellation(cancelled)
            if len(self._records) >= self._max_records:
                raise JournalLimitError("transaction journal record limit reached")
            recovery_size = sum(
                record.recovery.size for record in self._records.values() if record.recovery is not None
            )
            if recovery_data is not None and recovery_size + len(recovery_data) > self._max_total_blob_bytes:
                raise JournalLimitError("recovery blob store total limit reached")

            transaction_id = secrets.token_hex(16)
            recovery: RecoveryBlobReference | None = None
            if recovery_data is not None:
                blob_id = f"recovery-{secrets.token_hex(16)}"
                recovery = RecoveryBlobReference(blob_id, len(recovery_data), _sha256(recovery_data))
                try:
                    cancelled = await _async_write_blob(self._blob_store, blob_id, recovery_data)
                except Exception:
                    await _best_effort_delete(self._blob_store, blob_id)
                    raise
                if cancelled:
                    await _best_effort_delete(self._blob_store, blob_id)
                    raise asyncio.CancelledError

            record = FilesystemTransactionRecord(
                transaction_id=transaction_id,
                service=service,
                operation=operation,
                target_path=target_path,
                state=TransactionState.PREPARED,
                created_at=now,
                updated_at=now,
                recovery=recovery,
            )
            try:
                cancelled = await self._async_commit_records({**self._records, transaction_id: record})
            except Exception:
                if recovery is not None:
                    await _best_effort_delete(self._blob_store, recovery.blob_id)
                raise
            _raise_deferred_cancellation(cancelled)
            return record

    async def async_mark_mutating(self, transaction_id: str) -> FilesystemTransactionRecord:
        """Record that remote mutation is about to begin."""

        return await self._async_transition(
            transaction_id,
            allowed=frozenset({TransactionState.PREPARED}),
            state=TransactionState.MUTATING,
        )

    async def async_mark_verifying(
        self,
        transaction_id: str,
        expectation: VerificationExpectation,
    ) -> FilesystemTransactionRecord:
        """Record exact commit or rollback identity before verification."""

        return await self._async_transition(
            transaction_id,
            allowed=frozenset({TransactionState.MUTATING, TransactionState.ROLLBACK_REQUIRED}),
            state=TransactionState.VERIFYING,
            verification=expectation,
        )

    async def async_mark_rollback_required(
        self,
        transaction_id: str,
        failure_code: JournalFailureCode,
    ) -> FilesystemTransactionRecord:
        """Persist that recovery is required before normal operation resumes."""

        return await self._async_transition(
            transaction_id,
            allowed=frozenset({TransactionState.MUTATING, TransactionState.VERIFYING}),
            state=TransactionState.ROLLBACK_REQUIRED,
            verification=None,
            failure_code=failure_code,
        )

    async def async_complete(
        self,
        transaction_id: str,
        evidence: VerificationEvidence,
        disposition: CompletionDisposition,
    ) -> FilesystemTransactionRecord:
        """Complete only after exact expected and observed identities match."""

        await self._ensure_initialized()
        async with self._lock:
            self._ensure_writable()
            record = self._require_record(transaction_id)
            if record.state is not TransactionState.VERIFYING or record.verification is None:
                raise JournalTransitionError("transaction is not awaiting exact verification")
            expected_disposition = (
                CompletionDisposition.COMMITTED
                if record.verification.purpose is VerificationPurpose.COMMIT
                else CompletionDisposition.ROLLED_BACK
            )
            if disposition is not expected_disposition:
                raise JournalTransitionError("completion disposition does not match verification purpose")
            if evidence.expected_sha256 != record.verification.sha256:
                raise JournalTransitionError("evidence does not match the durable verification expectation")
            if evidence.observed_sha256 != evidence.expected_sha256:
                raise JournalTransitionError("observed provider content does not match the expected identity")
            completed = replace(
                record,
                state=TransactionState.COMPLETED,
                updated_at=self._clock(),
                disposition=disposition,
            )
            cancelled = await self._async_commit_records({**self._records, transaction_id: completed})
            _raise_deferred_cancellation(cancelled)
            return completed

    async def async_read_recovery(self, transaction_id: str) -> bytes:
        """Read and independently verify one transaction's recovery bytes."""

        await self._ensure_initialized()
        async with self._lock:
            record = self._require_record(transaction_id)
            reference = record.recovery
        if reference is None:
            raise RecoveryBlobError(JournalIssue.RECOVERY_BLOB_MISSING, "transaction has no recovery content")
        try:
            content = bytes(await self._blob_store.async_read(reference.blob_id))
        except (OSError, KeyError) as err:
            raise RecoveryBlobError(JournalIssue.RECOVERY_BLOB_MISSING, "recovery content is unavailable") from err
        if len(content) != reference.size or _sha256(content) != reference.sha256:
            raise RecoveryBlobError(
                JournalIssue.RECOVERY_BLOB_CORRUPT,
                "recovery content failed exact integrity verification",
            )
        return content

    async def async_recovery_candidates(self) -> tuple[FilesystemTransactionRecord, ...]:
        """Enumerate non-terminal transactions after setup or restart."""

        await self._ensure_initialized()
        async with self._lock:
            return tuple(
                sorted(
                    (record for record in self._records.values() if record.state is not TransactionState.COMPLETED),
                    key=lambda record: (record.created_at, record.transaction_id),
                )
            )

    async def async_unresolved_facts(self) -> tuple[UnresolvedTransactionFact, ...]:
        """Return sanitized, network-free facts suitable for Repairs."""

        await self._ensure_initialized()
        async with self._lock:
            facts = list(self._load_issues)
            records = tuple(self._records.values())
        for record in records:
            if record.state is TransactionState.COMPLETED:
                continue
            issue = JournalIssue.RECOVERY_REQUIRED
            recovery_available = record.recovery is not None
            if record.recovery is not None:
                try:
                    await self.async_read_recovery(record.transaction_id)
                except RecoveryBlobError as err:
                    issue = err.issue
                    recovery_available = False
            facts.append(
                UnresolvedTransactionFact(
                    issue=issue,
                    transaction_id=record.transaction_id,
                    service=record.service,
                    operation=record.operation,
                    state=record.state,
                    recovery_available=recovery_available,
                    created_at=record.created_at,
                    failure_code=record.failure_code,
                )
            )
        return tuple(sorted(facts, key=lambda fact: fact.transaction_id))

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return bounded secret-free journal diagnostics without network I/O."""

        await self._ensure_initialized()
        async with self._lock:
            records = tuple(sorted(self._records.values(), key=lambda item: item.transaction_id))
            issues = self._load_issues
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "integrity_blocked": self._integrity_blocked,
            "record_count": len(records),
            "unresolved_count": sum(record.state is not TransactionState.COMPLETED for record in records) + len(issues),
            "records": [_diagnostic_record(record) for record in records],
            "load_issues": [fact.issue for fact in issues],
        }

    async def _async_transition(
        self,
        transaction_id: str,
        *,
        allowed: frozenset[TransactionState],
        state: TransactionState,
        verification: VerificationExpectation | object | None = ...,  # sentinel preserves current value
        failure_code: JournalFailureCode | object | None = ...,
    ) -> FilesystemTransactionRecord:
        await self._ensure_initialized()
        async with self._lock:
            self._ensure_writable()
            record = self._require_record(transaction_id)
            if record.state not in allowed:
                raise JournalTransitionError(f"cannot move transaction from {record.state} to {state}")
            if state is TransactionState.VERIFYING and isinstance(
                verification,
                VerificationExpectation,
            ):
                required_purpose = (
                    VerificationPurpose.COMMIT
                    if record.state is TransactionState.MUTATING
                    else VerificationPurpose.ROLLBACK
                )
                if verification.purpose is not required_purpose:
                    raise JournalTransitionError("verification purpose does not match the durable transaction state")
            changed = replace(
                record,
                state=state,
                updated_at=self._clock(),
                verification=record.verification if verification is ... else verification,
                failure_code=record.failure_code if failure_code is ... else failure_code,
            )
            cancelled = await self._async_commit_records({**self._records, transaction_id: changed})
            _raise_deferred_cancellation(cancelled)
            return changed

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.async_initialize()

    def _ensure_writable(self) -> None:
        if self._integrity_blocked:
            raise JournalIntegrityError("filesystem journal metadata is corrupt; mutation is blocked")

    def _require_record(self, transaction_id: str) -> FilesystemTransactionRecord:
        try:
            return self._records[transaction_id]
        except KeyError as err:
            raise KeyError(f"unknown filesystem transaction {transaction_id}") from err

    async def _async_commit_records(self, records: dict[str, FilesystemTransactionRecord]) -> bool:
        """Drain an atomic metadata commit and report deferred cancellation.

        The separately owned save avoids the fatal ambiguity where cancellation
        arrives after durable persistence but before the caller learns whether
        it won. Public operations re-raise cancellation only after local state
        and recovery blobs agree with the durable document.
        """

        payload = _serialize_document(records)
        save_task = asyncio.create_task(
            self._metadata_store.async_save(payload),
            name="nitrado-filesystem-journal-save",
        )
        cancelled = False
        while True:
            try:
                await asyncio.shield(save_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if save_task.done():
                    save_task.result()
                    break
        self._records = records
        return cancelled

    async def _async_prune_locked(self, now: datetime) -> bool:
        cutoff = now - self._completed_retention
        removable = [
            record
            for record in self._records.values()
            if record.state is TransactionState.COMPLETED and record.updated_at <= cutoff
        ]
        remaining_count = len(self._records) - len(removable)
        if remaining_count > self._max_records:
            retained_completed = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.state is TransactionState.COMPLETED and record not in removable
                ),
                key=lambda record: (record.updated_at, record.transaction_id),
            )
            removable.extend(retained_completed[: remaining_count - self._max_records])
        if not removable:
            return False
        records = dict(self._records)
        for record in removable:
            records.pop(record.transaction_id, None)
        cancelled = await self._async_commit_records(records)
        for record in removable:
            if record.recovery is not None:
                await _best_effort_delete(self._blob_store, record.recovery.blob_id)
        return cancelled

    async def _async_prune_for_blob_space_locked(self, required_bytes: int) -> bool:
        """Prune oldest completed recovery blobs under byte pressure.

        Unresolved transactions are never candidates.  Metadata is committed
        before the corresponding private blobs are deleted, preserving the
        same crash ordering as normal retention pruning.
        """

        current = sum(record.recovery.size for record in self._records.values() if record.recovery is not None)
        excess = current + required_bytes - self._max_total_blob_bytes
        if excess <= 0:
            return False
        candidates = sorted(
            (
                record
                for record in self._records.values()
                if record.state is TransactionState.COMPLETED and record.recovery is not None
            ),
            key=lambda record: (record.updated_at, record.transaction_id),
        )
        removable: list[FilesystemTransactionRecord] = []
        reclaimed = 0
        for record in candidates:
            recovery = record.recovery
            if recovery is None:
                continue
            removable.append(record)
            reclaimed += recovery.size
            if reclaimed >= excess:
                break
        if reclaimed < excess:
            return False
        records = dict(self._records)
        for record in removable:
            records.pop(record.transaction_id, None)
        cancelled = await self._async_commit_records(records)
        for record in removable:
            recovery = record.recovery
            if recovery is not None:
                await _best_effort_delete(self._blob_store, recovery.blob_id)
        return cancelled

    async def _async_reconcile_orphan_blobs_locked(self) -> None:
        """Delete only unreferenced blobs from this journal's private namespace."""

        lister = getattr(self._blob_store, "async_list", None)
        if not callable(lister):
            return
        referenced = {record.recovery.blob_id for record in self._records.values() if record.recovery is not None}
        for blob_id in await lister():
            if blob_id not in referenced:
                await _best_effort_delete(self._blob_store, blob_id)


def _serialize_document(records: Mapping[str, FilesystemTransactionRecord]) -> dict[str, Any]:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "records": [
            _serialize_record(record) for record in sorted(records.values(), key=lambda item: item.transaction_id)
        ],
    }


def _serialize_record(record: FilesystemTransactionRecord) -> dict[str, Any]:
    recovery = None
    if record.recovery is not None:
        recovery = {
            "blob_id": record.recovery.blob_id,
            "size": record.recovery.size,
            "sha256": record.recovery.sha256,
        }
    verification = None
    if record.verification is not None:
        verification = {
            "purpose": record.verification.purpose,
            "sha256": record.verification.sha256,
        }
    return {
        "transaction_id": record.transaction_id,
        "account_entry_id": record.service.account_entry_id,
        "service_id": record.service.service_id,
        "operation": record.operation,
        "target_path": record.target_path,
        "state": record.state,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "recovery": recovery,
        "verification": verification,
        "failure_code": record.failure_code,
        "disposition": record.disposition,
    }


def _deserialize_document(
    payload: Mapping[str, Any] | None,
) -> tuple[
    dict[str, FilesystemTransactionRecord],
    tuple[UnresolvedTransactionFact, ...],
    bool,
]:
    if payload is None:
        return {}, (), False
    try:
        if payload.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise ValueError("unsupported schema")
        raw_records = payload.get("records")
        if not isinstance(raw_records, list) or len(raw_records) > DEFAULT_MAX_RECORDS * 4:
            raise ValueError("invalid record collection")
        records: dict[str, FilesystemTransactionRecord] = {}
        for raw in raw_records:
            record = _deserialize_record(raw)
            if record.transaction_id in records:
                raise ValueError("duplicate transaction id")
            records[record.transaction_id] = record
        return records, (), False
    except (KeyError, TypeError, ValueError):
        return {}, (_corrupt_metadata_fact(),), True


def _corrupt_metadata_fact() -> UnresolvedTransactionFact:
    return UnresolvedTransactionFact(
        issue=JournalIssue.CORRUPT_METADATA,
        transaction_id="journal-metadata",
        service=None,
        operation=None,
        state=None,
        recovery_available=False,
        created_at=None,
        failure_code=None,
    )


def _deserialize_record(raw: Any) -> FilesystemTransactionRecord:
    if not isinstance(raw, Mapping):
        raise TypeError("record is not a mapping")
    recovery_raw = raw.get("recovery")
    recovery = None
    if recovery_raw is not None:
        if not isinstance(recovery_raw, Mapping):
            raise TypeError("invalid recovery reference")
        recovery = RecoveryBlobReference(
            str(recovery_raw["blob_id"]),
            int(recovery_raw["size"]),
            str(recovery_raw["sha256"]),
        )
    verification_raw = raw.get("verification")
    verification = None
    if verification_raw is not None:
        if not isinstance(verification_raw, Mapping):
            raise TypeError("invalid verification expectation")
        verification = VerificationExpectation(
            VerificationPurpose(verification_raw["purpose"]),
            str(verification_raw["sha256"]),
        )
    failure = raw.get("failure_code")
    disposition = raw.get("disposition")
    return FilesystemTransactionRecord(
        transaction_id=str(raw["transaction_id"]),
        service=ServiceReference(str(raw["account_entry_id"]), str(raw["service_id"])),
        operation=FileTransactionOperation(raw["operation"]),
        target_path=str(raw["target_path"]),
        state=TransactionState(raw["state"]),
        created_at=_parse_datetime(raw["created_at"]),
        updated_at=_parse_datetime(raw["updated_at"]),
        recovery=recovery,
        verification=verification,
        failure_code=JournalFailureCode(failure) if failure is not None else None,
        disposition=CompletionDisposition(disposition) if disposition is not None else None,
    )


def _diagnostic_record(record: FilesystemTransactionRecord) -> dict[str, Any]:
    return {
        "transaction_id": record.transaction_id,
        "account_entry_id": record.service.account_entry_id,
        "service_id": record.service.service_id,
        "operation": record.operation,
        "state": record.state,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "target_path_sha256_prefix": _sha256(record.target_path.encode())[:16],
        "target_path_depth": len(record.target_path.split("/")),
        "recovery_available": record.recovery is not None,
        "recovery_size": record.recovery.size if record.recovery is not None else None,
        "verification_purpose": record.verification.purpose if record.verification is not None else None,
        "failure_code": record.failure_code,
        "disposition": record.disposition,
    }


async def _best_effort_delete(blob_store: AsyncRecoveryBlobStore, blob_id: str) -> None:
    try:
        await blob_store.async_delete(blob_id)
    except (OSError, KeyError):
        return


async def _async_write_blob(blob_store: AsyncRecoveryBlobStore, blob_id: str, content: bytes) -> bool:
    """Drain an atomic blob write and report deferred caller cancellation."""

    write_task = asyncio.create_task(
        blob_store.async_write(blob_id, content),
        name="nitrado-filesystem-recovery-write",
    )
    cancelled = False
    while True:
        try:
            await asyncio.shield(write_task)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if write_task.done():
                write_task.result()
                return cancelled


def _raise_deferred_cancellation(cancelled: bool) -> None:
    if cancelled:
        raise asyncio.CancelledError


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"invalid {label}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"invalid {label}")
    return value


def _validate_target_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TARGET_PATH_LENGTH:
        raise ValueError("invalid target path")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise ValueError("target path must be a canonical relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("target path must be a canonical relative POSIX path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("target path contains control characters")
    return value


def _validate_sha256(value: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("invalid SHA-256 digest")


def _validate_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("journal timestamps must be timezone-aware UTC")


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    _validate_utc(parsed)
    return parsed


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
