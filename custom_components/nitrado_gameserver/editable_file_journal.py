"""Durable recovery references for profile-owned editable files."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from .const import DOMAIN

SCHEMA_VERSION = 1
MIN_RETENTION_SECONDS = 90 * 24 * 60 * 60
MIN_RECENT_PER_FILE = 10
MAX_STORED_RECORDS = 100_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EditableFileJournalError(RuntimeError):
    """Raised when durable editable-file recovery truth is unavailable."""


@dataclass(frozen=True, slots=True)
class EditableFileRecoveryRecord:
    """Secret-free reference to one verified remote recovery file."""

    recovery_id: str
    account_entry_id: str
    service_id: str
    file_key: str
    declared_path: str
    backup_path: str
    backup_kind: str
    source_revision: str
    resulting_revision: str
    operation_id: str
    created_at: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.recovery_id, "recovery ID"),
            (self.account_entry_id, "account entry ID"),
            (self.service_id, "service ID"),
            (self.file_key, "file key"),
            (self.operation_id, "operation ID"),
        ):
            if _SAFE_ID.fullmatch(str(value)) is None:
                raise ValueError(f"invalid {label}")
        if self.backup_kind not in {"apply", "pre_rollback"}:
            raise ValueError("invalid editable-file backup kind")
        if (
            not _valid_remote_path(self.declared_path)
            or not _valid_remote_path(self.backup_path)
            or not self.backup_path.startswith(f"{self.declared_path}.nitrado_gameserver.")
        ):
            raise ValueError("invalid editable-file recovery path binding")
        if _SHA256.fullmatch(self.source_revision) is None or _SHA256.fullmatch(self.resulting_revision) is None:
            raise ValueError("invalid editable-file content revision")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, int) or self.created_at < 0:
            raise ValueError("invalid editable-file recovery timestamp")

    def as_storage(self) -> dict[str, Any]:
        return {
            "recovery_id": self.recovery_id,
            "account_entry_id": self.account_entry_id,
            "service_id": self.service_id,
            "file_key": self.file_key,
            "declared_path": self.declared_path,
            "backup_path": self.backup_path,
            "backup_kind": self.backup_kind,
            "source_revision": self.source_revision,
            "resulting_revision": self.resulting_revision,
            "operation_id": self.operation_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_storage(cls, value: Any) -> EditableFileRecoveryRecord:
        if not isinstance(value, dict):
            raise ValueError("editable-file recovery record is malformed")
        return cls(
            recovery_id=str(value["recovery_id"]),
            account_entry_id=str(value["account_entry_id"]),
            service_id=str(value["service_id"]),
            file_key=str(value["file_key"]),
            declared_path=str(value["declared_path"]),
            backup_path=str(value["backup_path"]),
            backup_kind=str(value["backup_kind"]),
            source_revision=str(value["source_revision"]),
            resulting_revision=str(value["resulting_revision"]),
            operation_id=str(value["operation_id"]),
            created_at=int(value["created_at"]),
        )


class EditableFileJournalStore(Protocol):
    async def async_load(self) -> Any: ...

    async def async_save(self, payload: dict[str, Any]) -> None: ...


class MemoryEditableFileJournalStore:
    """In-memory store for isolated tests and standalone coordinators."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload

    async def async_load(self) -> Any:
        return self.payload

    async def async_save(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)


