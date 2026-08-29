"""Home Assistant persistence adapters for filesystem recovery state."""

from __future__ import annotations

import os
import re
import shutil
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any

from .const import DOMAIN
from .filesystem_journal import DEFAULT_MAX_RECOVERY_BLOB_BYTES

_BLOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_ENTRY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MIN_FREE_BYTES_AFTER_WRITE = 16 * 1024 * 1024


class HomeAssistantJournalMetadataStore:
    """Persist transaction metadata through Home Assistant's atomic Store."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}.filesystem_journal.{entry_id}")

    async def async_load(self) -> dict[str, Any] | None:
        payload = await self._store.async_load()
        return payload if isinstance(payload, dict) else None

    async def async_save(self, payload: Any) -> None:
        await self._store.async_save(dict(payload))

    async def async_remove(self) -> None:
        """Remove all filesystem journal metadata for this config entry."""

        await self._store.async_remove()


class HomeAssistantRecoveryBlobStore:
    """Keep bounded recovery bytes outside JSON storage with private modes."""

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        *,
        max_blob_bytes: int = DEFAULT_MAX_RECOVERY_BLOB_BYTES,
    ) -> None:
        if _ENTRY_ID.fullmatch(str(entry_id)) is None:
            raise ValueError("invalid config entry id for recovery storage")
        self._hass = hass
        self._root = Path(hass.config.path(".storage", DOMAIN, "recovery", entry_id))
        self._max_blob_bytes = max_blob_bytes

    async def async_write(self, blob_id: str, content: bytes) -> None:
        path = self._path(blob_id)
        data = bytes(content)
        if len(data) > self._max_blob_bytes:
            raise ValueError("recovery blob exceeds its configured size limit")
        await self._hass.async_add_executor_job(_write_private_blob, self._root, path, data)

    async def async_read(self, blob_id: str) -> bytes:
        return await self._hass.async_add_executor_job(
            _read_private_blob,
            self._path(blob_id),
            self._max_blob_bytes,
        )

    async def async_delete(self, blob_id: str) -> None:
        await self._hass.async_add_executor_job(_delete_private_blob, self._path(blob_id))

    async def async_list(self) -> tuple[str, ...]:
        """List only integration-owned recovery blobs for orphan reconciliation."""

        return await self._hass.async_add_executor_job(_list_private_blobs, self._root)

    async def async_remove_all(self) -> None:
        """Remove only validated recovery blobs and their now-empty entry directory."""

        await self._hass.async_add_executor_job(_remove_private_blob_root, self._root)

    def _path(self, blob_id: str) -> Path:
        if not _BLOB_ID.fullmatch(str(blob_id)):
            raise ValueError("invalid recovery blob id")
        return self._root / str(blob_id)


def _write_private_blob(root: Path, path: Path, content: bytes) -> None:
    _ensure_private_root(root)
    if shutil.disk_usage(root).free - len(content) < _MIN_FREE_BYTES_AFTER_WRITE:
        raise OSError("insufficient free space for a verified recovery blob")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    root_descriptor = _open_directory(root)
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=root_descriptor)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(root_descriptor)
        except BaseException:
            with suppress(OSError):
                os.unlink(path.name, dir_fd=root_descriptor)
            raise
    except BaseException:
        raise
    finally:
        os.close(root_descriptor)


def _read_private_blob(path: Path, max_bytes: int) -> bytes:
    root_descriptor = _open_directory(path.parent)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, dir_fd=root_descriptor)
    finally:
        os.close(root_descriptor)
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        details = os.fstat(stream.fileno())
        if not stat.S_ISREG(details.st_mode) or details.st_size < 0 or details.st_size > max_bytes:
            raise OSError("recovery blob size is outside its configured limit")
        content = stream.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise OSError("recovery blob exceeded its configured limit while reading")
    return content


def _delete_private_blob(path: Path) -> None:
    try:
        root_descriptor = _open_directory(path.parent)
    except FileNotFoundError:
        return
    try:
        try:
            os.unlink(path.name, dir_fd=root_descriptor)
        except FileNotFoundError:
            return
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)


def _list_private_blobs(root: Path) -> tuple[str, ...]:
    try:
        root_descriptor = _open_directory(root)
    except FileNotFoundError:
        return ()
    try:
        names: list[str] = []
        for name in os.listdir(root_descriptor):
            if _BLOB_ID.fullmatch(name) is None:
                continue
            details = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if stat.S_ISREG(details.st_mode):
                names.append(name)
        return tuple(sorted(names))
    finally:
        os.close(root_descriptor)


def _remove_private_blob_root(root: Path) -> None:
    """Remove an exact entry recovery directory without following links."""

    try:
        root_descriptor = _open_directory(root)
    except FileNotFoundError:
        return
    try:
        names = tuple(os.listdir(root_descriptor))
        for name in names:
            if _BLOB_ID.fullmatch(name) is None:
                raise OSError("recovery storage contains an unexpected entry")
            details = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("recovery storage contains a non-regular entry")
        for name in names:
            os.unlink(name, dir_fd=root_descriptor)
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)
    os.rmdir(root)
    with suppress(OSError):
        os.rmdir(root.parent)
    with suppress(OSError):
        os.rmdir(root.parent.parent)


def _ensure_private_root(root: Path) -> None:
    domain_root = root.parent.parent
    domain_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (domain_root, root.parent, root):
        with suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise OSError("recovery storage path is not a private directory")
    os.chmod(root, 0o700)


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
