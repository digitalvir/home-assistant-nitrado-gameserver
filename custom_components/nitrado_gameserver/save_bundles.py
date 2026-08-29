"""Portable, bounded, profile-declared save-game bundles."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from .const import STOPPED_STATUS
from .extensions import ProfileExtensionError, profile_action_context
from .filesystem import (
    DEFAULT_MAX_BINARY_BYTES,
    DEFAULT_MAX_DIRECTORY_ENTRIES,
    DEFAULT_MAX_PATH_BYTES,
    DEFAULT_MAX_TREE_BYTES,
    DEFAULT_MAX_TREE_DEPTH,
    DEFAULT_MAX_TREE_FILES,
    FileManifestEntry,
    FileTransportSecurityError,
    FileTreeManifest,
    FileTreeSnapshot,
    RemotePath,
    SpoolTreeFiles,
    canonical_relative_path,
    tree_manifest_sha256,
)
from .plugins.base import (
    GameProfile,
    ProfileManifestError,
    SaveBundleDeclaration,
    async_invoke_profile,
    blocked,
)
from .plugins.registry import profile_registry_generation
from .profile_logging import log_profile_failure
from .runtime import ServiceRuntime, runtime_profile_extension_manifest

_LOGGER = logging.getLogger(__name__)
_SAVE_BUNDLE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nitrado-save-bundle")


class _SaveBundleWorkerCancelled(RuntimeError):
    """Internal cooperative cancellation signal for ZIP workers."""


def _check_worker_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _SaveBundleWorkerCancelled("Save-bundle ZIP work was cancelled")


async def _async_run_zip_worker(callback: Callable[..., Any], *args: Any) -> Any:
    """Run bounded ZIP work and retain ownership until real worker completion."""

    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()
    future = loop.run_in_executor(_SAVE_BUNDLE_EXECUTOR, callback, *args, cancel_event)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancelled:
        cancel_event.set()
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancel_event.set()
        with suppress(Exception, asyncio.CancelledError):
            future.result()
        raise cancelled


SAVE_BUNDLE_SCHEMA = "nitrado_gameserver.save_bundle.v1"
SAVE_BUNDLE_MANIFEST = "_nitrado_save_bundle.json"
MAX_SAVE_BUNDLE_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_SAVE_BUNDLE_FILE_BYTES = DEFAULT_MAX_BINARY_BYTES
# JSON ASCII escaping can expand a valid UTF-8 path to roughly three times its
# byte length.  Derive the limit so every legal 5,000-path export can re-open.
MAX_SAVE_BUNDLE_MANIFEST_BYTES = DEFAULT_MAX_TREE_FILES * (DEFAULT_MAX_PATH_BYTES * 3 + 256)
MAX_SAVE_BUNDLE_EXPANDED_BYTES = DEFAULT_MAX_TREE_BYTES + MAX_SAVE_BUNDLE_MANIFEST_BYTES
MAX_ZIP_CENTRAL_DIRECTORY_BYTES = DEFAULT_MAX_DIRECTORY_ENTRIES * (DEFAULT_MAX_PATH_BYTES + 256)
MAX_ZIP_COMPRESSION_RATIO = 1000
_ZIP_CHUNK_BYTES = 1024 * 1024
_IGNORED_ARCHIVE_PARTS = frozenset({"__MACOSX"})
_IGNORED_ARCHIVE_NAMES = frozenset({".DS_Store", "Thumbs.db"})

SaveBundleProgress = Callable[[str, str, Mapping[str, Any]], None]


@dataclass(slots=True)
class SaveBundleExport:
    """One generated archive backed by a closeable spool."""

    filename: str
    stream: Any
    manifest: FileTreeManifest

    def close(self) -> None:
        self.stream.close()


@dataclass(slots=True)
class SaveBundleUpload:
    """Validated uploaded save overlay backed by a closeable spool."""

    files: SpoolTreeFiles
    manifest: FileTreeManifest
    source_prefix: str
    portable_manifest: bool

    def close(self) -> None:
        self.files.close()


@dataclass(slots=True)
class SaveBundlePreview:
    """Exact stopped-server review for one uploaded overlay."""

    key: str
    name: str
    target_root: str
    world_id: str
    excluded_paths: tuple[str, ...]
    current: FileTreeManifest
    proposed: FileTreeManifest
    upload: SaveBundleUpload
    added: tuple[FileManifestEntry, ...]
    replaced: tuple[FileManifestEntry, ...]
    unchanged: tuple[FileManifestEntry, ...]
    preserved: tuple[FileManifestEntry, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.replaced)

    def close(self) -> None:
        self.upload.close()


@dataclass(slots=True, frozen=True)
class SaveBundleApplyResult:
    """Verified tree-replacement result exposed without save contents."""

    transaction_id: str
    operation_id: str
    manifest: FileTreeManifest


class OverlayTreeFiles(Mapping[str, bytes]):
    """Present an uploaded overlay on top of an exact live snapshot."""

    def __init__(self, base: Mapping[str, bytes], overlay: Mapping[str, bytes]) -> None:
        self._base = base
        self._overlay = overlay
        self._paths = tuple(sorted(set(base) | set(overlay)))

    def __getitem__(self, path: str) -> bytes:
        return self._overlay[path] if path in self._overlay else self._base[path]

    def __iter__(self):
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)


async def async_export_save_bundle(
    runtime: ServiceRuntime,
    client: Any,
    bundle_key: str,
    *,
    now: int | None = None,
    progress: SaveBundleProgress | None = None,
) -> SaveBundleExport:
    """Snapshot and archive one exact profile-declared save tree."""

    _report_progress(progress, "locating", "Locating the active save tree…")
    profile, generation = _profile_snapshot(runtime)
    declaration = await _save_bundle_declaration(runtime, bundle_key)
    _require_state(runtime, declaration)
    root = await _resolve_root(runtime, client, declaration, now=now)
    _report_progress(progress, "connecting", "Opening a verified FTPS snapshot of the active save…")
    _require_profile_snapshot(runtime, profile, generation)

    def snapshot_progress(event: str, details: Mapping[str, Any]) -> None:
        files = int(details.get("files", 0))
        size = int(details.get("bytes", 0))
        common = {
            "files_done": files,
            "bytes_done": size,
            "directories_done": int(details.get("directories", 0)),
        }
        if event == "path_excluded":
            _report_progress(
                progress,
                "excluding_history",
                "Skipping profile-excluded save history — only editor-facing files belong in this ZIP.",
                **common,
            )
        elif event == "file_started" or event == "file_downloaded":
            _report_progress(
                progress,
                "downloading",
                f"Downloading live save files — {files} complete, {_format_progress_bytes(size)} read…",
                current_file=str(details.get("path", "")),
                **common,
            )
        elif event == "directory_scanned":
            _report_progress(
                progress,
                "scanning",
                f"Scanning the active save tree — {common['directories_done']} folder(s) checked…",
                **common,
            )

    service_id = runtime.state.identity.service_id
    async with client.authoritative_read(service_id):
        snapshot = await client.snapshot_tree(
            service_id,
            root,
            max_files=DEFAULT_MAX_TREE_FILES,
            max_bytes=DEFAULT_MAX_TREE_BYTES,
            max_depth=DEFAULT_MAX_TREE_DEPTH,
            excluded_paths=declaration.excluded_paths,
            progress=snapshot_progress,
        )
    try:
        _report_progress(
            progress,
            "validating",
            f"Validating {len(snapshot.manifest.entries)} live save file(s)…",
            files_done=len(snapshot.manifest.entries),
            files_total=len(snapshot.manifest.entries),
            bytes_done=snapshot.manifest.total_bytes,
            bytes_total=snapshot.manifest.total_bytes,
        )
        _require_profile_snapshot(runtime, profile, generation)
        _validate_required_files(declaration, snapshot.manifest)
        stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")  # noqa: SIM115
        try:
            loop = asyncio.get_running_loop()

            def zip_progress(stage: str, message: str, details: Mapping[str, Any]) -> None:
                if progress is not None:
                    loop.call_soon_threadsafe(progress, stage, message, details)

            await _async_run_zip_worker(
                _write_export_zip,
                stream,
                snapshot,
                declaration,
                runtime,
                root,
                now,
                zip_progress,
            )
        except BaseException:
            stream.close()
            raise
        _report_progress(
            progress,
            "finalizing",
            "Finalizing the verified editor ZIP…",
            files_done=len(snapshot.manifest.entries),
            files_total=len(snapshot.manifest.entries),
            bytes_done=snapshot.manifest.total_bytes,
            bytes_total=snapshot.manifest.total_bytes,
        )
        stamp = datetime.fromtimestamp(now or time.time(), tz=UTC).strftime("%Y%m%d-%H%M%SZ")
        profile_id = str(runtime.profile.profile_id).replace("_", "-")
        return SaveBundleExport(f"{profile_id}-save-{stamp}.zip", stream, snapshot.manifest)
    finally:
        snapshot.close()


async def async_inspect_save_bundle(
    runtime: ServiceRuntime,
    client: Any,
    bundle_key: str,
    archive_stream: Any,
    *,
    now: int | None = None,
    progress: SaveBundleProgress | None = None,
) -> SaveBundlePreview:
    """Validate an uploaded ZIP and compare its overlay with live truth."""

    _report_progress(progress, "checking_zip", "Checking the uploaded ZIP structure and save files…")
    profile, generation = _profile_snapshot(runtime)
    declaration = await _save_bundle_declaration(runtime, bundle_key)
    _require_state(runtime, declaration)
    upload = await _async_run_zip_worker(
        _read_upload_zip,
        archive_stream,
        declaration,
        str(profile.profile_id),
    )
    try:
        _report_progress(progress, "locating", "Locating the active save tree for comparison…")
        root = await _resolve_root(runtime, client, declaration, now=now)
        _require_profile_snapshot(runtime, profile, generation)
        service_id = runtime.state.identity.service_id
        async with client.authoritative_read(service_id):
            current = await client.snapshot_tree(
                service_id,
                root,
                max_files=DEFAULT_MAX_TREE_FILES,
                max_bytes=DEFAULT_MAX_TREE_BYTES,
                max_depth=DEFAULT_MAX_TREE_DEPTH,
                excluded_paths=declaration.excluded_paths,
                progress=lambda event, details: _report_progress(
                    progress,
                    "comparing",
                    f"Reading the live editor save for comparison — {int(details.get('files', 0))} file(s), "
                    f"{_format_progress_bytes(int(details.get('bytes', 0)))}…",
                    files_done=int(details.get("files", 0)),
                    bytes_done=int(details.get("bytes", 0)),
                    directories_done=int(details.get("directories", 0)),
                    current_file=str(details.get("path", "")),
                ),
            )
        try:
            _report_progress(progress, "validating", "Calculating the exact file-by-file restore review…")
            _require_profile_snapshot(runtime, profile, generation)
            _validate_required_files(declaration, current.manifest)
            proposed, added, replaced, unchanged, preserved = _overlay_manifest(current.manifest, upload.manifest)
            _validate_required_files(declaration, proposed)
            if proposed.total_bytes > DEFAULT_MAX_TREE_BYTES or len(proposed.entries) > DEFAULT_MAX_TREE_FILES:
                raise ProfileExtensionError(blocked("The merged save tree exceeds the supported safety limits."))
            return SaveBundlePreview(
                key=declaration.key,
                name=declaration.name,
                target_root=root,
                world_id=PurePosixPath(root).name,
                excluded_paths=declaration.excluded_paths,
                current=current.manifest,
                proposed=proposed,
                upload=upload,
                added=added,
                replaced=replaced,
                unchanged=unchanged,
                preserved=preserved,
            )
        finally:
            current.close()
    except BaseException:
        upload.close()
        raise


async def async_save_bundle_declaration(
    runtime: ServiceRuntime,
    bundle_key: str,
) -> SaveBundleDeclaration:
    """Return one validated declaration for HTTP access checks."""

    return await _save_bundle_declaration(runtime, bundle_key)


def _write_export_zip(
    stream: Any,
    snapshot: FileTreeSnapshot,
    declaration: SaveBundleDeclaration,
    runtime: ServiceRuntime,
    root: str,
    now: int | None,
    progress: SaveBundleProgress | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    created_at = int(now or time.time())
    metadata = {
        "schema": SAVE_BUNDLE_SCHEMA,
        "profile_id": str(runtime.profile.profile_id),
        "bundle_key": declaration.key,
        "world_id": PurePosixPath(root).name,
        "created_at": created_at,
        "manifest_sha256": tree_manifest_sha256(snapshot.manifest),
        "files": [{"path": item.path, "size": item.size, "sha256": item.sha256} for item in snapshot.manifest.entries],
    }
    with zipfile.ZipFile(
        stream, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
    ) as archive:
        archive.writestr(_zip_info(SAVE_BUNDLE_MANIFEST, created_at), json.dumps(metadata, sort_keys=True, indent=2))
        total_files = len(snapshot.manifest.entries)
        bytes_done = 0
        for index, item in enumerate(snapshot.manifest.entries, start=1):
            _check_worker_cancelled(cancel_event)
            _report_progress(
                progress,
                "compressing",
                f"Compressing the editor ZIP — file {index} of {total_files}…",
                files_done=index - 1,
                files_total=total_files,
                bytes_done=bytes_done,
                bytes_total=snapshot.manifest.total_bytes,
                current_file=item.path,
            )
            with archive.open(_zip_info(item.path, created_at), mode="w", force_zip64=True) as target:
                offset = 0
                while offset < item.size:
                    _check_worker_cancelled(cancel_event)
                    if isinstance(snapshot.files, SpoolTreeFiles):
                        chunk = snapshot.files.read_chunk(item.path, offset, _ZIP_CHUNK_BYTES)
                    else:
                        chunk = snapshot.files[item.path][offset : offset + _ZIP_CHUNK_BYTES]
                    if not chunk:
                        raise ProfileExtensionError(
                            blocked("The save snapshot ended unexpectedly while creating the ZIP.")
                        )
                    target.write(chunk)
                    offset += len(chunk)
            bytes_done += item.size
            _report_progress(
                progress,
                "compressing",
                f"Compressing the editor ZIP — {index} of {total_files} file(s) complete…",
                files_done=index,
                files_total=total_files,
                bytes_done=bytes_done,
                bytes_total=snapshot.manifest.total_bytes,
                current_file=item.path,
            )
    stream.seek(0)


def _zip_info(path: str, timestamp: int) -> zipfile.ZipInfo:
    moment = max(datetime.fromtimestamp(timestamp, tz=UTC), datetime(1980, 1, 1, tzinfo=UTC))
    info = zipfile.ZipInfo(
        path, date_time=(moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    info.create_system = 3
    return info


def _read_upload_zip(
    stream: Any,
    declaration: SaveBundleDeclaration,
    expected_profile_id: str,
    cancel_event: threading.Event | None = None,
) -> SaveBundleUpload:
    _check_worker_cancelled(cancel_event)
    stream.seek(0)
    files = SpoolTreeFiles()
    try:
        _preflight_zip_entry_count(stream)
        with zipfile.ZipFile(stream, mode="r") as archive:
            infos = _validated_zip_infos(archive)
            portable = _portable_manifest(archive, infos, declaration, expected_profile_id)
            prefix = _select_save_prefix(infos, portable, declaration)
            selected: list[tuple[zipfile.ZipInfo, str]] = []
            allowed_manifest_paths = None if portable is None else {item["path"] for item in portable["files"]}
            prefix_marker = f"{prefix}/" if prefix else ""
            for info in infos:
                _check_worker_cancelled(cancel_event)
                path = canonical_relative_path(info.filename, allow_root=False)
                if path == SAVE_BUNDLE_MANIFEST or _ignored_archive_path(path):
                    continue
                if prefix and not path.startswith(prefix_marker):
                    if portable is not None:
                        raise ProfileExtensionError(
                            blocked("The ZIP contains content outside its declared portable save root.")
                        )
                    continue
                relative = path[len(prefix_marker) :] if prefix else path
                relative = canonical_relative_path(relative, allow_root=False)
                if _is_excluded_path(relative, declaration.excluded_paths):
                    raise ProfileExtensionError(
                        blocked(
                            f"The ZIP contains profile-excluded history instead of only editor save files: {relative}"
                        )
                    )
                if not relative.casefold().endswith(tuple(s.casefold() for s in declaration.allowed_suffixes)):
                    raise ProfileExtensionError(
                        blocked(f"The ZIP contains unsupported content inside the selected save: {relative}")
                    )
                if allowed_manifest_paths is not None and relative not in allowed_manifest_paths:
                    raise ProfileExtensionError(
                        blocked(f"The ZIP contains a file not declared by its bundle manifest: {relative}")
                    )
                selected.append((info, relative))
            if not selected:
                raise ProfileExtensionError(blocked("The ZIP does not contain save files."))
            aliases: set[str] = set()
            entries: list[FileManifestEntry] = []
            total = 0
            for info, relative in selected:
                alias = relative.casefold()
                if alias in aliases:
                    raise ProfileExtensionError(blocked("The ZIP contains duplicate or case-colliding save paths."))
                aliases.add(alias)
                with archive.open(info, mode="r") as source:
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        _check_worker_cancelled(cancel_event)
                        chunk = source.read(_ZIP_CHUNK_BYTES)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_SAVE_BUNDLE_FILE_BYTES:
                            raise ProfileExtensionError(blocked(f"A save file exceeds the supported limit: {relative}"))
                        chunks.append(chunk)
                    content = b"".join(chunks)
                if len(content) != info.file_size or len(content) > MAX_SAVE_BUNDLE_FILE_BYTES:
                    raise ProfileExtensionError(blocked(f"A save file exceeds the supported limit: {relative}"))
                entries.append(files.append(relative, content))
                total += len(content)
            manifest = FileTreeManifest(tuple(sorted(entries, key=lambda item: item.path)), total)
            return SaveBundleUpload(files, manifest, prefix, portable is not None)
    except ProfileExtensionError:
        files.close()
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as err:
        files.close()
        raise ProfileExtensionError(blocked("The uploaded file is not a valid, supported ZIP archive.")) from err


def _validated_zip_infos(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos: list[zipfile.ZipInfo] = []
    aliases: set[str] = set()
    total = 0
    for info in archive.infolist():
        raw = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
        if not raw:
            continue
        try:
            path = canonical_relative_path(raw, allow_root=False)
        except FileTransportSecurityError as err:
            raise ProfileExtensionError(blocked("The ZIP contains an unsafe or ambiguous path.")) from err
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type not in {0, 0o100000, 0o040000}:
            raise ProfileExtensionError(blocked("The ZIP contains links or unsupported filesystem entries."))
        if info.flag_bits & 0x1:
            raise ProfileExtensionError(blocked("Encrypted ZIP files are not supported."))
        if info.is_dir():
            continue
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ProfileExtensionError(blocked("The ZIP uses an unsupported compression method."))
        if info.file_size < 0 or info.file_size > MAX_SAVE_BUNDLE_FILE_BYTES:
            raise ProfileExtensionError(blocked("A ZIP entry exceeds the supported file-size limit."))
        if info.file_size > _ZIP_CHUNK_BYTES and (
            info.compress_size <= 0 or info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO
        ):
            raise ProfileExtensionError(blocked("The ZIP has an unsafe compression ratio."))
        total += info.file_size
        if total > MAX_SAVE_BUNDLE_EXPANDED_BYTES or len(infos) >= DEFAULT_MAX_TREE_FILES + 1:
            raise ProfileExtensionError(blocked("The ZIP exceeds the supported file-count or expanded-size limit."))
        alias = path.casefold()
        if alias in aliases:
            raise ProfileExtensionError(blocked("The ZIP contains duplicate or case-colliding paths."))
        aliases.add(alias)
        infos.append(info)
    return tuple(infos)


def _preflight_zip_entry_count(stream: Any) -> None:
    """Bound central-directory materialization before ``ZipFile`` opens it."""

    try:
        stream.seek(0, 2)
        archive_size = stream.tell()
        tail_size = min(archive_size, 65_557)
        stream.seek(archive_size - tail_size)
        tail = stream.read(tail_size)

        marker = len(tail)
        eocd: tuple[int, int, int, int, int, int, int] | None = None
        while True:
            marker = tail.rfind(b"PK\x05\x06", 0, marker)
            if marker < 0:
                break
            if len(tail) - marker >= 22:
                try:
                    (
                        _signature,
                        disk,
                        central_disk,
                        disk_entries,
                        total_entries,
                        central_size,
                        central_offset,
                        comment_length,
                    ) = struct.unpack_from("<4s4H2LH", tail, marker)
                except struct.error:
                    marker -= 1
                    continue
                if marker + 22 + comment_length == len(tail):
                    eocd = (
                        archive_size - tail_size + marker,
                        disk,
                        central_disk,
                        disk_entries,
                        total_entries,
                        central_size,
                        central_offset,
                    )
                    break
            marker -= 1
        if eocd is None:
            raise ProfileExtensionError(blocked("The ZIP end-of-central-directory record is malformed."))

        eocd_offset, disk, central_disk, disk_entries, total_entries, central_size, central_offset = eocd
        uses_zip64 = any(
            value == sentinel
            for value, sentinel in (
                (disk_entries, 0xFFFF),
                (total_entries, 0xFFFF),
                (central_size, 0xFFFFFFFF),
                (central_offset, 0xFFFFFFFF),
            )
        )
        if disk != 0 or central_disk != 0 or (not uses_zip64 and disk_entries != total_entries):
            raise ProfileExtensionError(blocked("Multi-disk ZIP archives are not supported."))

        central_end = eocd_offset
        if uses_zip64:
            locator_offset = eocd_offset - 20
            if locator_offset < 0:
                raise ProfileExtensionError(blocked("The ZIP64 central directory is malformed."))
            stream.seek(locator_offset)
            locator = stream.read(20)
            locator_signature, zip64_disk, zip64_offset, zip64_disks = struct.unpack("<4sLQL", locator)
            if locator_signature != b"PK\x06\x07" or zip64_disk != 0 or zip64_disks != 1:
                raise ProfileExtensionError(blocked("Multi-disk ZIP archives are not supported."))
            if zip64_offset < 0 or zip64_offset + 56 > locator_offset:
                raise ProfileExtensionError(blocked("The ZIP64 central directory is malformed."))
            stream.seek(zip64_offset)
            record = stream.read(56)
            (
                zip64_signature,
                record_size,
                _made_by,
                _required,
                zip64_disk,
                zip64_central_disk,
                zip64_disk_entries,
                zip64_total_entries,
                central_size,
                central_offset,
            ) = struct.unpack("<4sQ2H2L4Q", record)
            if (
                zip64_signature != b"PK\x06\x06"
                or record_size < 44
                or zip64_offset + 12 + record_size != locator_offset
                or zip64_disk != 0
                or zip64_central_disk != 0
                or zip64_disk_entries != zip64_total_entries
            ):
                raise ProfileExtensionError(blocked("The ZIP64 central directory is malformed."))
            total_entries = zip64_total_entries
            central_end = zip64_offset

        if total_entries > DEFAULT_MAX_DIRECTORY_ENTRIES:
            raise ProfileExtensionError(blocked("The ZIP exceeds the supported total entry-count limit."))
        if central_size > MAX_ZIP_CENTRAL_DIRECTORY_BYTES:
            raise ProfileExtensionError(blocked("The ZIP central directory exceeds the supported size limit."))
        if central_offset > archive_size or central_size > archive_size - central_offset:
            raise ProfileExtensionError(blocked("The ZIP central directory points outside the uploaded archive."))
        if central_offset + central_size != central_end:
            raise ProfileExtensionError(blocked("The ZIP central-directory geometry is inconsistent."))
        if total_entries:
            _preflight_central_directory_records(
                stream,
                central_offset=central_offset,
                central_size=central_size,
                expected_entries=total_entries,
            )
        elif central_size != 0:
            raise ProfileExtensionError(blocked("The ZIP central-directory geometry is inconsistent."))
    except struct.error as err:
        raise ProfileExtensionError(blocked("The ZIP central directory is malformed.")) from err
    finally:
        stream.seek(0)


def _preflight_central_directory_records(
    stream: Any,
    *,
    central_offset: int,
    central_size: int,
    expected_entries: int,
) -> None:
    """Count bounded physical records before ``ZipFile`` allocates them."""

    cursor = central_offset
    remaining = central_size
    physical_entries = 0
    while remaining:
        if remaining < 46:
            raise ProfileExtensionError(blocked("The ZIP central directory is malformed."))
        stream.seek(cursor)
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise ProfileExtensionError(blocked("The ZIP central directory is malformed."))
        name_length, extra_length, comment_length = struct.unpack_from("<3H", header, 28)
        record_size = 46 + name_length + extra_length + comment_length
        if record_size > remaining:
            raise ProfileExtensionError(blocked("The ZIP central-directory geometry is inconsistent."))
        physical_entries += 1
        if physical_entries > DEFAULT_MAX_DIRECTORY_ENTRIES:
            raise ProfileExtensionError(blocked("The ZIP exceeds the supported total entry-count limit."))
        cursor += record_size
        remaining -= record_size
    if physical_entries != expected_entries:
        raise ProfileExtensionError(blocked("The ZIP central-directory entry count is inconsistent."))


def _portable_manifest(
    archive: zipfile.ZipFile,
    infos: tuple[zipfile.ZipInfo, ...],
    declaration: SaveBundleDeclaration,
    expected_profile_id: str,
) -> dict[str, Any] | None:
    info = next((item for item in infos if item.filename == SAVE_BUNDLE_MANIFEST), None)
    if info is None:
        return None
    if info.file_size > MAX_SAVE_BUNDLE_MANIFEST_BYTES:
        raise ProfileExtensionError(blocked("The bundle manifest exceeds the supported size limit."))
    try:
        with archive.open(info, mode="r") as source:
            raw = source.read(MAX_SAVE_BUNDLE_MANIFEST_BYTES + 1)
        if len(raw) != info.file_size or len(raw) > MAX_SAVE_BUNDLE_MANIFEST_BYTES:
            raise ProfileExtensionError(blocked("The bundle manifest exceeds the supported size limit."))
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as err:
        raise ProfileExtensionError(blocked("The bundle manifest is malformed.")) from err
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SAVE_BUNDLE_SCHEMA
        or payload.get("profile_id") != expected_profile_id
        or payload.get("bundle_key") != declaration.key
        or not isinstance(payload.get("files"), list)
        or len(payload["files"]) > DEFAULT_MAX_TREE_FILES
    ):
        raise ProfileExtensionError(blocked("The ZIP contains an incompatible save-bundle manifest."))
    seen: set[str] = set()
    for item in payload["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ProfileExtensionError(blocked("The bundle manifest contains an invalid file entry."))
        try:
            path = canonical_relative_path(item["path"], allow_root=False)
        except FileTransportSecurityError as err:
            raise ProfileExtensionError(blocked("The bundle manifest contains an unsafe path.")) from err
        if path != item["path"] or path.casefold() in seen:
            raise ProfileExtensionError(blocked("The bundle manifest contains duplicate or ambiguous paths."))
        seen.add(path.casefold())
    return payload


def _select_save_prefix(
    infos: tuple[zipfile.ZipInfo, ...],
    portable: dict[str, Any] | None,
    declaration: SaveBundleDeclaration,
) -> str:
    paths = tuple(
        canonical_relative_path(info.filename, allow_root=False)
        for info in infos
        if info.filename != SAVE_BUNDLE_MANIFEST
        and not _ignored_archive_path(canonical_relative_path(info.filename, allow_root=False))
    )
    candidates: list[str] = []
    if portable is not None:
        declared = tuple(item["path"] for item in portable["files"])
        aliases = {path.casefold() for path in paths}
        first = declared[0] if declared else None
        if first is not None:
            first_alias = first.casefold()
            for path in paths:
                alias = path.casefold()
                if alias == first_alias:
                    prefix = ""
                elif alias.endswith(f"/{first_alias}"):
                    prefix = path[: -(len(first) + 1)]
                else:
                    continue
                marker = f"{prefix}/" if prefix else ""
                if all(f"{marker}{relative}".casefold() in aliases for relative in declared):
                    candidates.append(prefix)
    else:
        file_markers = tuple(tuple(PurePosixPath(item).parts) for item in declaration.editor_root_files)
        directory_markers = tuple(tuple(PurePosixPath(item).parts) for item in declaration.editor_root_directories)
        for path in paths:
            parts = PurePosixPath(path).parts
            folded = tuple(part.casefold() for part in parts)
            for marker in file_markers:
                marker_folded = tuple(part.casefold() for part in marker)
                if len(parts) >= len(marker) and folded[-len(marker) :] == marker_folded:
                    candidates.append("/".join(parts[: -len(marker)]))
            for marker in directory_markers:
                marker_folded = tuple(part.casefold() for part in marker)
                width = len(marker)
                for index in range(0, len(parts) - width):
                    if folded[index : index + width] == marker_folded:
                        candidates.append("/".join(parts[:index]))
                        break
    if not candidates:
        raise ProfileExtensionError(blocked("The ZIP does not contain a save root recognized by this game profile."))
    unique = sorted(set(candidates))
    excluded = tuple(item.casefold() for item in declaration.excluded_paths)
    filtered: list[str] = []
    for candidate in unique:
        nested_under_excluded = False
        for ancestor in unique:
            if ancestor == candidate:
                continue
            marker = f"{ancestor}/" if ancestor else ""
            if not candidate.startswith(marker):
                continue
            relative = candidate[len(marker) :].casefold()
            if any(relative == item or relative.startswith(f"{item}/") for item in excluded):
                nested_under_excluded = True
                break
        if not nested_under_excluded:
            filtered.append(candidate)
    if len(filtered) != 1:
        raise ProfileExtensionError(blocked("The ZIP contains multiple ambiguous save-game roots."))
    return filtered[0]


def _ignored_archive_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts and (parts[0] in _IGNORED_ARCHIVE_PARTS or parts[-1] in _IGNORED_ARCHIVE_NAMES))


def _overlay_manifest(
    current: FileTreeManifest,
    upload: FileTreeManifest,
) -> tuple[
    FileTreeManifest,
    tuple[FileManifestEntry, ...],
    tuple[FileManifestEntry, ...],
    tuple[FileManifestEntry, ...],
    tuple[FileManifestEntry, ...],
]:
    current_map = {item.path: item for item in current.entries}
    upload_map = {item.path: item for item in upload.entries}
    merged = {**current_map, **upload_map}
    added = tuple(item for path, item in sorted(upload_map.items()) if path not in current_map)
    replaced = tuple(
        item for path, item in sorted(upload_map.items()) if path in current_map and item != current_map[path]
    )
    unchanged = tuple(
        item for path, item in sorted(upload_map.items()) if path in current_map and item == current_map[path]
    )
    preserved = tuple(item for path, item in sorted(current_map.items()) if path not in upload_map)
    proposed = FileTreeManifest(
        tuple(merged[path] for path in sorted(merged)), sum(item.size for item in merged.values())
    )
    return proposed, added, replaced, unchanged, preserved


async def _save_bundle_declaration(runtime: ServiceRuntime, bundle_key: str) -> SaveBundleDeclaration:
    if runtime.profile is None:
        raise ProfileExtensionError(blocked("No game profile is selected for this service."))
    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        raise ProfileExtensionError(blocked("The selected game profile manifest is invalid.")) from err
    declaration = next((item for item in manifest.save_bundles if item.key == bundle_key), None)
    if declaration is None:
        raise ProfileExtensionError(blocked(f"Profile save bundle not found: {bundle_key}"))
    return declaration


async def _resolve_root(
    runtime: ServiceRuntime,
    client: Any,
    declaration: SaveBundleDeclaration,
    *,
    now: int | None,
) -> str:
    context = profile_action_context(client, runtime, now=now)
    try:
        root = await async_invoke_profile(declaration.root_fn, context)
    except Exception as err:  # noqa: BLE001 - untrusted profile boundary
        log_profile_failure(
            _LOGGER,
            "save_root",
            err,
            profile_id=getattr(runtime.profile, "profile_id", "unknown"),
            key=declaration.key,
        )
        raise ProfileExtensionError(blocked(f"{declaration.name} location could not be resolved safely.")) from None
    if type(root) is not str or not root.strip():
        raise ProfileExtensionError(blocked(f"{declaration.name} is not available on this server."))
    try:
        return RemotePath.parse(root.strip(), allow_root=False).value
    except FileTransportSecurityError as err:
        raise ProfileExtensionError(blocked("The profile returned an unsafe save-tree path.")) from err


def _validate_required_files(declaration: SaveBundleDeclaration, manifest: FileTreeManifest) -> None:
    paths = {item.path.casefold() for item in manifest.entries}
    missing = [path for path in declaration.required_files if path.casefold() not in paths]
    if missing:
        raise ProfileExtensionError(blocked(f"The save tree is missing required file: {missing[0]}"))


def _is_excluded_path(path: str, excluded_paths: tuple[str, ...]) -> bool:
    alias = canonical_relative_path(path, allow_root=False).casefold()
    excluded = tuple(canonical_relative_path(item, allow_root=False).casefold() for item in excluded_paths)
    return any(alias == item or alias.startswith(f"{item}/") for item in excluded)


def active_save_manifest(
    manifest: FileTreeManifest,
    excluded_paths: tuple[str, ...],
) -> FileTreeManifest:
    """Return the editor-facing portion of a full remote save tree."""

    entries = tuple(item for item in manifest.entries if not _is_excluded_path(item.path, excluded_paths))
    return FileTreeManifest(entries, sum(item.size for item in entries))


def _format_progress_bytes(value: int) -> str:
    size = max(int(value), 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def _report_progress(
    progress: SaveBundleProgress | None,
    stage: str,
    message: str,
    **details: Any,
) -> None:
    if progress is not None:
        progress(stage, message, details)


def _require_state(runtime: ServiceRuntime, declaration: SaveBundleDeclaration) -> None:
    if not runtime.status_fresh or runtime.using_cached_data or runtime.server is None:
        raise ProfileExtensionError(blocked("Refresh the server status before working with save bundles."))
    if declaration.requires_stopped and runtime.server.raw_status != STOPPED_STATUS:
        raise ProfileExtensionError(
            blocked("Stop the game server before downloading, reviewing, or restoring a save bundle.")
        )


def _profile_snapshot(runtime: ServiceRuntime) -> tuple[GameProfile, tuple[int, int]]:
    profile = runtime.profile
    if profile is None:
        raise ProfileExtensionError(blocked("No game profile is selected for this service."))
    return profile, (runtime.profile_generation, runtime.profile_registry_generation_seen)


def _require_profile_snapshot(
    runtime: ServiceRuntime,
    profile: GameProfile,
    generation: tuple[int, int],
) -> None:
    if (
        runtime.profile is not profile
        or runtime.profile_generation != generation[0]
        or runtime.profile_registry_generation_seen != generation[1]
        or generation[1] != profile_registry_generation()
    ):
        raise ProfileExtensionError(blocked("The selected game profile changed during the save operation."))


def preview_payload(preview: SaveBundlePreview) -> dict[str, Any]:
    """Return a bounded, content-free review payload."""

    def items(values: tuple[FileManifestEntry, ...]) -> list[dict[str, Any]]:
        return [{"path": item.path, "size": item.size, "sha256": item.sha256} for item in values[:200]]

    return {
        "key": preview.key,
        "name": preview.name,
        "world_id": preview.world_id,
        "changed": preview.changed,
        "portable_manifest": preview.upload.portable_manifest,
        "source_prefix": preview.upload.source_prefix,
        "current": {
            "files": len(preview.current.entries),
            "bytes": preview.current.total_bytes,
            "sha256": tree_manifest_sha256(preview.current),
        },
        "proposed": {
            "files": len(preview.proposed.entries),
            "bytes": preview.proposed.total_bytes,
            "sha256": tree_manifest_sha256(preview.proposed),
        },
        "counts": {
            "added": len(preview.added),
            "replaced": len(preview.replaced),
            "unchanged": len(preview.unchanged),
            "preserved": len(preview.preserved),
            "deleted": 0,
        },
        "added": items(preview.added),
        "replaced": items(preview.replaced),
        "unchanged": items(preview.unchanged),
        "preserved": items(preview.preserved),
        "truncated": any(
            len(values) > 200 for values in (preview.added, preview.replaced, preview.unchanged, preview.preserved)
        ),
    }


__all__ = (
    "MAX_SAVE_BUNDLE_UPLOAD_BYTES",
    "OverlayTreeFiles",
    "SaveBundleApplyResult",
    "SaveBundleExport",
    "SaveBundlePreview",
    "active_save_manifest",
    "async_export_save_bundle",
    "async_inspect_save_bundle",
    "async_save_bundle_declaration",
    "preview_payload",
)
