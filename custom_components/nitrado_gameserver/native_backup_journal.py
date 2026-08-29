"""Durable facts for provider-native backup restore operations.

Native restores cannot be rolled back by this integration and a successful
HTTP response does not prove completion.  This journal therefore records the
exact provider target and preconditions before transport, then distinguishes
provider acceptance from an observed provider restore transition.  It never
represents game health.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

NATIVE_BACKUP_JOURNAL_SCHEMA_VERSION = 1
DEFAULT_MAX_NATIVE_RESTORE_RECORDS = 64
DEFAULT_NATIVE_RESTORE_RETENTION = timedelta(days=30)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class NativeBackupJournalError(RuntimeError):
    """Base error for durable native-backup operation tracking."""


class NativeBackupJournalIntegrityError(NativeBackupJournalError):
    """Raised when corrupt metadata prevents safe restore operations."""


class NativeBackupJournalLimitError(NativeBackupJournalError):
    """Raised when bounded durable tracking is full."""


class NativeBackupJournalTransitionError(NativeBackupJournalError):
    """Raised for an invalid restore state transition."""


class NativeRestoreState(StrEnum):
    """Truthful durable restore states."""

    PREPARED = "prepared"
    REQUESTING = "requesting"
    ACCEPTED = "accepted"
    OBSERVING = "observing"
    OBSERVED = "observed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ABORTED_BEFORE_REQUEST = "aborted_before_request"
    ACKNOWLEDGED_UNVERIFIED = "acknowledged_unverified"


class NativeRestoreFailureCode(StrEnum):
    """Secret-free classifications suitable for diagnostics and Repairs."""

    CANCELLED = "cancelled"
    TRANSPORT_UNCONFIRMED = "transport_unconfirmed"
    OBSERVATION_TIMEOUT = "observation_timeout"
    OBSERVATION_FAILED = "observation_failed"
    PROCESS_RESTARTED = "process_restarted"
    PRECONDITION_FAILED = "precondition_failed"


class NativeRestoreIssue(StrEnum):
    """Actionable facts exposed to Home Assistant Repairs."""

    RESTORE_REVIEW_REQUIRED = "restore_review_required"
    CORRUPT_METADATA = "corrupt_metadata"


class AsyncNativeBackupJournalStore(Protocol):
    """Atomic JSON-compatible metadata persistence."""

    async def async_load(self) -> Any: ...

    async def async_save(self, payload: Mapping[str, Any]) -> None: ...

    async def async_quarantine(self, payload: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class NativeRestoreTarget:
    """Exact account-scoped provider backup identity and preconditions."""

    account_entry_id: str
    service_id: str
    folder: str
    backup_id: str
    expected_size_bytes: int
    expected_created_at: str
    expected_status: str | None
    expected_backup_type: str | None = None
    expected_file_size_bytes: int | None = None

    def __post_init__(self) -> None:
        _identifier(self.account_entry_id, "account entry id")
        if not self.service_id.isdigit():
            raise ValueError("service id must contain only digits")
        _folder(self.folder)
        _backup_number(self.backup_id)
        if not 0 <= self.expected_size_bytes <= (1 << 63) - 1:
            raise ValueError("expected backup size is outside its supported range")
        _bounded_text(self.expected_created_at, "expected backup timestamp")
        if self.expected_status is not None:
            _bounded_text(self.expected_status, "expected backup status")
        if self.expected_backup_type is not None:
            _bounded_text(self.expected_backup_type, "expected backup type")
        if self.expected_file_size_bytes is not None and not (0 <= self.expected_file_size_bytes <= (1 << 63) - 1):
            raise ValueError("expected backup file size is outside its supported range")


@dataclass(frozen=True, slots=True)
class NativeRestoreRecord:
    """One durable provider-native restore operation."""

    operation_id: str
    target: NativeRestoreTarget
    state: NativeRestoreState
    created_at: datetime
    updated_at: datetime
    provider_status: str | None = None
    failure_code: NativeRestoreFailureCode | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation id")
        _utc(self.created_at)
        _utc(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("restore update time precedes creation time")
        if self.provider_status is not None:
            _bounded_text(self.provider_status, "provider status")


@dataclass(frozen=True, slots=True)
class NativeRestoreFact:
    """Secret-free unresolved fact for diagnostics and Repairs."""

    issue: NativeRestoreIssue
    operation_id: str
    target: NativeRestoreTarget | None
    state: NativeRestoreState | None
    created_at: datetime | None
    failure_code: NativeRestoreFailureCode | None


class NativeBackupRestoreJournal:
    """Bounded durable native-backup restore tracker."""

    def __init__(
        self,
        store: AsyncNativeBackupJournalStore,
        *,
        max_records: int = DEFAULT_MAX_NATIVE_RESTORE_RECORDS,
        completed_retention: timedelta = DEFAULT_NATIVE_RESTORE_RETENTION,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_records < 1:
            raise ValueError("native restore journal limit must be positive")
        if completed_retention < timedelta(0):
            raise ValueError("native restore retention cannot be negative")
        self._store = store
        self._max_records = max_records
        self._completed_retention = completed_retention
        self._clock = clock
        self._records: dict[str, NativeRestoreRecord] = {}
        self._integrity_blocked = False
        self._corrupt_payload: Any = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def async_initialize(self) -> None:
        """Load state and conservatively classify interrupted requests."""

        async with self._lock:
            if self._initialized:
                return
            payload = await self._store.async_load()
            try:
                self._records = _deserialize(payload)
            except (AttributeError, KeyError, TypeError, ValueError):
                self._records = {}
                self._integrity_blocked = True
                self._corrupt_payload = payload
                self._initialized = True
                return
            self._initialized = True
            now = self._clock()
            changed = False
            records = dict(self._records)
            for operation_id, record in tuple(records.items()):
                if record.state is NativeRestoreState.PREPARED:
                    records[operation_id] = replace(
                        record,
                        state=NativeRestoreState.ABORTED_BEFORE_REQUEST,
                        updated_at=now,
                        failure_code=NativeRestoreFailureCode.PROCESS_RESTARTED,
                    )
                    changed = True
                elif record.state in {
                    NativeRestoreState.REQUESTING,
                    NativeRestoreState.ACCEPTED,
                    NativeRestoreState.OBSERVING,
                }:
                    records[operation_id] = replace(
                        record,
                        state=NativeRestoreState.OUTCOME_UNKNOWN,
                        updated_at=now,
                        failure_code=NativeRestoreFailureCode.PROCESS_RESTARTED,
                    )
                    changed = True
            records, pruned = self._pruned(records, now)
            if changed or pruned:
                cancelled = await self._commit(records)
                _raise_deferred_cancel(cancelled)

    async def async_acknowledge_unverified(self, operation_id: str) -> NativeRestoreRecord:
        """Close an unknown outcome without representing it as successful.

        This is an administrator incident-resolution action.  It preserves the
        original failure classification and exact restore target for audit,
        while allowing future restores after the administrator has reviewed
        provider and game state independently.
        """

        return await self._transition(
            operation_id,
            {NativeRestoreState.OUTCOME_UNKNOWN},
            NativeRestoreState.ACKNOWLEDGED_UNVERIFIED,
            preserve_existing_facts=True,
        )

    async def async_quarantine_and_reset_corrupt(self) -> None:
        """Archive corrupt metadata, then reset only after durable quarantine.

        The caller must provide the explicit administrator confirmation.  This
        method never interprets the quarantined operation as successful.
        """

        await self._ensure_initialized()
        async with self._lock:
            if not self._integrity_blocked:
                raise NativeBackupJournalIntegrityError("native restore journal is not corrupt")
            quarantine = getattr(self._store, "async_quarantine", None)
            if not callable(quarantine):
                raise NativeBackupJournalIntegrityError(
                    "native restore journal storage cannot quarantine corrupt metadata"
                )
            cancelled = await _await_durable(
                quarantine(self._corrupt_payload),
                name="nitrado-native-backup-journal-quarantine",
            )
            cancelled = (
                await _await_durable(
                    self._store.async_save(_serialize({})),
                    name="nitrado-native-backup-journal-reset",
                )
                or cancelled
            )
            self._records = {}
            self._corrupt_payload = None
            self._integrity_blocked = False
            _raise_deferred_cancel(cancelled)

    async def async_prepare(self, target: NativeRestoreTarget) -> NativeRestoreRecord:
        """Persist the exact target and strong preconditions before transport."""

        await self._ensure_initialized()
        async with self._lock:
            self._ensure_writable()
            if any(
                record.target.account_entry_id == target.account_entry_id
                and record.target.service_id == target.service_id
                and not _terminal(record.state)
                for record in self._records.values()
            ):
                raise NativeBackupJournalError("an unresolved native-backup restore already exists for this service")
            now = self._clock()
            records, _ = self._pruned(dict(self._records), now)
            if len(records) >= self._max_records:
                raise NativeBackupJournalLimitError("native restore journal record limit reached")
            record = NativeRestoreRecord(
                operation_id=secrets.token_hex(16),
                target=target,
                state=NativeRestoreState.PREPARED,
                created_at=now,
                updated_at=now,
            )
            cancelled = await self._commit({**records, record.operation_id: record})
            if cancelled:
                aborted = replace(
                    record,
                    state=NativeRestoreState.ABORTED_BEFORE_REQUEST,
                    updated_at=self._clock(),
                    failure_code=NativeRestoreFailureCode.CANCELLED,
                )
                await self._commit({**self._records, record.operation_id: aborted})
                raise asyncio.CancelledError
            return record

    async def async_mark_requesting(self, operation_id: str) -> NativeRestoreRecord:
        return await self._transition(
            operation_id,
            {NativeRestoreState.PREPARED},
            NativeRestoreState.REQUESTING,
        )

    async def async_abort_before_request(
        self,
        operation_id: str,
        failure_code: NativeRestoreFailureCode,
    ) -> NativeRestoreRecord:
        """Record that no provider restore request was sent."""

        return await self._transition(
            operation_id,
            {NativeRestoreState.PREPARED, NativeRestoreState.REQUESTING},
            NativeRestoreState.ABORTED_BEFORE_REQUEST,
            failure_code=failure_code,
        )

    async def async_mark_accepted(self, operation_id: str) -> NativeRestoreRecord:
        return await self._transition(
            operation_id,
            {NativeRestoreState.REQUESTING},
            NativeRestoreState.ACCEPTED,
        )

    async def async_mark_observing(self, operation_id: str) -> NativeRestoreRecord:
        return await self._transition(
            operation_id,
            {NativeRestoreState.ACCEPTED},
            NativeRestoreState.OBSERVING,
        )

    async def async_mark_observed(
        self,
        operation_id: str,
        *,
        provider_status: str,
    ) -> NativeRestoreRecord:
        return await self._transition(
            operation_id,
            {NativeRestoreState.OBSERVING},
            NativeRestoreState.OBSERVED,
            provider_status=provider_status,
        )

    async def async_mark_outcome_unknown(
        self,
        operation_id: str,
        failure_code: NativeRestoreFailureCode,
        *,
        provider_status: str | None = None,
    ) -> NativeRestoreRecord:
        return await self._transition(
            operation_id,
            {
                NativeRestoreState.PREPARED,
                NativeRestoreState.REQUESTING,
                NativeRestoreState.ACCEPTED,
                NativeRestoreState.OBSERVING,
            },
            NativeRestoreState.OUTCOME_UNKNOWN,
            failure_code=failure_code,
            provider_status=provider_status,
        )

    async def async_unresolved_facts(self) -> tuple[NativeRestoreFact, ...]:
        """Return network-free actionable facts."""

        await self._ensure_initialized()
        async with self._lock:
            if self._integrity_blocked:
                return (
                    NativeRestoreFact(
                        NativeRestoreIssue.CORRUPT_METADATA,
                        "native-backup-journal",
                        None,
                        None,
                        None,
                        None,
                    ),
                )
            return tuple(
                NativeRestoreFact(
                    NativeRestoreIssue.RESTORE_REVIEW_REQUIRED,
                    record.operation_id,
                    record.target,
                    record.state,
                    record.created_at,
                    record.failure_code,
                )
                for record in sorted(self._records.values(), key=lambda item: (item.created_at, item.operation_id))
                if not _terminal(record.state)
            )

    async def async_diagnostics(self) -> dict[str, Any]:
        """Return bounded secret-free restore facts without network I/O."""

        await self._ensure_initialized()
        async with self._lock:
            records = tuple(sorted(self._records.values(), key=lambda item: item.operation_id))
            return {
                "schema_version": NATIVE_BACKUP_JOURNAL_SCHEMA_VERSION,
                "integrity_blocked": self._integrity_blocked,
                "record_count": len(records),
                "unresolved_count": sum(not _terminal(record.state) for record in records),
                "records": [
                    {
                        "operation_id": record.operation_id,
                        "account_entry_id": record.target.account_entry_id,
                        "service_id": record.target.service_id,
                        "folder": record.target.folder,
                        "backup_id": record.target.backup_id,
                        "state": record.state,
                        "created_at": record.created_at.isoformat(),
                        "updated_at": record.updated_at.isoformat(),
                        "provider_status": record.provider_status,
                        "failure_code": record.failure_code,
                        "game_health_verified": False,
                    }
                    for record in records
                ],
            }

    async def _transition(
        self,
        operation_id: str,
        allowed: set[NativeRestoreState],
        state: NativeRestoreState,
        *,
        provider_status: str | None = None,
        failure_code: NativeRestoreFailureCode | None = None,
        preserve_existing_facts: bool = False,
    ) -> NativeRestoreRecord:
        await self._ensure_initialized()
        async with self._lock:
            self._ensure_writable()
            try:
                record = self._records[operation_id]
            except KeyError as err:
                raise KeyError(f"unknown native restore operation {operation_id}") from err
            if record.state not in allowed:
                raise NativeBackupJournalTransitionError(f"cannot move native restore from {record.state} to {state}")
            changed = replace(
                record,
                state=state,
                updated_at=self._clock(),
                provider_status=(record.provider_status if preserve_existing_facts else provider_status),
                failure_code=(record.failure_code if preserve_existing_facts else failure_code),
            )
            cancelled = await self._commit({**self._records, operation_id: changed})
            _raise_deferred_cancel(cancelled)
            return changed

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.async_initialize()

    def _ensure_writable(self) -> None:
        if self._integrity_blocked:
            raise NativeBackupJournalIntegrityError("native restore journal metadata is corrupt; restore is blocked")

    async def _commit(self, records: dict[str, NativeRestoreRecord]) -> bool:
        task = asyncio.create_task(
            self._store.async_save(_serialize(records)),
            name="nitrado-native-backup-journal-save",
        )
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if task.done():
                    task.result()
                    break
        self._records = records
        return cancelled

    def _pruned(
        self,
        records: dict[str, NativeRestoreRecord],
        now: datetime,
    ) -> tuple[dict[str, NativeRestoreRecord], bool]:
        cutoff = now - self._completed_retention
        pruned = {
            operation_id: record
            for operation_id, record in records.items()
            if not (_terminal(record.state) and record.updated_at <= cutoff)
        }
        return pruned, len(pruned) != len(records)


def _serialize(records: Mapping[str, NativeRestoreRecord]) -> dict[str, Any]:
    return {
        "schema_version": NATIVE_BACKUP_JOURNAL_SCHEMA_VERSION,
        "records": [
            {
                "operation_id": record.operation_id,
                "account_entry_id": record.target.account_entry_id,
                "service_id": record.target.service_id,
                "folder": record.target.folder,
                "backup_id": record.target.backup_id,
                "expected_size_bytes": record.target.expected_size_bytes,
                "expected_created_at": record.target.expected_created_at,
                "expected_status": record.target.expected_status,
                "expected_backup_type": record.target.expected_backup_type,
                "expected_file_size_bytes": record.target.expected_file_size_bytes,
                "state": record.state,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                "provider_status": record.provider_status,
                "failure_code": record.failure_code,
            }
            for record in sorted(records.values(), key=lambda item: item.operation_id)
        ],
    }


def _deserialize(payload: Mapping[str, Any] | None) -> dict[str, NativeRestoreRecord]:
    if payload is None:
        return {}
    if payload.get("schema_version") != NATIVE_BACKUP_JOURNAL_SCHEMA_VERSION:
        raise ValueError("unsupported native restore journal schema")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > DEFAULT_MAX_NATIVE_RESTORE_RECORDS * 4:
        raise ValueError("invalid native restore record collection")
    records: dict[str, NativeRestoreRecord] = {}
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise TypeError("native restore record is not a mapping")
        target = NativeRestoreTarget(
            account_entry_id=str(raw["account_entry_id"]),
            service_id=str(raw["service_id"]),
            folder=str(raw["folder"]),
            backup_id=str(raw["backup_id"]),
            expected_size_bytes=int(raw["expected_size_bytes"]),
            expected_created_at=str(raw["expected_created_at"]),
            expected_status=(str(raw["expected_status"]) if raw.get("expected_status") else None),
            expected_backup_type=(str(raw["expected_backup_type"]) if raw.get("expected_backup_type") else None),
            expected_file_size_bytes=(
                int(raw["expected_file_size_bytes"]) if raw.get("expected_file_size_bytes") is not None else None
            ),
        )
        failure = raw.get("failure_code")
        record = NativeRestoreRecord(
            operation_id=str(raw["operation_id"]),
            target=target,
            state=NativeRestoreState(raw["state"]),
            created_at=_datetime(raw["created_at"]),
            updated_at=_datetime(raw["updated_at"]),
            provider_status=(str(raw["provider_status"]) if raw.get("provider_status") else None),
            failure_code=(NativeRestoreFailureCode(failure) if failure is not None else None),
        )
        _validate_record_state(record)
        if record.operation_id in records:
            raise ValueError("duplicate native restore operation id")
        records[record.operation_id] = record
    return records


def _terminal(state: NativeRestoreState) -> bool:
    return state in {
        NativeRestoreState.OBSERVED,
        NativeRestoreState.ABORTED_BEFORE_REQUEST,
        NativeRestoreState.ACKNOWLEDGED_UNVERIFIED,
    }


def _validate_record_state(record: NativeRestoreRecord) -> None:
    """Reject impossible durable combinations instead of hiding Repairs."""

    if record.state is NativeRestoreState.OBSERVED:
        if record.provider_status is None or record.failure_code is not None:
            raise ValueError("observed native restore record is inconsistent")
        return
    if record.state is NativeRestoreState.OUTCOME_UNKNOWN:
        if record.failure_code is None:
            raise ValueError("unknown native restore record lacks a failure classification")
        return
    if record.state is NativeRestoreState.ABORTED_BEFORE_REQUEST:
        if record.failure_code not in {
            NativeRestoreFailureCode.CANCELLED,
            NativeRestoreFailureCode.PRECONDITION_FAILED,
            NativeRestoreFailureCode.PROCESS_RESTARTED,
        }:
            raise ValueError("aborted native restore record lacks an appropriate reason")
        return
    if record.state is NativeRestoreState.ACKNOWLEDGED_UNVERIFIED:
        if record.failure_code is None:
            raise ValueError("acknowledged native restore record lost its uncertainty reason")
        return
    if record.failure_code is not None:
        raise ValueError("in-progress native restore record contains a failure classification")


async def _await_durable(awaitable: Any, *, name: str) -> bool:
    """Finish one persistence step even when the caller is cancelled."""

    task = asyncio.create_task(awaitable, name=name)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if task.done():
                task.result()
                return cancelled


def _identifier(value: str, label: str) -> str:
    if _IDENTIFIER_RE.fullmatch(str(value)) is None:
        raise ValueError(f"invalid {label}")
    return str(value)


def _selector(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def _folder(value: str) -> str:
    normalized = _selector(value, "backup folder")
    if (
        not normalized[0].isalnum()
        or any(not (char.isalnum() or char in "_.-") for char in normalized)
        or normalized in {".", ".."}
    ):
        raise ValueError("invalid backup folder")
    return normalized


def _backup_number(value: str) -> str:
    normalized = _selector(value, "backup id")
    if not normalized.isdigit():
        raise ValueError("invalid backup id")
    return normalized


def _bounded_text(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 512:
        raise ValueError(f"invalid {label}")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("native restore timestamps must be UTC")
    return value


def _datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    _utc(parsed)
    return parsed


def _raise_deferred_cancel(cancelled: bool) -> None:
    if cancelled:
        raise asyncio.CancelledError


__all__ = (
    "AsyncNativeBackupJournalStore",
    "NativeBackupJournalError",
    "NativeBackupJournalIntegrityError",
    "NativeBackupJournalLimitError",
    "NativeBackupJournalTransitionError",
    "NativeBackupRestoreJournal",
    "NativeRestoreFact",
    "NativeRestoreFailureCode",
    "NativeRestoreIssue",
    "NativeRestoreRecord",
    "NativeRestoreState",
    "NativeRestoreTarget",
)
