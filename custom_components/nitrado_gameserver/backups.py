"""Typed provider-native Nitrado backup inventory and restore service.

Native backup facts are intentionally separate from filesystem snapshots and
game-health validation.  A provider restore acknowledgement never proves that
the intended world loaded or that its save data is healthy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .api.nitrado import NitradoApiError
from .native_backup_journal import (
    NativeBackupRestoreJournal,
    NativeRestoreFailureCode,
    NativeRestoreTarget,
)

MAX_NATIVE_BACKUPS = 512
MAX_BACKUP_SELECTOR_CHARS = 128
MAX_BACKUP_TEXT_CHARS = 512
MAX_BACKUP_SIZE_BYTES = (1 << 63) - 1
MAX_RESTORE_OBSERVATION_SECONDS = 600.0


class NativeBackupError(Exception):
    """Raised when native-backup facts or preconditions are unsafe."""


class NativeBackupRestoreUnconfirmedError(NativeBackupError):
    """Raised when the provider may have accepted a restore but did not confirm it."""

    provider_outcome_unknown = True


@dataclass(frozen=True, slots=True)
class NativeBackupIdentity:
    """Exact provider identity needed to address one native backup."""

    folder: str
    backup_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "folder", _folder_selector(self.folder))
        object.__setattr__(self, "backup_id", _backup_number(self.backup_id))

    @property
    def game(self) -> str:
        """Compatibility alias; Nitrado's documented selector is ``folder``."""

        return self.folder


@dataclass(frozen=True, slots=True)
class NativeBackup:
    """Sanitized facts reported by Nitrado for one provider backup."""

    identity: NativeBackupIdentity
    created_at: str | None = None
    size_bytes: int | None = None
    file_size_bytes: int | None = None
    status: str | None = None
    name: str | None = None
    backup_type: str | None = None


@dataclass(frozen=True, slots=True)
class NativeBackupCapabilities:
    """Provider operations implemented by this integration.

    Nitrado's official gameserver API documentation exposes inventory,
    ``restore_possible``, and restore endpoints. It does not document a
    gameserver backup-creation endpoint, so creation deliberately remains
    unavailable instead of guessing an API.
    """

    inventory: bool = True
    create: bool = False
    restore: bool = True


@dataclass(frozen=True, slots=True)
class NativeBackupInventory:
    """Bounded snapshot of provider-reported backup facts."""

    service_id: str
    backups: tuple[NativeBackup, ...]
    truncated: bool = False
    capabilities: NativeBackupCapabilities = NativeBackupCapabilities()


@dataclass(frozen=True, slots=True)
class NativeBackupRestoreRequest:
    """Exact restore target plus optional inventory preconditions."""

    identity: NativeBackupIdentity
    expected_size_bytes: int
    expected_created_at: str
    expected_status: str | None = None
    expected_backup_type: str | None = None
    expected_file_size_bytes: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.expected_size_bytes <= MAX_BACKUP_SIZE_BYTES:
            raise ValueError("expected backup size is outside its supported range")
        created_at = _optional_text(self.expected_created_at)
        status = _optional_text(self.expected_status)
        backup_type = _optional_text(self.expected_backup_type)
        if self.expected_file_size_bytes is not None and not (
            0 <= self.expected_file_size_bytes <= MAX_BACKUP_SIZE_BYTES
        ):
            raise ValueError("expected backup file size is outside its supported range")
        if created_at is None:
            raise ValueError("expected backup timestamp is required")
        object.__setattr__(self, "expected_created_at", created_at)
        object.__setattr__(self, "expected_status", status)
        object.__setattr__(self, "expected_backup_type", backup_type)


class NativeBackupRestoreOutcome(StrEnum):
    """Provider restore progress without pretending it proves game health."""

    ACCEPTED = "accepted"
    PROVIDER_TRANSITION_OBSERVED = "provider_transition_observed"


