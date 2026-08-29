"""Durable arbitration for every provider-mutating operation.

The filesystem and native-backup journals retain the recovery material needed
by their own transports.  This journal sits above them: it establishes one
write-before-dispatch reservation boundary for all mutation entry points and
retains enough non-secret intent to explain an operation after a crash.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from .const import DOMAIN

SCHEMA_VERSION = 1
MAX_RECORDS = 256
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_NONTERMINAL_PHASES = frozenset({"reserved", "dispatched", "verifying", "unknown"})


class OperationJournalError(RuntimeError):
    """Raised when durable operation truth cannot safely authorize a write."""


class OperationPhase(StrEnum):
    """Persisted lifecycle of one mutation."""

    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    VERIFYING = "verifying"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    """Exact account/service identity; service IDs alone are not unique."""

    account_entry_id: str
    service_id: str

    def __post_init__(self) -> None:
        _validate_key(self.account_entry_id, "account entry ID")
        _validate_key(self.service_id, "service ID")


@dataclass(frozen=True, slots=True)
class OperationIntent:
    """Bounded, sanitized intent retained without payloads or credentials."""

    kind: str
    target_key: str
    scopes: tuple[str, ...] = ("service",)
    generation: str | None = None
    expected_evidence: str = "provider acknowledgement"

    def __post_init__(self) -> None:
        _validate_key(self.kind, "operation kind")
        _validate_key(self.target_key, "operation target")
        if not self.scopes or len(self.scopes) > 8:
            raise ValueError("operation intent must contain between one and eight scopes")
        for scope in self.scopes:
            _validate_key(scope, "operation scope")
        if self.generation is not None:
            _validate_key(self.generation, "operation generation")
        if not isinstance(self.expected_evidence, str) or not (1 <= len(self.expected_evidence) <= 160):
            raise ValueError("expected evidence must be between 1 and 160 characters")
        if any(ord(char) < 0x20 for char in self.expected_evidence):
            raise ValueError("expected evidence contains control characters")


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """One persisted operation fact."""

    operation_id: str
    identity: OperationIdentity
    intent: OperationIntent
    phase: OperationPhase
    created_at: int
    updated_at: int
    result_code: str | None = None

    def as_storage(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "account_entry_id": self.identity.account_entry_id,
            "service_id": self.identity.service_id,
            "kind": self.intent.kind,
            "target_key": self.intent.target_key,
            "scopes": list(self.intent.scopes),
            "generation": self.intent.generation,
            "expected_evidence": self.intent.expected_evidence,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result_code": self.result_code,
        }

    @classmethod
    def from_storage(cls, value: Any) -> OperationRecord:
        if not isinstance(value, dict):
            raise ValueError("operation record is malformed")
        operation_id = str(value["operation_id"])
        _validate_key(operation_id, "operation ID")
        result_code = value.get("result_code")
        if result_code is not None:
            result_code = str(result_code)
            _validate_key(result_code, "operation result code")
        return cls(
            operation_id=operation_id,
            identity=OperationIdentity(str(value["account_entry_id"]), str(value["service_id"])),
            intent=OperationIntent(
                str(value["kind"]),
                str(value["target_key"]),
                tuple(str(item) for item in value["scopes"]),
                str(value["generation"]) if value.get("generation") is not None else None,
                str(value["expected_evidence"]),
            ),
            phase=OperationPhase(str(value["phase"])),
            created_at=_valid_timestamp(value["created_at"]),
            updated_at=_valid_timestamp(value["updated_at"]),
            result_code=result_code,
        )


class OperationJournalStore(Protocol):
    """Minimal atomic metadata store."""

    async def async_load(self) -> Any: ...

    async def async_save(self, payload: dict[str, Any]) -> None: ...


class MemoryOperationJournalStore:
    """Test/standalone store; production binds a Home Assistant Store."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload

    async def async_load(self) -> dict[str, Any] | None:
        return self.payload

    async def async_save(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)