class HomeAssistantEditableFileJournalStore:
    """Atomic HA Store adapter namespaced by account entry."""

    def __init__(self, hass: Any, account_entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        if _SAFE_ID.fullmatch(str(account_entry_id)) is None:
            raise ValueError("invalid account entry ID")
        self._store = Store(hass, SCHEMA_VERSION, f"{DOMAIN}.editable_file_history.{account_entry_id}")

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_save(self, payload: dict[str, Any]) -> None:
        await self._store.async_save(dict(payload))

    async def async_remove(self) -> None:
        """Remove this account's editable-file recovery history."""

        await self._store.async_remove()


class EditableFileJournal:
    """Persist and prune recovery metadata without deleting remote backups."""

    def __init__(self, store: EditableFileJournalStore, *, now_fn: Any | None = None) -> None:
        self._store = store
        self._now_fn = now_fn or (lambda: int(time.time()))
        self._records: dict[str, EditableFileRecoveryRecord] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self.storage_error: str | None = None

    async def async_initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            try:
                payload = await self._store.async_load()
                raw = [] if payload is None else payload.get("records") if isinstance(payload, dict) else None
                if raw is None or not isinstance(raw, list) or len(raw) > MAX_STORED_RECORDS:
                    raise ValueError("invalid editable-file recovery collection")
                if payload is not None and payload.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("unsupported editable-file recovery schema")
                records = tuple(EditableFileRecoveryRecord.from_storage(item) for item in raw)
                if len({record.recovery_id for record in records}) != len(records):
                    raise ValueError("duplicate editable-file recovery IDs")
                self._records = {record.recovery_id: record for record in records}
                self._initialized = True
                self.storage_error = None
            except Exception as err:
                self.storage_error = "Durable editable-file recovery history is unavailable"
                raise EditableFileJournalError(self.storage_error) from err

    async def async_record(
        self,
        *,
        account_entry_id: str,
        service_id: str,
        file_key: str,
        declared_path: str,
        backup_path: str,
        backup_kind: str,
        source_revision: str,
        resulting_revision: str,
        operation_id: str,
        created_at: int | None = None,
    ) -> EditableFileRecoveryRecord:
        await self.async_initialize()
        async with self._lock:
            record = EditableFileRecoveryRecord(
                recovery_id=f"recovery-{secrets.token_hex(12)}",
                account_entry_id=str(account_entry_id),
                service_id=str(service_id),
                file_key=str(file_key),
                declared_path=str(declared_path),
                backup_path=str(backup_path),
                backup_kind=str(backup_kind),
                source_revision=str(source_revision),
                resulting_revision=str(resulting_revision),
                operation_id=str(operation_id),
                created_at=self._now() if created_at is None else int(created_at),
            )
            self._records[record.recovery_id] = record
            self._prune_metadata()
            await self._save_locked()
            return record

    async def async_records(self, service_id: str, file_key: str) -> tuple[EditableFileRecoveryRecord, ...]:
        await self.async_initialize()
        return tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if record.service_id == str(service_id) and record.file_key == str(file_key)
                ),
                key=lambda record: (record.created_at, record.recovery_id),
                reverse=True,
            )
        )

    async def async_require(
        self,
        service_id: str,
        file_key: str,
        recovery_id: str,
    ) -> EditableFileRecoveryRecord:
        await self.async_initialize()
        record = self._records.get(str(recovery_id))
        if record is None or record.service_id != str(service_id) or record.file_key != str(file_key):
            raise EditableFileJournalError("The selected editable-file recovery record is unavailable")
        return record

    def _prune_metadata(self) -> None:
        cutoff = self._now() - MIN_RETENTION_SECONDS
        groups: dict[tuple[str, str], list[EditableFileRecoveryRecord]] = {}
        for record in self._records.values():
            groups.setdefault((record.service_id, record.file_key), []).append(record)
        keep: set[str] = set()
        for records in groups.values():
            ordered = sorted(records, key=lambda item: (item.created_at, item.recovery_id), reverse=True)
            for index, record in enumerate(ordered):
                if index < MIN_RECENT_PER_FILE or record.created_at >= cutoff:
                    keep.add(record.recovery_id)
        self._records = {key: value for key, value in self._records.items() if key in keep}

    async def _save_locked(self) -> None:
        try:
            await self._store.async_save(
                {
                    "schema_version": SCHEMA_VERSION,
                    "records": [
                        record.as_storage()
                        for record in sorted(
                            self._records.values(), key=lambda item: (item.created_at, item.recovery_id)
                        )
                    ],
                }
            )
            self.storage_error = None
        except Exception as err:
            self.storage_error = "Durable editable-file recovery history could not be saved"
            raise EditableFileJournalError(self.storage_error) from err

    def _now(self) -> int:
        value = self._now_fn()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise EditableFileJournalError("Editable-file recovery clock is invalid")
        return value


def _valid_remote_path(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return all(part not in {"", ".", ".."} for part in path.parts if part != "/")


__all__ = (
    "EditableFileJournal",
    "EditableFileJournalError",
    "EditableFileRecoveryRecord",
    "HomeAssistantEditableFileJournalStore",
    "MemoryEditableFileJournalStore",
)