@dataclass(frozen=True, slots=True)
class NativeBackupRestoreResult:
    """Provider restore acknowledgement, explicitly not game-health proof."""

    service_id: str
    identity: NativeBackupIdentity
    provider_accepted: bool
    provider_returned_data: bool
    provider_restore_observed: bool = False
    operation_id: str | None = None
    game_health_verified: bool = field(default=False, init=False)

    @property
    def outcome(self) -> NativeBackupRestoreOutcome:
        """Return the strongest provider fact actually established."""

        if self.provider_restore_observed:
            return NativeBackupRestoreOutcome.PROVIDER_TRANSITION_OBSERVED
        return NativeBackupRestoreOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class NativeBackupRestoreObservation:
    """Bounded observation of Nitrado's restore transition."""

    restore_started: bool
    restore_finished: bool
    provider_status: str

    def __post_init__(self) -> None:
        status = _optional_text(self.provider_status)
        if status is None:
            raise ValueError("provider restore observation requires a status")
        object.__setattr__(self, "provider_status", status)


class NativeBackupRestoreObserver(Protocol):
    """Core-owned observer that watches provider state, not game health."""

    async def async_observe_native_restore(
        self,
        service_id: str,
        identity: NativeBackupIdentity,
    ) -> NativeBackupRestoreObservation: ...


class _NativeBackupClient(Protocol):
    async def list_native_backups(self, service_id: str) -> dict[str, Any]: ...

    async def native_backup_restore_possible(self, service_id: str) -> bool: ...

    async def restore_native_backup(
        self,
        service_id: str,
        folder: str,
        backup_id: str,
    ) -> dict[str, Any]: ...


class NitradoNativeBackupService:
    """Service-bound provider-native backup operations."""

    def __init__(
        self,
        client: _NativeBackupClient,
        service_id: str,
        *,
        account_entry_id: str | None = None,
        journal: NativeBackupRestoreJournal | None = None,
        observer: NativeBackupRestoreObserver | None = None,
        pre_restore_check: Callable[[], Awaitable[None]] | None = None,
        mutation_started: Callable[[], None] | None = None,
        observation_timeout: float = 180.0,
    ) -> None:
        self._client = client
        self._service_id = _numeric_service_id(service_id)
        self._account_entry_id = account_entry_id
        self._journal = journal
        self._observer = observer
        self._pre_restore_check = pre_restore_check
        self._mutation_started = mutation_started
        if not 0 < observation_timeout <= MAX_RESTORE_OBSERVATION_SECONDS:
            raise ValueError("native restore observation timeout is outside its bounded range")
        self._observation_timeout = observation_timeout

    @property
    def capabilities(self) -> NativeBackupCapabilities:
        """Return facts about the implemented native-backup surface."""

        return NativeBackupCapabilities()

    async def async_list(self, *, limit: int = MAX_NATIVE_BACKUPS) -> NativeBackupInventory:
        """Return a bounded, sanitized native-backup inventory."""

        if not 1 <= limit <= MAX_NATIVE_BACKUPS:
            raise ValueError(f"backup inventory limit must be between 1 and {MAX_NATIVE_BACKUPS}")
        try:
            payload = await self._client.list_native_backups(self._service_id)
        except NitradoApiError as err:
            raise NativeBackupError("Nitrado native-backup inventory is unavailable") from err
        backups = parse_native_backup_inventory(payload, limit=MAX_NATIVE_BACKUPS)
        return NativeBackupInventory(
            self._service_id,
            backups[:limit],
            truncated=len(backups) > limit,
        )

    async def async_restore(self, request: NativeBackupRestoreRequest) -> NativeBackupRestoreResult:
        """Restore an exact inventory item after rechecking its preconditions."""

        inventory = await self.async_list()
        candidate = next((item for item in inventory.backups if item.identity == request.identity), None)
        if candidate is None:
            raise NativeBackupError("The exact native backup is no longer present in provider inventory")
        if candidate.size_bytes is None or candidate.size_bytes != request.expected_size_bytes:
            raise NativeBackupError("Native backup size changed after restore was planned")
        if candidate.created_at is None or candidate.created_at != request.expected_created_at:
            raise NativeBackupError("Native backup timestamp changed after restore was planned")
        if candidate.status != request.expected_status:
            raise NativeBackupError("Native backup status changed after restore was planned")
        if candidate.backup_type != request.expected_backup_type:
            raise NativeBackupError("Native backup type changed after restore was planned")
        if candidate.file_size_bytes != request.expected_file_size_bytes:
            raise NativeBackupError("Native backup file size changed after restore was planned")

        try:
            restore_possible = await self._client.native_backup_restore_possible(self._service_id)
        except NitradoApiError as err:
            raise NativeBackupError("Nitrado native-backup restore availability could not be confirmed") from err
        if restore_possible is not True:
            raise NativeBackupError("Nitrado reports that native-backup restore is not currently possible")
        if self._journal is None or self._account_entry_id is None:
            raise NativeBackupError("Durable native-backup restore tracking is unavailable")

        record = await self._journal.async_prepare(
            NativeRestoreTarget(
                account_entry_id=self._account_entry_id,
                service_id=self._service_id,
                folder=request.identity.folder,
                backup_id=request.identity.backup_id,
                expected_size_bytes=request.expected_size_bytes,
                expected_created_at=request.expected_created_at,
                expected_status=request.expected_status,
                expected_backup_type=request.expected_backup_type,
                expected_file_size_bytes=request.expected_file_size_bytes,
            )
        )

        try:
            await self._journal.async_mark_requesting(record.operation_id)
        except asyncio.CancelledError:
            await _drain_abort_before_request(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.CANCELLED,
            )
            raise

        if self._pre_restore_check is not None:
            try:
                await self._pre_restore_check()
            except asyncio.CancelledError:
                await _drain_abort_before_request(
                    self._journal,
                    record.operation_id,
                    NativeRestoreFailureCode.CANCELLED,
                )
                raise
            except Exception as err:
                await _drain_abort_before_request(
                    self._journal,
                    record.operation_id,
                    NativeRestoreFailureCode.PRECONDITION_FAILED,
                )
                raise NativeBackupError("Native-backup restore preconditions changed before transport") from err

        if self._mutation_started is not None:
            try:
                self._mutation_started()
            except Exception:
                await _drain_abort_before_request(
                    self._journal,
                    record.operation_id,
                    NativeRestoreFailureCode.PRECONDITION_FAILED,
                )
                raise

        try:
            data = await self._client.restore_native_backup(
                self._service_id,
                request.identity.folder,
                request.identity.backup_id,
            )
        except asyncio.CancelledError:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.CANCELLED,
            )
            raise
        except NitradoApiError as err:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.TRANSPORT_UNCONFIRMED,
            )
            raise NativeBackupRestoreUnconfirmedError(
                "Nitrado did not confirm the native-backup restore outcome"
            ) from err
        except Exception as err:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.TRANSPORT_UNCONFIRMED,
            )
            raise NativeBackupRestoreUnconfirmedError(
                "Nitrado native-backup restore transport failed without a confirmed outcome"
            ) from err

        try:
            await self._journal.async_mark_accepted(record.operation_id)
        except asyncio.CancelledError:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.CANCELLED,
            )
            raise

        if self._observer is None:
            return NativeBackupRestoreResult(
                service_id=self._service_id,
                identity=request.identity,
                provider_accepted=True,
                provider_returned_data=bool(data),
                operation_id=record.operation_id,
            )

        try:
            await self._journal.async_mark_observing(record.operation_id)
        except asyncio.CancelledError:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.CANCELLED,
            )
            raise
        try:
            async with asyncio.timeout(self._observation_timeout):
                observation = await self._observer.async_observe_native_restore(
                    self._service_id,
                    request.identity,
                )
        except TimeoutError as err:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.OBSERVATION_TIMEOUT,
            )
            raise NativeBackupRestoreUnconfirmedError(
                "Nitrado accepted the restore but its provider transition was not observed in time"
            ) from err
        except asyncio.CancelledError:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.CANCELLED,
            )
            raise
        except Exception as err:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.OBSERVATION_FAILED,
            )
            raise NativeBackupRestoreUnconfirmedError(
                "Nitrado accepted the restore but provider observation failed"
            ) from err
        if not isinstance(observation, NativeBackupRestoreObservation):
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.OBSERVATION_FAILED,
            )
            raise NativeBackupRestoreUnconfirmedError("Nitrado restore observer returned an invalid result")
        if not observation.restore_started or not observation.restore_finished:
            await _drain_unknown(
                self._journal,
                record.operation_id,
                NativeRestoreFailureCode.OBSERVATION_TIMEOUT,
                provider_status=observation.provider_status,
            )
            raise NativeBackupRestoreUnconfirmedError(
                "Nitrado accepted the restore but a complete provider transition was not observed"
            )
        await self._journal.async_mark_observed(
            record.operation_id,
            provider_status=observation.provider_status,
        )
        return NativeBackupRestoreResult(
            service_id=self._service_id,
            identity=request.identity,
            provider_accepted=True,
            provider_returned_data=bool(data),
            provider_restore_observed=True,
            operation_id=record.operation_id,
        )


def parse_native_backup_inventory(
    payload: Mapping[str, Any],
    *,
    limit: int = MAX_NATIVE_BACKUPS,
) -> tuple[NativeBackup, ...]:
    """Parse documented and observed Nitrado backup inventory shapes."""

    if not isinstance(payload, Mapping):
        raise NativeBackupError("Nitrado native-backup inventory is malformed")
    if not 1 <= limit <= MAX_NATIVE_BACKUPS:
        raise ValueError(f"backup inventory limit must be between 1 and {MAX_NATIVE_BACKUPS}")

    if "backups" not in payload:
        raise NativeBackupError("Nitrado native-backup inventory is missing its backups container")
    root = payload.get("backups")
    if not isinstance(root, (list, Mapping)):
        raise NativeBackupError("Nitrado native-backup inventory has an invalid backups container")
    parsed: list[NativeBackup] = []
    visited = 0

    def walk(value: Any, inherited_folder: str | None = None) -> None:
        nonlocal visited
        visited += 1
        if visited > MAX_NATIVE_BACKUPS * 8:
            raise NativeBackupError("Nitrado native-backup inventory exceeded its structural limit")
        if len(parsed) >= limit:
            return
        if isinstance(value, list):
            for item in value:
                walk(item, inherited_folder)
            return
        if not isinstance(value, Mapping):
            return

        backup_id = _backup_id(value)
        if backup_id is not None:
            folder = _first(value, "folder", "game", "game_short") or inherited_folder
            if folder is None:
                raise NativeBackupError("Nitrado native-backup entry is missing its folder")
            try:
                identity = NativeBackupIdentity(str(folder), str(backup_id))
            except (TypeError, ValueError):
                raise NativeBackupError("Nitrado native-backup entry has an invalid identity") from None
            parsed.append(
                NativeBackup(
                    identity=identity,
                    created_at=_optional_text(
                        _first(
                            value,
                            "backup_timestamp",
                            "created_at",
                            "created",
                            "timestamp",
                            "date",
                            "time",
                        )
                    ),
                    size_bytes=_optional_nonnegative_int(
                        _first(value, "backup_size", "size_bytes", "size", "filesize")
                    ),
                    file_size_bytes=_optional_nonnegative_int(_first(value, "backup_file_size", "file_size_bytes")),
                    status=_optional_text(_first(value, "status", "state")),
                    name=_optional_text(_first(value, "name", "label")),
                    backup_type=_optional_text(_first(value, "backup_type", "type")),
                )
            )
            return

        if any(
            key in value
            for key in (
                "backup_number",
                "backup_id",
                "backup_timestamp",
                "backup_size",
                "backup_file_size",
                "backup_type",
            )
        ):
            raise NativeBackupError("Nitrado native-backup entry is incomplete")

        declared_folder = _first(value, "folder", "game", "game_short")
        container_folder = (
            str(declared_folder)
            if isinstance(declared_folder, (str, int)) and not isinstance(declared_folder, bool)
            else inherited_folder
        )
        for key, item in value.items():
            folder = container_folder
            if isinstance(key, str) and key not in {
                "backup",
                "backups",
                "data",
                "gameserver",
                "master",
            }:
                folder = key
            walk(item, folder)

    walk(root)
    unique: dict[NativeBackupIdentity, NativeBackup] = {}
    for item in parsed:
        previous = unique.setdefault(item.identity, item)
        if previous != item:
            raise NativeBackupError("Nitrado native-backup inventory contains conflicting duplicate identities")
    return tuple(unique.values())


def _backup_id(value: Mapping[str, Any]) -> str | int | None:
    candidate = _first(value, "backup_number", "backup_id", "backup", "id")
    if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
        return candidate
    return None


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None and candidate != "":
            return candidate
    return None


def _selector(value: Any, label: str) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"backup {label} is invalid")
    normalized = str(value).strip()
    if not normalized or len(normalized) > MAX_BACKUP_SELECTOR_CHARS:
        raise ValueError(f"backup {label} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"backup {label} is invalid")
    return normalized


def _folder_selector(value: Any) -> str:
    normalized = _selector(value, "folder")
    if (
        not normalized[0].isalnum()
        or any(not (char.isalnum() or char in "_.-") for char in normalized)
        or normalized in {".", ".."}
    ):
        raise ValueError("backup folder is invalid")
    return normalized


def _backup_number(value: Any) -> str:
    normalized = _selector(value, "backup ID")
    if not normalized.isdigit():
        raise ValueError("backup ID is invalid")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > MAX_BACKUP_TEXT_CHARS:
        raise NativeBackupError("Nitrado native-backup text field exceeded its safety limit")
    return normalized


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise NativeBackupError("Nitrado native-backup size field is malformed") from None
    if not 0 <= parsed <= MAX_BACKUP_SIZE_BYTES:
        raise NativeBackupError("Nitrado native-backup size field is outside its supported range")
    return parsed


def _numeric_service_id(value: Any) -> str:
    normalized = str(value).strip()
    if not normalized.isdigit():
        raise ValueError("Nitrado service ID must contain only digits")
    return normalized


async def _drain_unknown(
    journal: NativeBackupRestoreJournal,
    operation_id: str,
    failure_code: NativeRestoreFailureCode,
    *,
    provider_status: str | None = None,
) -> None:
    """Persist an ambiguous outcome even when the caller is being cancelled."""

    task = asyncio.create_task(
        journal.async_mark_outcome_unknown(
            operation_id,
            failure_code,
            provider_status=provider_status,
        ),
        name="nitrado-native-restore-mark-unknown",
    )
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if task.done():
                task.result()
                return


async def _drain_abort_before_request(
    journal: NativeBackupRestoreJournal,
    operation_id: str,
    failure_code: NativeRestoreFailureCode,
) -> None:
    """Persist proof that transport was never invoked despite cancellation."""

    task = asyncio.create_task(
        journal.async_abort_before_request(operation_id, failure_code),
        name="nitrado-native-restore-abort-before-request",
    )
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if task.done():
                task.result()
                return


__all__ = (
    "MAX_NATIVE_BACKUPS",
    "NativeBackup",
    "NativeBackupCapabilities",
    "NativeBackupError",
    "NativeBackupIdentity",
    "NativeBackupInventory",
    "NativeBackupRestoreObservation",
    "NativeBackupRestoreObserver",
    "NativeBackupRestoreOutcome",
    "NativeBackupRestoreRequest",
    "NativeBackupRestoreResult",
    "NativeBackupRestoreUnconfirmedError",
    "NitradoNativeBackupService",
    "parse_native_backup_inventory",
)