class HomeAssistantOperationJournalStore:
    """Atomic HA Store adapter namespaced by account entry."""

    def __init__(self, hass: Any, account_entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        _validate_key(str(account_entry_id), "account entry ID")
        self._store = Store(hass, SCHEMA_VERSION, f"{DOMAIN}.operation_journal.{account_entry_id}")

    async def async_load(self) -> Any:
        payload = await self._store.async_load()
        return payload

    async def async_save(self, payload: dict[str, Any]) -> None:
        await self._store.async_save(dict(payload))

    async def async_remove(self) -> None:
        """Remove this account's operation journal."""

        await self._store.async_remove()


class OperationJournal:
    """Crash-safe operation truth and conflict arbiter."""

    def __init__(self, store: OperationJournalStore, *, now_fn: Any | None = None) -> None:
        self._store = store
        self._now_fn = now_fn or (lambda: int(time.time()))
        self._records: dict[str, OperationRecord] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self.storage_error: str | None = None

    async def async_initialize(self) -> None:
        """Load state and convert every interrupted operation to unknown."""

        async with self._lock:
            if self._initialized:
                return
            try:
                payload = await self._store.async_load()
                if payload is None:
                    loaded: tuple[OperationRecord, ...] = ()
                else:
                    if not isinstance(payload, dict):
                        raise ValueError("operation journal payload is malformed")
                    if payload.get("schema_version") != SCHEMA_VERSION:
                        raise ValueError("unsupported operation journal schema")
                    raw = payload.get("records")
                    if not isinstance(raw, list) or len(raw) > MAX_RECORDS:
                        raise ValueError("invalid operation journal record collection")
                    loaded = tuple(OperationRecord.from_storage(item) for item in raw)
                    if len({item.operation_id for item in loaded}) != len(loaded):
                        raise ValueError("duplicate operation IDs")
                self._records = {item.operation_id: item for item in loaded}
                now = self._now()
                changed = False
                for operation_id, record in tuple(self._records.items()):
                    if record.phase in {
                        OperationPhase.RESERVED,
                        OperationPhase.DISPATCHED,
                        OperationPhase.VERIFYING,
                    }:
                        self._records[operation_id] = replace(
                            record,
                            phase=OperationPhase.UNKNOWN,
                            updated_at=now,
                            result_code="interrupted",
                        )
                        changed = True
                if changed:
                    await _cancellation_shielded(self._async_save_locked())
            except BaseException as err:
                if isinstance(err, asyncio.CancelledError):
                    raise
                self._records = {}
                self.storage_error = "Durable operation state is invalid or unavailable; all mutations are blocked."
            else:
                self.storage_error = None
            self._initialized = True

    async def async_reserve(
        self,
        identity: OperationIdentity,
        intent: OperationIntent,
    ) -> OperationReservation:
        """Persist a conflict-checked reservation before any dispatch."""

        async with self._lock:
            self._require_available()
            for record in self._records.values():
                if (
                    record.identity == identity
                    and record.phase
                    in {
                        OperationPhase.RESERVED,
                        OperationPhase.DISPATCHED,
                        OperationPhase.VERIFYING,
                        OperationPhase.UNKNOWN,
                    }
                    and _scopes_conflict(record.intent.scopes, intent.scopes)
                ):
                    raise OperationJournalError(
                        f"Operation {record.operation_id} is {record.phase.value}; resolve it before retrying."
                    )
            self._prune_terminal_locked()
            if len(self._records) >= MAX_RECORDS:
                raise OperationJournalError("Operation journal capacity is exhausted by unresolved records")
            now = self._now()
            operation_id = f"op-{secrets.token_hex(12)}"
            record = OperationRecord(
                operation_id,
                identity,
                intent,
                OperationPhase.RESERVED,
                now,
                now,
            )
            self._records[operation_id] = record
            try:
                await _cancellation_shielded(self._async_save_locked())
            except asyncio.CancelledError:
                # No caller received a token and no transport could have run.
                # Persist that safe fact rather than stranding a reservation.
                self._records[operation_id] = replace(
                    record,
                    phase=OperationPhase.TERMINAL,
                    updated_at=self._now(),
                    result_code="cancelled_before_dispatch",
                )
                try:
                    await _cancellation_shielded(self._async_save_locked())
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - any Store failure must fail all writes closed
                    self.storage_error = "Durable operation state could not be saved; all mutations are blocked."
                raise
            except BaseException:
                self._records.pop(operation_id, None)
                self.storage_error = "Durable operation state could not be saved; all mutations are blocked."
                raise
            return OperationReservation(self, record, asyncio.current_task())

    async def async_records(self, identity: OperationIdentity | None = None) -> tuple[OperationRecord, ...]:
        async with self._lock:
            self._require_available()
            records = self._records.values()
            if identity is not None:
                records = (item for item in records if item.identity == identity)
            return tuple(sorted(records, key=lambda item: (item.created_at, item.operation_id)))

    async def async_acknowledge_unknown(self, identity: OperationIdentity, operation_id: str) -> None:
        """Record an administrator acknowledgement without claiming success."""

        async with self._lock:
            record = self._require_record(operation_id, identity)
            if record.phase is not OperationPhase.UNKNOWN:
                raise OperationJournalError("Only an unknown operation can be acknowledged")
            await self._async_transition_locked(record, OperationPhase.TERMINAL, "acknowledged_unknown")

    async def async_reconcile(
        self,
        identity: OperationIdentity,
        operation_id: str,
        result_code: str,
    ) -> OperationRecord:
        """Resolve verifying/unknown truth only after operation-specific evidence."""

        async with self._lock:
            self._require_available()
            record = self._require_record(operation_id, identity)
            if record.phase not in {OperationPhase.VERIFYING, OperationPhase.UNKNOWN}:
                raise OperationJournalError("Only a verifying or unknown operation can be reconciled")
            return await self._async_transition_locked(record, OperationPhase.TERMINAL, result_code)

    async def _async_transition(
        self,
        operation_id: str,
        identity: OperationIdentity,
        phase: OperationPhase,
        result_code: str | None = None,
    ) -> OperationRecord:
        async with self._lock:
            self._require_available()
            record = self._require_record(operation_id, identity)
            return await self._async_transition_locked(record, phase, result_code)

    async def _async_transition_locked(
        self,
        record: OperationRecord,
        phase: OperationPhase,
        result_code: str | None,
    ) -> OperationRecord:
        allowed = {
            OperationPhase.RESERVED: {OperationPhase.DISPATCHED, OperationPhase.TERMINAL},
            OperationPhase.DISPATCHED: {OperationPhase.VERIFYING, OperationPhase.TERMINAL, OperationPhase.UNKNOWN},
            OperationPhase.VERIFYING: {OperationPhase.TERMINAL, OperationPhase.UNKNOWN},
            OperationPhase.UNKNOWN: {OperationPhase.TERMINAL},
            OperationPhase.TERMINAL: set(),
        }
        if phase not in allowed[record.phase]:
            raise OperationJournalError(f"Invalid operation transition {record.phase.value} -> {phase.value}")
        if result_code is not None:
            _validate_key(result_code, "operation result code")
        updated = replace(record, phase=phase, updated_at=self._now(), result_code=result_code)
        self._records[record.operation_id] = updated
        try:
            await _cancellation_shielded(self._async_save_locked())
        except asyncio.CancelledError:
            # The shield waits for the atomic Store write before propagating
            # cancellation. Keep memory aligned with the now-durable phase.
            raise
        except BaseException:
            self._records[record.operation_id] = record
            self.storage_error = "Durable operation state could not be saved; all mutations are blocked."
            raise
        return updated

    def _require_record(self, operation_id: str, identity: OperationIdentity) -> OperationRecord:
        record = self._records.get(str(operation_id))
        if record is None or record.identity != identity:
            raise OperationJournalError("Operation token is stale or bound to a different service")
        return record

    def _require_available(self) -> None:
        if not self._initialized:
            raise OperationJournalError("Operation journal is not initialized")
        if self.storage_error is not None:
            raise OperationJournalError(self.storage_error)

    def _prune_terminal_locked(self) -> None:
        if len(self._records) < MAX_RECORDS:
            return
        terminal = sorted(
            (item for item in self._records.values() if item.phase is OperationPhase.TERMINAL),
            key=lambda item: (item.updated_at, item.operation_id),
        )
        while terminal and len(self._records) >= MAX_RECORDS:
            self._records.pop(terminal.pop(0).operation_id, None)

    async def _async_save_locked(self) -> None:
        records = sorted(self._records.values(), key=lambda item: (item.created_at, item.operation_id))
        await self._store.async_save(
            {"schema_version": SCHEMA_VERSION, "records": [item.as_storage() for item in records]}
        )

    def _now(self) -> int:
        return _valid_timestamp(self._now_fn())


class OperationReservation:
    """Task-bound token proving that authoritative arbitration was acquired."""

    def __init__(
        self,
        journal: OperationJournal,
        record: OperationRecord,
        owner_task: asyncio.Task[Any] | None,
    ) -> None:
        self._journal = journal
        self._record = record
        self._owner_task = owner_task

    @property
    def operation_id(self) -> str:
        return self._record.operation_id

    @property
    def identity(self) -> OperationIdentity:
        return self._record.identity

    @property
    def phase(self) -> OperationPhase:
        return self._record.phase

    def assert_owner(self, identity: OperationIdentity) -> None:
        if identity != self.identity or asyncio.current_task() is not self._owner_task:
            raise OperationJournalError("Nested operation token is bound to another task or service")

    async def async_mark_dispatched(self) -> None:
        self.assert_owner(self.identity)
        self._record = await self._journal._async_transition(
            self.operation_id, self.identity, OperationPhase.DISPATCHED
        )

    async def async_mark_verifying(self) -> None:
        self.assert_owner(self.identity)
        if self.phase is OperationPhase.DISPATCHED:
            self._record = await self._journal._async_transition(
                self.operation_id, self.identity, OperationPhase.VERIFYING
            )

    async def async_complete(self, result_code: str = "succeeded") -> None:
        self.assert_owner(self.identity)
        if self.phase in {OperationPhase.DISPATCHED, OperationPhase.VERIFYING, OperationPhase.RESERVED}:
            self._record = await self._journal._async_transition(
                self.operation_id, self.identity, OperationPhase.TERMINAL, result_code
            )

    async def async_fail(self, *, cancelled: bool = False) -> None:
        """Fail safely before dispatch; otherwise retain deliberately unknown truth."""

        self.assert_owner(self.identity)
        if self.phase is OperationPhase.RESERVED:
            self._record = await self._journal._async_transition(
                self.operation_id,
                self.identity,
                OperationPhase.TERMINAL,
                "cancelled_before_dispatch" if cancelled else "failed_before_dispatch",
            )
        elif self.phase in {OperationPhase.DISPATCHED, OperationPhase.VERIFYING}:
            self._record = await self._journal._async_transition(
                self.operation_id,
                self.identity,
                OperationPhase.UNKNOWN,
                "cancelled_after_dispatch" if cancelled else "failed_after_dispatch",
            )


def _scopes_conflict(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return "service" in left or "service" in right or bool(set(left) & set(right))


def _validate_key(value: str, label: str) -> None:
    if not isinstance(value, str) or _SAFE_KEY.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")


def _valid_timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid operation timestamp")
    parsed = int(value)
    if parsed < 0:
        raise ValueError("invalid operation timestamp")
    return parsed


async def _cancellation_shielded(awaitable: Any) -> Any:
    """Do not let task cancellation tear an atomic truth write in half."""

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise
