"""Reliable, bounded filesystem primitives for Nitrado services.

This module deliberately keeps provider transport mechanics out of game
profiles.  Paths accepted by the internal API are service-root-relative.
Nitrado's account-specific HTTP ``.../ftproot`` prefix is discovered from a
root listing and must be proven before any HTTP/FTP path translation occurs.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import posixpath
import socket
import ssl
import tempfile
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum

# FTPS is mandatory by default; plaintext FTP is gated per service by explicit
# administrator consent.  Bandit cannot infer that runtime security policy.
from ftplib import FTP, FTP_TLS, error_perm, parse227, parse229  # nosec B402
from ftplib import all_errors as FTP_ERRORS  # nosec B402
from io import BytesIO
from typing import Any, Protocol, TypeVar

from .api.nitrado import NitradoApiError, NitradoClient, NitradoFtpCredentials
from .filesystem_journal import (
    CompletionDisposition,
    FilesystemTransactionJournal,
    FilesystemTransactionRecord,
    FileTransactionOperation,
    JournalFailureCode,
    ServiceReference,
    TransactionState,
    VerificationEvidence,
    VerificationExpectation,
    VerificationKind,
    VerificationPurpose,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BINARY_BYTES = 128 * 1024 * 1024
# This covers known provider backups above 90 MiB while still bounding the
# current materialized tree representation.  The future provider connector
# must move to a disk-backed snapshot type before increasing this further.
DEFAULT_MAX_TREE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_TREE_FILES = 5_000
DEFAULT_MAX_TREE_DIRECTORIES = 5_000
DEFAULT_MAX_DIRECTORY_ENTRIES = DEFAULT_MAX_TREE_FILES + DEFAULT_MAX_TREE_DIRECTORIES
DEFAULT_MAX_TREE_DEPTH = 32
DEFAULT_MAX_PATH_BYTES = 4096
DEFAULT_MAX_COMPONENT_BYTES = 255
FTP_CONNECT_TIMEOUT_SECONDS = 30
FTP_OPERATION_TIMEOUT_SECONDS = 60
FTP_WORKER_DRAIN_TIMEOUT_SECONDS = 35
FTP_CAPABILITY_CACHE_SECONDS = 300
FTP_CAPABILITY_FAILURE_BACKOFF_MAX_SECONDS = 60
FTP_ROOT_MARKER = "/ftproot/"

# Header plus the worst-case per-file path/header overhead.  Journal storage
# must accept the encoded recovery object, not merely its raw file payload.
MAX_TREE_RECOVERY_BLOB_BYTES = DEFAULT_MAX_TREE_BYTES + DEFAULT_MAX_TREE_FILES * (12 + DEFAULT_MAX_PATH_BYTES) + 6

_T = TypeVar("_T")
_EXPECTED_CONTENT_UNSET = object()


class FileTransportError(NitradoApiError):
    """Raised when a provider filesystem operation fails safely."""


class FileTransportSecurityError(FileTransportError):
    """Raised when provider transport security policy blocks an operation."""


class FileVerificationError(FileTransportError):
    """Raised when provider content does not match the expected result."""


class FileRecoveryError(FileTransportError):
    """Raised when a failed mutation cannot be rolled back and verified."""


class FileConcurrentModificationError(FileTransportError):
    """Raised when the authoritative FTP value changed after preview."""


class FileEntryType(StrEnum):
    """Portable provider file entry types."""

    FILE = "file"
    DIRECTORY = "directory"


class FileTransportKind(StrEnum):
    """Provider mechanisms used for one operation."""

    HTTP = "http_file_api"
    FTP = "ftp"
    FTPS = "ftps"


class VerificationStrength(StrEnum):
    """Strength of a completed provider verification."""

    NONE = "none"
    SAME_TRANSPORT = "same_transport"
    INDEPENDENT_TRANSPORT = "independent_transport"


@dataclass(slots=True, frozen=True, order=True)
class RemotePath:
    """A canonical service-root-relative path.

    This type rejects every ambiguous spelling rather than normalizing it.
    That keeps authorization and locking decisions stable across transports.
    The empty value represents the service FTP root.
    """

    value: str

    @classmethod
    def parse(cls, path: str | RemotePath, *, allow_root: bool = True) -> RemotePath:
        """Validate a consumer-supplied relative path."""

        if isinstance(path, cls):
            if not allow_root and not path.value:
                raise FileTransportSecurityError("A file path cannot name the service root")
            return path
        value = str(path)
        if not value:
            if allow_root:
                return cls("")
            raise FileTransportSecurityError("A file path cannot be empty")
        if value != value.strip():
            raise FileTransportSecurityError("Remote path cannot contain surrounding whitespace")
        if value.startswith("/"):
            raise FileTransportSecurityError("Remote paths must be relative to the service root")
        if "\\" in value:
            raise FileTransportSecurityError("Remote paths must use POSIX separators")
        if len(value.encode("utf-8")) > DEFAULT_MAX_PATH_BYTES:
            raise FileTransportSecurityError("Remote path exceeds the configured length limit")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise FileTransportSecurityError("Remote path contains an unsafe or ambiguous component")
        for part in parts:
            if part != part.strip():
                raise FileTransportSecurityError("Remote path component has ambiguous surrounding whitespace")
            if len(part.encode("utf-8")) > DEFAULT_MAX_COMPONENT_BYTES:
                raise FileTransportSecurityError("Remote path component exceeds the configured length limit")
            if any(ord(char) < 32 or ord(char) == 127 for char in part):
                raise FileTransportSecurityError("Remote path contains a control character")
            if unicodedata.normalize("NFC", part) != part:
                raise FileTransportSecurityError("Remote path must use NFC-normalized Unicode")
        return cls("/".join(parts))

    def child(self, path: str | RemotePath) -> RemotePath:
        """Return a validated descendant."""

        child = self.parse(path)
        if not self.value:
            return child
        if not child.value:
            return self
        return self.parse(f"{self.value}/{child.value}")

    @property
    def parent(self) -> RemotePath:
        """Return the canonical parent path."""

        return self.parse(posixpath.dirname(self.value))

    @property
    def name(self) -> str:
        """Return the final component."""

        return posixpath.basename(self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(slots=True, frozen=True)
class FileCapabilities:
    """Explicit filesystem capabilities available for one service."""

    list_directory: bool = True
    read_text: bool = True
    read_bytes: bool = False
    write_file: bool = False
    recursive_read: bool = False
    recursive_write: bool = False
    recursive_delete: bool = False
    rename: bool = False
    secure_transport: bool = True
    transport: FileTransportKind = FileTransportKind.HTTP


@dataclass(slots=True, frozen=True)
class FileEntry:
    """One normalized remote filesystem entry."""

    path: str
    name: str
    entry_type: FileEntryType
    size: int | None = None
    modified: str | None = None


@dataclass(slots=True, frozen=True)
class FileManifestEntry:
    """Expected identity for one file in a tree operation."""

    path: str
    size: int
    sha256: str


@dataclass(slots=True, frozen=True)
class FileTreeManifest:
    """Exact bounded manifest for a remote or local file tree."""

    entries: tuple[FileManifestEntry, ...]
    total_bytes: int

    @classmethod
    def from_files(cls, files: Mapping[str, bytes]) -> FileTreeManifest:
        """Build a deterministic manifest from relative binary content."""

        entries: list[FileManifestEntry] = []
        total = 0
        for raw_path, blob in sorted(files.items()):
            path = canonical_relative_path(raw_path, allow_root=False)
            data = bytes(blob)
            total += len(data)
            entries.append(FileManifestEntry(path, len(data), sha256_bytes(data)))
        return cls(tuple(entries), total)


class SpoolTreeFiles(Mapping[str, bytes]):
    """Closeable tree content backed by an unnamed spooled temporary file.

    Only one file is materialized when the mapping is indexed.  The aggregate
    tree rolls to an OS-managed temporary file after a small threshold, so a
    valid large game tree cannot consume the Home Assistant process heap.
    No temporary path is exposed to profiles or provider consumers.
    """

    _SPOOL_MEMORY_BYTES = 1024 * 1024

    def __init__(self) -> None:
        # The spool intentionally lives for the snapshot object's lifetime and
        # is closed by ``close``/``__del__``, not this constructor scope.
        self._stream = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=self._SPOOL_MEMORY_BYTES,
            mode="w+b",
        )
        self._entries: dict[str, tuple[int, int]] = {}
        self._aliases: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False

    def append(self, path: str, content: bytes) -> FileManifestEntry:
        """Append one canonical file and return its exact manifest entry."""

        canonical = canonical_relative_path(path, allow_root=False)
        blob = bytes(content)
        with self._lock:
            self._ensure_open()
            if canonical in self._entries:
                raise FileTransportError("Tree snapshot contains a duplicate path")
            alias = canonical.casefold()
            if alias in self._aliases:
                raise FileTransportSecurityError("Tree snapshot contains case-ambiguous paths")
            self._stream.seek(0, 2)
            offset = self._stream.tell()
            self._stream.write(blob)
            self._entries[canonical] = (offset, len(blob))
            self._aliases.add(alias)
        return FileManifestEntry(canonical, len(blob), sha256_bytes(blob))

    def __getitem__(self, path: str) -> bytes:
        canonical = canonical_relative_path(path, allow_root=False)
        with self._lock:
            self._ensure_open()
            try:
                offset, size = self._entries[canonical]
            except KeyError:
                raise KeyError(path) from None
            self._stream.seek(offset)
            content = self._stream.read(size)
        if len(content) != size:
            raise FileRecoveryError("Spooled tree content was truncated")
        return content

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            self._ensure_open()
            return iter(tuple(self._entries))

    def __len__(self) -> int:
        with self._lock:
            self._ensure_open()
            return len(self._entries)

    def read_chunk(self, path: str, offset: int, size: int) -> bytes:
        """Read one bounded chunk without materializing the whole file."""

        if offset < 0 or size < 1:
            raise ValueError("tree chunk bounds are invalid")
        canonical = canonical_relative_path(path, allow_root=False)
        with self._lock:
            self._ensure_open()
            try:
                start, length = self._entries[canonical]
            except KeyError:
                raise KeyError(path) from None
            if offset > length:
                raise ValueError("tree chunk offset exceeds file size")
            self._stream.seek(start + offset)
            return self._stream.read(min(size, length - offset))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._entries.clear()
            self._aliases.clear()
            self._stream.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise FileRecoveryError("Tree snapshot content is closed")

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()


@dataclass(slots=True, frozen=True)
class FileTreeSnapshot:
    """Recoverable bounded tree content plus exact manifest."""

    files: Mapping[str, bytes]
    manifest: FileTreeManifest

    def close(self) -> None:
        """Release any disk-backed snapshot content."""

        close = getattr(self.files, "close", None)
        if callable(close):
            close()


@dataclass(slots=True, frozen=True)
class FileOperationResult:
    """Sanitized result for one verified provider mutation."""

    transaction_id: str
    operation: str
    transport: FileTransportKind
    verification: VerificationStrength
    manifest: FileTreeManifest | None = None
    recovery_available: bool = False


class FileTransport(Protocol):
    """Internal capability-oriented provider transport."""

    async def capabilities(self, service_id: str) -> FileCapabilities:
        """Return capabilities for one service."""

    async def list_directory(self, service_id: str, path: str) -> tuple[FileEntry, ...]:
        """List one directory."""

    async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
        """Read one bounded file."""

    async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
        """Write one file."""


class NitradoHttpFileTransport:
    """Nitrado HTTP file-browser transport with narrow text semantics."""

    def __init__(self, client: NitradoClient) -> None:
        self.client = client
        self._root_prefixes: dict[str, str] = {}
        self._mapping_locks: dict[str, asyncio.Lock] = {}

    async def capabilities(self, service_id: str) -> FileCapabilities:
        del service_id
        return FileCapabilities(write_file=True)

    async def list_raw(self, service_id: str, directory: str | None = None) -> dict[str, Any]:
        payload = await self.client.list_files(service_id, directory)
        if directory is None:
            self._cache_root_mapping(service_id, payload)
        return payload

    async def list_directory(self, service_id: str, path: str) -> tuple[FileEntry, ...]:
        provider_path = await self.provider_path(service_id, path)
        payload = await self.list_raw(service_id, provider_path)
        return parse_http_file_entries(payload, root_prefix=self._root_prefixes[str(service_id)])

    async def read_text(self, service_id: str, path: str) -> str:
        return await self.client.download_file(service_id, await self.provider_path(service_id, path))

    async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
        text = await self.read_text(service_id, path)
        data = text.encode("utf-8")
        if len(data) > max_bytes:
            raise FileTransportError("Nitrado HTTP file read exceeded the configured limit")
        return data

    async def write_text(self, service_id: str, path: str, content: str) -> None:
        await self.client.upload_text_file(
            service_id,
            await self.provider_path(service_id, path),
            content,
        )

    async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as err:
            raise FileTransportError("Nitrado HTTP file transport only supports UTF-8 text writes") from err
        await self.write_text(service_id, path, text)

    async def relative_path(self, service_id: str, path: str | RemotePath) -> str:
        """Translate a proven provider path to service-root-relative form."""

        prefix = await self._ensure_root_mapping(service_id)
        if isinstance(path, RemotePath) or not str(path).startswith("/"):
            return canonical_relative_path(path)
        raw = str(path)
        if raw == prefix:
            return ""
        marker = f"{prefix}/"
        if not raw.startswith(marker):
            raise FileTransportSecurityError("Provider path is outside the proven service FTP root")
        return canonical_relative_path(raw[len(marker) :])

    async def provider_path(self, service_id: str, path: str | RemotePath) -> str:
        """Translate a relative path using the proven per-service HTTP root."""

        prefix = await self._ensure_root_mapping(service_id)
        relative = await self.relative_path(service_id, path)
        return provider_api_path(relative, root_prefix=prefix)

    async def relative_payload(self, service_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return a deep copy whose provider paths are root-relative.

        Nitrado has emitted several list envelope shapes.  Only explicit path
        fields are translated; all other provider metadata is preserved for
        profile matching compatibility.
        """

        prefix = await self._ensure_root_mapping(service_id)

        def convert(value: Any) -> Any:
            if isinstance(value, Mapping):
                copied: dict[str, Any] = {}
                for key, child in value.items():
                    if key in {"path", "file"} and isinstance(child, str):
                        copied[str(key)] = legacy_provider_path_to_relative(
                            child,
                            root_prefix=prefix,
                        )
                    else:
                        copied[str(key)] = convert(child)
                return copied
            if isinstance(value, list):
                return [convert(child) for child in value]
            if isinstance(value, tuple):
                return [convert(child) for child in value]
            return value

        converted = convert(payload)
        if not isinstance(converted, dict):
            raise FileTransportError("Nitrado file list returned an invalid payload")
        return converted

    def observed_status(self, service_id: str) -> dict[str, Any]:
        """Return cached mapping state without provider I/O."""

        prefix = self._root_prefixes.get(str(service_id))
        return {
            "root_mapping_proven": prefix is not None,
            # The actual account-specific prefix is intentionally not exposed
            # in diagnostics; only its proof state matters to users.
        }

    async def _ensure_root_mapping(self, service_id: str) -> str:
        service_id = str(service_id)
        cached = self._root_prefixes.get(service_id)
        if cached is not None:
            return cached
        lock = self._mapping_locks.setdefault(service_id, asyncio.Lock())
        async with lock:
            cached = self._root_prefixes.get(service_id)
            if cached is not None:
                return cached
            payload = await self.client.list_files(service_id, None)
            self._cache_root_mapping(service_id, payload)
            return self._root_prefixes[service_id]

    def _cache_root_mapping(self, service_id: str, payload: Mapping[str, Any]) -> None:
        prefix = derive_http_root_prefix(payload)
        existing = self._root_prefixes.get(str(service_id))
        if existing is not None and existing != prefix:
            raise FileTransportSecurityError("Nitrado HTTP file root changed during the active session")
        self._root_prefixes[str(service_id)] = prefix


class _CredentialRejected(Exception):
    """A connection was rejected before an operation began."""


class _PlaintextConsentRequired(FileTransportSecurityError):
    """Secure FTP failed and the only permitted fallback needs consent."""


class _PinnedPassiveMixin:
    """Prefer EPSV and pin passive data connections to the control peer."""

    def makepasv(self) -> tuple[str, int]:
        if self.sock is None:  # type: ignore[attr-defined]
            raise FileTransportError("FTP control connection is not established")
        peer = self.sock.getpeername()[0]  # type: ignore[attr-defined]
        try:
            return peer, parse229(self.sendcmd("EPSV"), self.sock.getpeername())[1]  # type: ignore[attr-defined]
        except error_perm:
            if self.af != socket.AF_INET:  # type: ignore[attr-defined]
                raise
            _ignored_host, port = parse227(self.sendcmd("PASV"))
            return peer, port

    def connect_pinned(self, hostname: str, address: str, port: int, timeout: float) -> str:
        """Connect to a previously validated address while retaining TLS SNI."""

        self.host = hostname  # type: ignore[attr-defined]
        self.port = port  # type: ignore[attr-defined]
        self.timeout = timeout  # type: ignore[attr-defined]
        self.sock = socket.create_connection((address, port), timeout, self.source_address)  # type: ignore[attr-defined]
        self.af = self.sock.family  # type: ignore[attr-defined]
        self.file = self.sock.makefile("r", encoding=self.encoding)  # type: ignore[attr-defined]
        return self.getresp()  # type: ignore[attr-defined,no-any-return]


class _PinnedFTP(_PinnedPassiveMixin, FTP):
    """Plain FTP with pinned control and passive data peers."""


class _PinnedFTP_TLS(_PinnedPassiveMixin, FTP_TLS):
    """Explicit FTPS with pinned control and passive data peers."""


@dataclass(slots=True)
class _FtpSession:
    ftp: FTP
    root: str
    kind: FileTransportKind
    tree_directory: str | None = None
    tree_entries: dict[str, str] = field(default_factory=dict)
    tree_ancestors: list[tuple[str, dict[str, str]]] = field(default_factory=list)


class _WorkerOperation:
    """Thread-safe handle used to abort and drain an FTP worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ftp: FTP | None = None
        self._aborted = False

    def attach(self, ftp: FTP) -> None:
        with self._lock:
            if self._aborted:
                with suppress(OSError):
                    ftp.close()
                raise FileTransportError("Nitrado FTP operation was aborted during connection")
            self._ftp = ftp

    def detach(self) -> None:
        with self._lock:
            self._ftp = None

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            ftp = self._ftp
        if ftp is not None:
            with suppress(OSError):
                ftp.close()


class _ScopedFtpSession:
    """One authenticated FTP session reused for a bounded transaction."""

    def __init__(
        self,
        transport: NitradoFtpTransport,
        service_id: str,
        session: _FtpSession,
        operation: _WorkerOperation,
    ) -> None:
        self._transport = transport
        self.service_id = str(service_id)
        self.session = session
        self.operation = operation
        self.closed = False

    @property
    def kind(self) -> FileTransportKind:
        return self.session.kind

    async def run(self, callback: Callable[..., _T], *args: Any) -> _T:
        if self.closed:
            raise FileTransportError("Nitrado FTP transaction session is closed")
        return await self._transport._run_scoped_callback(self, callback, *args)

    async def list_directory(self, path: str) -> tuple[FileEntry, ...]:
        return await self.run(_ftp_list_directory, ftp_relative_path(path))

    async def position_tree_cursor(self, path: str) -> None:
        """Position a verified cursor at one tree root.

        Tree snapshots reuse this cursor instead of re-walking and re-listing
        every path ancestor before every MLSD and RETR data transfer.
        """

        await self.run(_ftp_tree_position, ftp_relative_path(path))

    async def list_tree_cursor(self, path: str) -> tuple[FileEntry, ...]:
        """List the cursor's verified current directory once."""

        return await self.run(_ftp_tree_list_current, ftp_relative_path(path))

    async def read_tree_cursor_file(self, directory: str, name: str, *, max_bytes: int) -> bytes:
        """Read one file classified by the cursor's current MLSD result."""

        _validate_ftp_read_limit(max_bytes)
        return await self.run(
            _ftp_tree_read_current,
            ftp_relative_path(directory),
            ftp_relative_path(name, allow_root=False),
            max_bytes,
        )

    async def enter_tree_cursor_directory(self, parent: str, name: str) -> None:
        """Enter one child already classified as a directory by MLSD."""

        await self.run(
            _ftp_tree_enter_current,
            ftp_relative_path(parent),
            ftp_relative_path(name, allow_root=False),
        )

    async def leave_tree_cursor_directory(self, child: str, parent: str) -> None:
        """Return from one verified child directory to its verified parent."""

        await self.run(
            _ftp_tree_leave_current,
            ftp_relative_path(child),
            ftp_relative_path(parent),
        )

    async def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        _validate_ftp_read_limit(max_bytes)
        return await self.run(_ftp_read_bytes, ftp_relative_path(path, allow_root=False), max_bytes)

    async def read_optional_bytes(self, path: str, *, max_bytes: int) -> bytes | None:
        _validate_ftp_read_limit(max_bytes)
        return await self.run(
            _ftp_read_optional_bytes,
            ftp_relative_path(path, allow_root=False),
            max_bytes,
        )

    async def write_bytes(self, path: str, content: bytes) -> None:
        await self.run(_ftp_write_bytes, ftp_relative_path(path, allow_root=False), bytes(content))

    async def make_directory(self, path: str, *, parents: bool = True) -> None:
        await self.run(_ftp_make_directory, ftp_relative_path(path), parents)

    async def delete_file(self, path: str) -> None:
        await self.run(_ftp_delete_file, ftp_relative_path(path, allow_root=False))

    async def delete_tree(self, path: str, *, keep_root: bool = True) -> None:
        await self.run(_ftp_delete_tree, ftp_relative_path(path, allow_root=False), keep_root)

    async def rename(self, source: str, target: str) -> None:
        await self.run(
            _ftp_rename,
            ftp_relative_path(source, allow_root=False),
            ftp_relative_path(target, allow_root=False),
        )


class _LegacyScopedFtp:
    """Adapter for focused test doubles that predate scoped sessions."""

    def __init__(self, transport: Any, service_id: str) -> None:
        self.transport = transport
        self.service_id = service_id

    @property
    def kind(self) -> FileTransportKind:
        observed = getattr(self.transport, "_observed_transport", {})
        return observed.get(self.service_id, FileTransportKind.FTP)

    async def list_directory(self, path: str) -> tuple[FileEntry, ...]:
        return await self.transport.list_directory(self.service_id, path)

    async def read_bytes(self, path: str, *, max_bytes: int) -> bytes:
        return await self.transport.read_bytes(self.service_id, path, max_bytes=max_bytes)

    async def read_optional_bytes(self, path: str, *, max_bytes: int) -> bytes | None:
        return await self.transport.read_optional_bytes(self.service_id, path, max_bytes=max_bytes)

    async def write_bytes(self, path: str, content: bytes) -> None:
        await self.transport.write_bytes(self.service_id, path, content)

    async def make_directory(self, path: str, *, parents: bool = True) -> None:
        await self.transport.make_directory(self.service_id, path, parents=parents)

    async def delete_file(self, path: str) -> None:
        await self.transport.delete_file(self.service_id, path)

    async def delete_tree(self, path: str, *, keep_root: bool = True) -> None:
        await self.transport.delete_tree(self.service_id, path, keep_root=keep_root)

    async def rename(self, source: str, target: str) -> None:
        await self.transport.rename(self.service_id, source, target)


class NitradoFtpTransport:
    """Bounded FTP/FTPS transport executed on one dedicated worker."""

    def __init__(
        self,
        credential_provider: Callable[[str], Awaitable[NitradoFtpCredentials]],
        *,
        plaintext_consent: Callable[[str], bool] | set[str] | None = None,
        executor: ThreadPoolExecutor | None = None,
        connection_factory: Callable[[NitradoFtpCredentials, bool], FTP] | None = None,
        endpoint_resolver: Callable[[str, int], tuple[str, ...]] | None = None,
        operation_timeout: float = FTP_OPERATION_TIMEOUT_SECONDS,
        drain_timeout: float = FTP_WORKER_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._credential_provider = credential_provider
        self._plaintext_consent = plaintext_consent if plaintext_consent is not None else set()
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="nitrado-ftp")
        self._owns_executor = executor is None
        self._connection_factory = connection_factory
        self._endpoint_resolver = endpoint_resolver or resolve_ftp_endpoint
        self._operation_timeout = operation_timeout
        self._drain_timeout = drain_timeout
        self._closed = False
        self._observed_transport: dict[str, FileTransportKind] = {}
        self._plaintext_required: set[str] = set()
        self._failure_counts: dict[str, int] = {}
        self._last_failure_code: dict[str, str] = {}
        self._last_failure_at: dict[str, float] = {}
        self._capability_proven_at: dict[str, float] = {}
        self._active: set[_WorkerOperation] = set()
        self._active_lock = asyncio.Lock()

    def update_plaintext_consent(self, service_ids: set[str]) -> None:
        """Replace mutable per-service plaintext consent without recreation."""

        self._plaintext_consent = {str(service_id) for service_id in service_ids}

    def observed_status(self, service_id: str) -> dict[str, Any]:
        """Return a no-network, secret-free status snapshot for diagnostics."""

        service_id = str(service_id)
        kind = self._observed_transport.get(service_id)
        return {
            "closed": self._closed,
            "plaintext_consent": self._plaintext_allowed(service_id),
            "plaintext_required": service_id in self._plaintext_required,
            "observed_transport": kind.value if kind is not None else None,
            "secure_transport_observed": kind is FileTransportKind.FTPS,
            "active_operations": len(self._active),
            "failure_count": self._failure_counts.get(service_id, 0),
            "last_failure_code": self._last_failure_code.get(service_id),
        }

    def observed_capabilities(self, service_id: str) -> FileCapabilities | None:
        """Return capabilities only after a transport has actually succeeded."""

        kind = self._observed_transport.get(str(service_id))
        if kind is None:
            return None
        return FileCapabilities(
            read_bytes=True,
            write_file=True,
            recursive_read=True,
            recursive_write=True,
            recursive_delete=True,
            rename=True,
            secure_transport=kind is FileTransportKind.FTPS,
            transport=kind,
        )

    def _plaintext_allowed(self, service_id: str) -> bool:
        if callable(self._plaintext_consent):
            return bool(self._plaintext_consent(str(service_id)))
        return str(service_id) in self._plaintext_consent

    async def async_close(self) -> None:
        """Reject new work, abort active sockets, and drain the worker."""

        self._closed = True
        async with self._active_lock:
            active = tuple(self._active)
        for operation in active:
            operation.abort()
        if self._owns_executor:
            try:
                async with asyncio.timeout(self._drain_timeout):
                    await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
            except TimeoutError as err:
                raise FileTransportError("Nitrado FTP worker could not be drained safely") from err

    async def capabilities(self, service_id: str) -> FileCapabilities:
        service_id = str(service_id)
        now = time.monotonic()
        observed = self.observed_capabilities(service_id)
        proven_at = self._capability_proven_at.get(service_id)
        if observed is not None and proven_at is not None and now - proven_at < FTP_CAPABILITY_CACHE_SECONDS:
            return observed
        failure_count = self._failure_counts.get(service_id, 0)
        failed_at = self._last_failure_at.get(service_id)
        if failure_count and failed_at is not None:
            backoff = min(2 ** min(failure_count - 1, 6), FTP_CAPABILITY_FAILURE_BACKOFF_MAX_SECONDS)
            if now - failed_at < backoff:
                raise FileTransportError("Nitrado FTP capability probe is temporarily backed off after failure")
        async with self.session(service_id):
            pass
        self._clear_failure(service_id)
        observed = self.observed_capabilities(service_id)
        if observed is None:  # pragma: no cover - a successful session records its transport.
            raise FileTransportError("Nitrado FTP capabilities could not be proven")
        return observed

    async def list_directory(self, service_id: str, path: str) -> tuple[FileEntry, ...]:
        return await self._run(service_id, _ftp_list_directory, ftp_relative_path(path))

    async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
        _validate_ftp_read_limit(max_bytes)
        return await self._run(service_id, _ftp_read_bytes, ftp_relative_path(path, allow_root=False), max_bytes)

    async def read_optional_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes | None:
        """Read a file or prove its absence with a same-session parent listing."""

        _validate_ftp_read_limit(max_bytes)
        return await self._run(
            service_id,
            _ftp_read_optional_bytes,
            ftp_relative_path(path, allow_root=False),
            max_bytes,
        )

    async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
        await self._run(service_id, _ftp_write_bytes, ftp_relative_path(path, allow_root=False), bytes(content))

    async def make_directory(self, service_id: str, path: str, *, parents: bool = True) -> None:
        await self._run(service_id, _ftp_make_directory, ftp_relative_path(path), parents)

    async def delete_file(self, service_id: str, path: str) -> None:
        await self._run(service_id, _ftp_delete_file, ftp_relative_path(path, allow_root=False))

    async def delete_tree(self, service_id: str, path: str, *, keep_root: bool = True) -> None:
        relative = ftp_relative_path(path, allow_root=False)
        await self._run(service_id, _ftp_delete_tree, relative, keep_root)

    async def rename(self, service_id: str, source: str, target: str) -> None:
        await self._run(
            service_id,
            _ftp_rename,
            ftp_relative_path(source, allow_root=False),
            ftp_relative_path(target, allow_root=False),
        )

    async def _credentials(self, service_id: str) -> tuple[NitradoFtpCredentials, tuple[str, ...]]:
        if self._closed:
            raise FileTransportError("Nitrado FTP transport is closed")
        credentials = await self._credential_provider(str(service_id))
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._executor,
            self._endpoint_resolver,
            credentials.hostname,
            credentials.port,
        )
        try:
            addresses = await asyncio.wait_for(future, timeout=self._operation_timeout)
        except TimeoutError as err:
            raise FileTransportError("Nitrado FTP endpoint resolution exceeded its execution budget") from err
        return credentials, addresses

    @asynccontextmanager
    async def session(self, service_id: str):
        """Open one authenticated session for an entire logical transaction."""

        service_id = str(service_id)
        try:
            credentials, addresses = await self._credentials(service_id)
        except BaseException:
            self._record_failure(service_id, "credentials_or_endpoint_unavailable")
            raise
        try:
            scoped = await self._open_session(service_id, credentials, addresses)
        except _CredentialRejected:
            try:
                credentials, addresses = await self._credentials(service_id)
                scoped = await self._open_session(service_id, credentials, addresses)
            except _CredentialRejected as err:
                self._record_failure(service_id, "credentials_rejected")
                raise FileTransportError("Nitrado rejected refreshed FTP credentials") from err
            except _PlaintextConsentRequired:
                self._record_failure(service_id, "plaintext_consent_required")
                raise
            except BaseException:
                self._record_failure(service_id, "credentials_or_endpoint_unavailable")
                raise
        except _PlaintextConsentRequired:
            self._record_failure(service_id, "plaintext_consent_required")
            raise
        except BaseException:
            self._record_failure(service_id, "transport_unavailable")
            raise
        try:
            yield scoped
        finally:
            await asyncio.shield(self._close_session(scoped))

    def _record_failure(self, service_id: str, code: str) -> None:
        service_id = str(service_id)
        if self._last_failure_code.get(service_id) == code:
            self._failure_counts[service_id] = self._failure_counts.get(service_id, 0) + 1
        else:
            self._last_failure_code[service_id] = code
            self._failure_counts[service_id] = 1
        self._last_failure_at[service_id] = time.monotonic()

    def _clear_failure(self, service_id: str) -> None:
        service_id = str(service_id)
        self._failure_counts.pop(service_id, None)
        self._last_failure_code.pop(service_id, None)
        self._last_failure_at.pop(service_id, None)

    async def _run(self, service_id: str, callback: Callable[..., _T], *args: Any) -> _T:
        async with self.session(service_id) as scoped:
            return await scoped.run(callback, *args)

    async def _open_session(
        self,
        service_id: str,
        credentials: NitradoFtpCredentials,
        addresses: tuple[str, ...],
    ) -> _ScopedFtpSession:
        if self._closed:
            raise FileTransportError("Nitrado FTP transport is closed")
        operation = _WorkerOperation()
        async with self._active_lock:
            self._active.add(operation)
        loop = asyncio.get_running_loop()

        def worker() -> _FtpSession:
            session = self._connect(
                credentials,
                addresses,
                plaintext_allowed=self._plaintext_allowed(service_id),
            )
            try:
                operation.attach(session.ftp)
            except BaseException:
                _close_ftp(session.ftp)
                raise
            return session

        future = loop.run_in_executor(self._executor, worker)
        future.add_done_callback(_consume_future_exception)
        try:
            done, pending = await asyncio.wait({future}, timeout=self._operation_timeout)
            if pending:
                operation.abort()
                await _drain_future(future, self._drain_timeout)
                raise FileTransportError("Nitrado FTP operation exceeded its execution budget")
            session = next(iter(done)).result()
        except asyncio.CancelledError as cancelled:
            operation.abort()
            try:
                await asyncio.shield(_drain_future(future, self._drain_timeout))
            except BaseException:  # noqa: BLE001 - cancellation must dominate every late worker failure.
                # Cancellation owns this path. A late worker failure must not
                # turn it into an authentication retry or revive the request.
                pass
            finally:
                self._discard_operation_when_done(loop, future, operation)
            raise cancelled
        except _CredentialRejected:
            self._discard_operation_when_done(loop, future, operation)
            raise
        except _PlaintextConsentRequired:
            self._plaintext_required.add(str(service_id))
            self._discard_operation_when_done(loop, future, operation)
            raise
        except FileTransportError:
            self._discard_operation_when_done(loop, future, operation)
            raise
        except FTP_ERRORS as err:
            self._discard_operation_when_done(loop, future, operation)
            raise FileTransportError(f"Nitrado FTP operation failed: {type(err).__name__}") from err
        except BaseException:
            self._discard_operation_when_done(loop, future, operation)
            raise
        self._observed_transport[str(service_id)] = session.kind
        self._capability_proven_at[str(service_id)] = time.monotonic()
        if session.kind is FileTransportKind.FTPS:
            self._plaintext_required.discard(str(service_id))
        return _ScopedFtpSession(self, str(service_id), session, operation)

    def _discard_operation_when_done(
        self,
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[Any],
        operation: _WorkerOperation,
    ) -> None:
        """Retain failed connection work in diagnostics until its worker exits."""

        async def discard() -> None:
            async with self._active_lock:
                self._active.discard(operation)

        def completed(_future: asyncio.Future[Any]) -> None:
            if loop.is_closed():
                return
            loop.create_task(discard())

        if future.done():
            completed(future)
        else:
            future.add_done_callback(completed)

    async def _run_scoped_callback(
        self,
        scoped: _ScopedFtpSession,
        callback: Callable[..., _T],
        *args: Any,
    ) -> _T:
        if self._closed:
            raise FileTransportError("Nitrado FTP transport is closed")
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, callback, scoped.session, *args)
        future.add_done_callback(_consume_future_exception)
        try:
            done, pending = await asyncio.wait({future}, timeout=self._operation_timeout)
            if pending:
                scoped.operation.abort()
                await _drain_future(future, self._drain_timeout)
                raise FileTransportError("Nitrado FTP operation exceeded its execution budget")
            result = next(iter(done)).result()
            self._clear_failure(scoped.service_id)
            return result
        except asyncio.CancelledError:
            scoped.operation.abort()
            await _drain_future(future, self._drain_timeout)
            raise
        except FileTransportError:
            self._record_failure(scoped.service_id, "transport_operation_failed")
            raise
        except FTP_ERRORS as err:
            self._record_failure(scoped.service_id, "transport_operation_failed")
            raise FileTransportError(f"Nitrado FTP operation failed: {type(err).__name__}") from err

    async def _close_session(self, scoped: _ScopedFtpSession) -> None:
        if scoped.closed:
            return
        scoped.closed = True
        scoped.operation.detach()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, _close_ftp, scoped.session.ftp)
        future.add_done_callback(_consume_future_exception)
        await _drain_future(future, self._drain_timeout)
        async with self._active_lock:
            self._active.discard(scoped.operation)

    def _connect(
        self,
        credentials: NitradoFtpCredentials,
        addresses: tuple[str, ...],
        *,
        plaintext_allowed: bool,
    ) -> _FtpSession:
        if self._connection_factory is not None:
            secure = credentials.secure_hint is not False
            if not secure and not plaintext_allowed:
                raise _PlaintextConsentRequired("Plaintext Nitrado FTP requires administrator consent")
            ftp = self._connection_factory(credentials, secure)
            kind = FileTransportKind.FTPS if secure else FileTransportKind.FTP
            return _finish_login(ftp, credentials, kind)

        # Always probe explicit FTPS first.  Provider payloads often label the
        # endpoint merely as "ftp" even when AUTH TLS is available; that label
        # is not authority to send credentials in plaintext.
        try:
            return self._connect_ftps(credentials, addresses)
        except ssl.SSLCertVerificationError as err:
            # Certificate failures are active security failures.  They never
            # trigger a downgrade, even when plaintext consent exists.
            raise FileTransportSecurityError("Nitrado FTPS certificate validation failed") from err
        except (ssl.SSLError, EOFError, OSError, error_perm) as err:
            if _is_login_rejection(err):
                raise _CredentialRejected from err
            if not plaintext_allowed:
                raise _PlaintextConsentRequired(
                    "Secure Nitrado FTP could not be established; plaintext FTP requires administrator consent"
                ) from err
            # Consent is explicit.  Protocol negotiation failures may fall
            # back, but certificate validation failures above may not.
            return self._connect_plaintext(credentials, addresses)

    def _connect_ftps(self, credentials: NitradoFtpCredentials, addresses: tuple[str, ...]) -> _FtpSession:
        last_error: BaseException | None = None
        for address in addresses:
            ftp = _PinnedFTP_TLS(context=ssl.create_default_context(), timeout=FTP_CONNECT_TIMEOUT_SECONDS)
            try:
                ftp.connect_pinned(credentials.hostname, address, credentials.port, FTP_CONNECT_TIMEOUT_SECONDS)
                ftp.auth()
                ftp.login(credentials.username, credentials.password)
                ftp.prot_p()
                ftp.set_pasv(True)
                return _finish_login(ftp, credentials, FileTransportKind.FTPS, already_logged_in=True)
            except ssl.SSLCertVerificationError:
                _close_ftp(ftp)
                raise
            except (ssl.SSLError, EOFError, OSError, error_perm) as err:
                _close_ftp(ftp)
                if _is_login_rejection(err):
                    raise _CredentialRejected from err
                last_error = err
        if last_error is None:
            raise FileTransportError("Nitrado FTP endpoint did not resolve to an address")
        raise last_error

    def _connect_plaintext(
        self,
        credentials: NitradoFtpCredentials,
        addresses: tuple[str, ...],
    ) -> _FtpSession:
        last_error: BaseException | None = None
        for address in addresses:
            ftp = _PinnedFTP(timeout=FTP_CONNECT_TIMEOUT_SECONDS)
            try:
                ftp.connect_pinned(credentials.hostname, address, credentials.port, FTP_CONNECT_TIMEOUT_SECONDS)
                ftp.login(credentials.username, credentials.password)
                ftp.set_pasv(True)
                return _finish_login(ftp, credentials, FileTransportKind.FTP, already_logged_in=True)
            except (EOFError, OSError, error_perm) as err:
                _close_ftp(ftp)
                if _is_login_rejection(err):
                    raise _CredentialRejected from err
                last_error = err
        if last_error is None:
            raise FileTransportError("Nitrado FTP endpoint did not resolve to an address")
        raise last_error


@dataclass(slots=True)
class NitradoFilesystemService:
    """Service-bound provider filesystem orchestrator owned by Nitrado core."""

    client: NitradoClient
    allow_plaintext_ftp_service_ids: set[str] = field(default_factory=set)
    account_entry_id: str | None = None
    journal: FilesystemTransactionJournal | None = None
    http: NitradoHttpFileTransport = field(init=False)
    ftp: NitradoFtpTransport = field(init=False)
    _mutation_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _closed: bool = False
    last_operation: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.http = NitradoHttpFileTransport(self.client)
        self.ftp = NitradoFtpTransport(
            self._fetch_ftp_credentials,
            plaintext_consent=self.allow_plaintext_ftp_service_ids,
        )

    async def _fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
        provider = getattr(self.client, "fetch_ftp_credentials", None)
        if not callable(provider):
            raise FileTransportError("Nitrado FTP is unavailable for this provider client")
        credentials = await provider(service_id)
        if not isinstance(credentials, NitradoFtpCredentials):
            raise FileTransportError("Nitrado FTP credential provider returned an invalid result")
        return credentials

    def update_plaintext_ftp_consent(self, service_ids: set[str]) -> None:
        """Apply per-service administrator consent without recreating transport."""

        self.allow_plaintext_ftp_service_ids = {str(service_id) for service_id in service_ids}
        self.ftp.update_plaintext_consent(self.allow_plaintext_ftp_service_ids)

    def observed_status(self, service_id: str) -> dict[str, Any]:
        """Return a no-network capability/status snapshot."""

        ftp = self.ftp.observed_status(service_id)
        return {
            "http": self.http.observed_status(service_id),
            "ftp": ftp,
        }

    def invalidate_provider_state(self, service_id: str) -> None:
        """Forget transport facts invalidated by a provider-side restore."""

        service_id = str(service_id)
        self.http._root_prefixes.pop(service_id, None)
        self.http._mapping_locks.pop(service_id, None)
        self.ftp._observed_transport.pop(service_id, None)
        self.ftp._plaintext_required.discard(service_id)
        self.ftp._failure_counts.pop(service_id, None)
        self.ftp._last_failure_code.pop(service_id, None)
        self.ftp._last_failure_at.pop(service_id, None)
        self.ftp._capability_proven_at.pop(service_id, None)

    async def fetch_players(self, service_id: str) -> tuple[str, ...]:
        """Delegate provider player reads without exposing the mutable client."""

        self._ensure_open()
        return await self.client.fetch_players(service_id)

    async def fetch_external_json(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
        headers: dict[str, str] | None = None,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        """Delegate bounded external JSON reads through core's HTTP policy."""

        self._ensure_open()
        return await self.client.fetch_external_json(
            url,
            allowed_hosts=allowed_hosts,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )

    async def async_close(self) -> None:
        self._closed = True
        await self.ftp.async_close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise FileTransportError("Nitrado filesystem service is closed")

    @property
    def supports_authoritative_file_transactions(self) -> bool:
        """Return whether the provider client exposes Nitrado FTP credentials."""

        return callable(getattr(self.client, "fetch_ftp_credentials", None))

    async def list_files(self, service_id: str, directory: str | None = None) -> dict[str, Any]:
        """Compatibility entry point for existing profile API version 1."""

        self._ensure_open()
        async with self.authoritative_read(service_id):
            return await self._list_files_unlocked(service_id, directory)

    async def _list_files_unlocked(self, service_id: str, directory: str | None) -> dict[str, Any]:
        """List files while the caller owns the authoritative-read gate."""

        relative = "" if directory is None else canonical_relative_path(directory)
        provider_directory = None if directory is None else await self.http.provider_path(service_id, relative)
        payload = await self.http.list_raw(service_id, provider_directory)
        # Profiles receive only service-root-relative paths.  They never get an
        # account-specific provider prefix that could later be confused with a
        # different service's FTP root.
        return await self.http.relative_payload(service_id, payload)

    async def download_file(self, service_id: str, path: str) -> str:
        """Compatibility entry point for existing bounded text reads."""

        self._ensure_open()
        async with self.authoritative_read(service_id):
            return await self._download_file_unlocked(service_id, path)

    async def _download_file_unlocked(self, service_id: str, path: str) -> str:
        """Download text while the caller owns the authoritative-read gate."""

        if not callable(getattr(self.client, "list_files", None)):
            return await self.client.download_file(service_id, path)
        try:
            return await self.http.read_text(service_id, path)
        except NitradoApiError:
            # The provider HTTP file browser is a convenient first witness but
            # has repeatedly returned transient 400/500/502 failures. Keep the
            # Profile API v1 contract useful by falling back to the core-owned
            # authoritative transport without exposing FTP to the profile.
            return await self._download_text_file_authoritative_unlocked(service_id, path)

    async def download_text_file_authoritative(self, service_id: str, path: str) -> str:
        """Read UTF-8 text through the same FTP authority used for mutation."""

        self._ensure_open()
        async with self.authoritative_read(service_id):
            return await self._download_text_file_authoritative_unlocked(service_id, path)

    async def _download_text_file_authoritative_unlocked(self, service_id: str, path: str) -> str:
        """Read UTF-8 text while the caller owns the authoritative-read gate."""

        relative = await self._relative_ftp_path(service_id, path)
        content = await self.ftp.read_bytes(
            service_id,
            relative,
            max_bytes=DEFAULT_MAX_BINARY_BYTES,
        )
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as err:
            raise FileTransportError("Editable file is not valid UTF-8 text") from err

    async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
        """Compatibility entry point for existing verified text transactions."""

        self._ensure_open()
        if not self.supports_authoritative_file_transactions:
            if not callable(getattr(self.client, "list_files", None)):
                # Narrow compatibility for pure test doubles and older
                # service-bound clients that already accept an authoritative
                # provider path but cannot establish an HTTP root mapping.
                await self.client.upload_text_file(service_id, path, content)
                return
            # Keep legacy HTTP fallback inside the same proven service-root
            # mapping used for reads. Passing a profile-relative path directly
            # to Nitrado's upload endpoint can write somewhere different from
            # the subsequent provider-path readback.
            await self.http.write_text(service_id, path, content)
            return
        # Existing editable-file plumbing creates remote backup siblings as
        # well as replacing targets.  FTPS is the only transport here that can
        # prove target absence and delete a partial creation during rollback.
        await self.write_bytes_verified(
            service_id,
            path,
            content.encode("utf-8"),
            require_ftp=True,
        )

    async def write_text_compare_and_swap(
        self,
        service_id: str,
        path: str,
        content: str,
        *,
        expected_content: str,
        backup_path: str | None = None,
    ) -> FileOperationResult:
        """Refuse stale previews and perform one verified FTP transaction."""

        return await self.write_bytes_verified(
            service_id,
            path,
            content.encode("utf-8"),
            require_ftp=True,
            expected_content=expected_content.encode("utf-8"),
            backup_path=backup_path,
        )

    async def capabilities(self, service_id: str) -> tuple[FileCapabilities, ...]:
        """Return conservative HTTP and FTP capability snapshots."""

        self._ensure_open()
        capabilities = [await self.http.capabilities(service_id)]
        with suppress(NitradoApiError):
            capabilities.append(await self.ftp.capabilities(service_id))
        return tuple(capabilities)

    @asynccontextmanager
    async def mutation(self, service_id: str):
        """Serialize every filesystem mutation for one provider service."""

        self._ensure_open()
        lock = self._mutation_locks.setdefault(str(service_id), asyncio.Lock())
        async with lock:
            self._ensure_open()
            yield

    @asynccontextmanager
    async def authoritative_read(self, service_id: str):
        """Keep one authoritative read outside any partial tree mutation."""

        self._ensure_open()
        lock = self._mutation_locks.setdefault(str(service_id), asyncio.Lock())
        async with lock:
            self._ensure_open()
            yield

    @asynccontextmanager
    async def _ftp_session(self, service_id: str):
        """Yield one transaction-scoped FTP facade, including test doubles."""

        session_factory = getattr(self.ftp, "session", None)
        if callable(session_factory):
            async with session_factory(service_id) as scoped:
                yield scoped
            return
        yield _LegacyScopedFtp(self.ftp, str(service_id))

    async def read_bytes(
        self,
        service_id: str,
        path: str,
        *,
        max_bytes: int = DEFAULT_MAX_BINARY_BYTES,
        require_ftp: bool = False,
    ) -> bytes:
        """Read bounded content through the appropriate provider transport."""

        self._ensure_open()
        if max_bytes < 1 or max_bytes > DEFAULT_MAX_BINARY_BYTES:
            raise FileTransportError("File read limit is outside the supported range")
        if require_ftp:
            relative = await self._relative_ftp_path(service_id, path)
            return await self.ftp.read_bytes(service_id, relative, max_bytes=max_bytes)
        try:
            return await self.http.read_bytes(service_id, path, max_bytes=max_bytes)
        except NitradoApiError:
            relative = await self._relative_ftp_path(service_id, path)
            return await self.ftp.read_bytes(service_id, relative, max_bytes=max_bytes)

    async def _relative_ftp_path(self, service_id: str, path: str | RemotePath) -> str:
        del service_id
        # FTP mutation authority accepts only the public profile contract:
        # strict paths relative to the authenticated service root.  Legacy HTTP
        # provider prefixes are deliberately not used to select an FTP target.
        return canonical_relative_path(path)

    async def write_bytes_verified(
        self,
        service_id: str,
        path: str,
        content: bytes,
        *,
        require_ftp: bool = False,
        expected_content: bytes | object = _EXPECTED_CONTENT_UNSET,
        backup_path: str | None = None,
    ) -> FileOperationResult:
        """Write one file, verify it exactly, and roll back on failure."""

        self._ensure_open()
        data = bytes(content)
        if len(data) > DEFAULT_MAX_BINARY_BYTES:
            raise FileTransportError("File write exceeds the configured limit")
        async with self.mutation(service_id):
            await self._ensure_no_unresolved_transaction(service_id)
            if require_ftp:
                target = await self._relative_ftp_path(service_id, path)
                backup = await self._relative_ftp_path(service_id, backup_path) if backup_path is not None else None
                async with self._ftp_session(service_id) as ftp_session:
                    previous = await ftp_session.read_optional_bytes(
                        target,
                        max_bytes=DEFAULT_MAX_BINARY_BYTES,
                    )
                    if expected_content is not _EXPECTED_CONTENT_UNSET and previous != expected_content:
                        raise FileConcurrentModificationError("Remote file changed after preview; mutation was refused")
                    if backup is not None:
                        if previous is None:
                            raise FileVerificationError("Cannot create a backup for an absent target")
                        if await ftp_session.read_optional_bytes(backup, max_bytes=1) is not None:
                            raise FileVerificationError("Generated editable-file backup already exists")
                        await ftp_session.write_bytes(backup, previous)
                        backup_readback = await ftp_session.read_bytes(
                            backup,
                            max_bytes=len(previous) + 1,
                        )
                        if backup_readback != previous:
                            raise FileVerificationError("Editable-file backup readback did not match")
                        latest = await ftp_session.read_optional_bytes(
                            target,
                            max_bytes=DEFAULT_MAX_BINARY_BYTES,
                        )
                        if latest != previous:
                            raise FileConcurrentModificationError("Remote file changed while its backup was created")
                    record = await self._journal_prepare_file(service_id, target, previous)
                    transaction_id = (
                        record.transaction_id if record is not None else new_transaction_id(service_id, "write")
                    )
                    await self._journal_mark_mutating(record)
                    try:
                        await self._write_ftp_transaction(
                            ftp_session,
                            target,
                            data,
                            transaction_id,
                            previous,
                        )
                    except BaseException as err:
                        await self._journal_record_rollback(record, previous, err)
                        raise
                    kind = ftp_session.kind
            else:
                target = await self.http.relative_path(service_id, path)
                previous = await self.http.read_bytes(
                    service_id,
                    target,
                    max_bytes=DEFAULT_MAX_BINARY_BYTES,
                )
                record = await self._journal_prepare_file(service_id, target, previous)
                transaction_id = (
                    record.transaction_id if record is not None else new_transaction_id(service_id, "write")
                )
                await self._journal_mark_mutating(record)
                try:
                    await self._write_http_transaction(service_id, target, data, previous)
                except (Exception, asyncio.CancelledError) as err:
                    await self._journal_record_rollback(record, previous, err)
                    raise
                kind = FileTransportKind.HTTP
            await self._journal_record_commit(record, data)
        result = FileOperationResult(
            transaction_id,
            "write_file",
            kind,
            VerificationStrength.SAME_TRANSPORT,
            recovery_available=True,
        )
        self.last_operation = operation_diagnostics(result)
        return result

    async def _write_http_transaction(
        self,
        service_id: str,
        path: str,
        data: bytes,
        previous: bytes,
    ) -> None:
        try:
            await self.http.write_bytes(service_id, path, data)
            remote = await self.http.read_bytes(service_id, path, max_bytes=len(data) + 1)
            if remote != data:
                raise FileVerificationError("Provider acknowledged the write but exact readback failed")
        except BaseException as err:
            try:
                await asyncio.shield(self.http.write_bytes(service_id, path, previous))
                restored = await asyncio.shield(self.http.read_bytes(service_id, path, max_bytes=len(previous) + 1))
                if restored != previous:
                    raise FileVerificationError("HTTP rollback readback did not match")
            except BaseException as rollback_err:
                raise FileRecoveryError("HTTP file write failed and rollback could not be verified") from rollback_err
            raise err

    async def _write_ftp_transaction(
        self,
        ftp_session: _ScopedFtpSession | _LegacyScopedFtp,
        path: str,
        data: bytes,
        transaction_id: str,
        previous: bytes | None,
    ) -> None:
        target = ftp_relative_path(path, allow_root=False)
        suffix = transaction_id[:12]
        temporary = _sibling_path(target, f".nitrado-upload-{suffix}")
        backup = _sibling_path(target, f".nitrado-backup-{suffix}")
        try:
            await ftp_session.write_bytes(temporary, data)
            staged = await ftp_session.read_bytes(temporary, max_bytes=len(data) + 1)
            if staged != data:
                raise FileVerificationError("Staged FTP upload did not match proposed content")
            if previous is not None:
                await ftp_session.rename(target, backup)
            await ftp_session.rename(temporary, target)
            remote = await ftp_session.read_bytes(target, max_bytes=len(data) + 1)
            if remote != data:
                raise FileVerificationError("Final FTP readback did not match proposed content")
            if previous is not None:
                await ftp_session.delete_file(backup)
        except BaseException:
            with suppress(FileTransportError, asyncio.CancelledError):
                await asyncio.shield(ftp_session.delete_file(temporary))
            try:
                await asyncio.shield(self._restore_ftp_backup(ftp_session, target, backup, previous))
            except BaseException as rollback_err:
                raise FileRecoveryError("FTP file write failed and rollback could not be verified") from rollback_err
            raise

    async def _restore_ftp_backup(
        self,
        ftp_session: _ScopedFtpSession | _LegacyScopedFtp,
        target: str,
        backup: str,
        previous: bytes | None,
    ) -> None:
        with suppress(FileTransportError, asyncio.CancelledError):
            await ftp_session.delete_file(target)
        if previous is None:
            restored = await ftp_session.read_optional_bytes(target, max_bytes=1)
            if restored is not None:
                raise FileVerificationError("FTP rollback could not restore exact target absence")
            return
        try:
            await ftp_session.rename(backup, target)
        except FileTransportError:
            await ftp_session.write_bytes(target, previous)
            with suppress(FileTransportError):
                await ftp_session.delete_file(backup)
        restored = await ftp_session.read_bytes(target, max_bytes=len(previous) + 1)
        if restored != previous:
            raise FileVerificationError("FTP rollback readback did not match original content")

    async def snapshot_tree(
        self,
        service_id: str,
        root: str,
        *,
        max_files: int = DEFAULT_MAX_TREE_FILES,
        max_bytes: int = DEFAULT_MAX_TREE_BYTES,
        max_depth: int = DEFAULT_MAX_TREE_DEPTH,
        max_directories: int = DEFAULT_MAX_TREE_DIRECTORIES,
        excluded_paths: Iterable[str] = (),
        progress: Callable[[str, Mapping[str, Any]], None] | None = None,
        _session: _ScopedFtpSession | _LegacyScopedFtp | None = None,
    ) -> FileTreeSnapshot:
        """Download one exact bounded tree through FTP/FTPS."""

        self._ensure_open()
        if _session is None:
            async with self._ftp_session(service_id) as ftp_session:
                return await self.snapshot_tree(
                    service_id,
                    root,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_depth=max_depth,
                    max_directories=max_directories,
                    excluded_paths=excluded_paths,
                    progress=progress,
                    _session=ftp_session,
                )
        # Verification callers use a one-unit sentinel above the accepted
        # ceiling so an oversized remote tree can be observed and rejected.
        if not 1 <= max_files <= DEFAULT_MAX_TREE_FILES + 1:
            raise FileTransportError("Tree snapshot file-count limit is outside the supported range")
        if not 1 <= max_bytes <= DEFAULT_MAX_TREE_BYTES + 1:
            raise FileTransportError("Tree snapshot byte limit is outside the supported range")
        if not 0 <= max_depth <= DEFAULT_MAX_TREE_DEPTH:
            raise FileTransportError("Tree snapshot depth limit is outside the supported range")
        if not 1 <= max_directories <= DEFAULT_MAX_TREE_DIRECTORIES:
            raise FileTransportError("Tree snapshot directory-count limit is outside the supported range")
        try:
            excluded = frozenset(canonical_relative_path(path, allow_root=False).casefold() for path in excluded_paths)
        except FileTransportSecurityError as err:
            raise FileTransportError("Tree snapshot exclusion path is unsafe") from err

        def is_excluded(path: str) -> bool:
            alias = path.casefold()
            return any(alias == item or alias.startswith(f"{item}/") for item in excluded)

        def report(event: str, **details: Any) -> None:
            if progress is not None:
                progress(event, details)

        files = SpoolTreeFiles()
        manifest_entries: list[FileManifestEntry] = []
        total_bytes = 0
        directories_seen = 0

        cursor_supported = all(
            callable(getattr(_session, name, None))
            for name in (
                "position_tree_cursor",
                "list_tree_cursor",
                "read_tree_cursor_file",
                "enter_tree_cursor_directory",
                "leave_tree_cursor_directory",
            )
        )

        async def walk(remote: str, relative: str, depth: int) -> None:
            nonlocal directories_seen, total_bytes
            if depth > max_depth:
                raise FileTransportError("Remote tree exceeded the configured depth limit")
            directories_seen += 1
            if directories_seen > max_directories:
                raise FileTransportError("Remote tree exceeded the configured directory-count limit")
            if cursor_supported:
                directory_entries = await _session.list_tree_cursor(remote)  # type: ignore[union-attr]
            else:
                directory_entries = await _session.list_directory(remote)
            report(
                "directory_scanned",
                directories=directories_seen,
                files=len(manifest_entries),
                bytes=total_bytes,
            )
            for entry in directory_entries:
                child_relative = canonical_relative_path(posixpath.join(relative, entry.name), allow_root=False)
                child_remote = join_remote_path(remote, entry.name)
                if is_excluded(child_relative):
                    report(
                        "path_excluded",
                        path=child_relative,
                        directories=directories_seen,
                        files=len(manifest_entries),
                        bytes=total_bytes,
                    )
                    continue
                if entry.entry_type is FileEntryType.DIRECTORY:
                    if cursor_supported:
                        await _session.enter_tree_cursor_directory(remote, entry.name)  # type: ignore[union-attr]
                        await walk(child_remote, child_relative, depth + 1)
                        await _session.leave_tree_cursor_directory(  # type: ignore[union-attr]
                            child_remote,
                            remote,
                        )
                    else:
                        await walk(child_remote, child_relative, depth + 1)
                    continue
                if len(manifest_entries) >= max_files:
                    raise FileTransportError("Remote tree exceeded the configured file-count limit")
                remaining = max_bytes - total_bytes
                if remaining < 0:
                    raise FileTransportError("Remote tree exceeded the configured byte limit")
                read_limit = min(remaining + 1, DEFAULT_MAX_BINARY_BYTES)
                report(
                    "file_started",
                    path=child_relative,
                    directories=directories_seen,
                    files=len(manifest_entries),
                    bytes=total_bytes,
                )
                if cursor_supported:
                    blob = await _session.read_tree_cursor_file(  # type: ignore[union-attr]
                        remote,
                        entry.name,
                        max_bytes=read_limit,
                    )
                else:
                    blob = await _session.read_bytes(child_remote, max_bytes=read_limit)
                total_bytes += len(blob)
                if total_bytes > max_bytes:
                    raise FileTransportError("Remote tree exceeded the configured byte limit")
                manifest_entries.append(files.append(child_relative, blob))
                report(
                    "file_downloaded",
                    path=child_relative,
                    directories=directories_seen,
                    files=len(manifest_entries),
                    bytes=total_bytes,
                )

        try:
            if cursor_supported:
                await _session.position_tree_cursor(root)  # type: ignore[union-attr]
            await walk(root, "", 0)
            manifest = FileTreeManifest(
                tuple(sorted(manifest_entries, key=lambda item: item.path)),
                total_bytes,
            )
            return FileTreeSnapshot(files, manifest)
        except BaseException:
            files.close()
            raise

    async def verify_tree(
        self,
        service_id: str,
        root: str,
        expected: FileTreeManifest,
        *,
        _session: _ScopedFtpSession | _LegacyScopedFtp | None = None,
    ) -> FileTreeManifest:
        """Prove exact expected files and absence of unexpected files."""

        snapshot = await self.snapshot_tree(
            service_id,
            root,
            max_files=max(len(expected.entries) + 1, 1),
            max_bytes=max(expected.total_bytes + 1, 1),
            _session=_session,
        )
        try:
            if snapshot.manifest != expected:
                raise FileVerificationError("Remote tree does not match the expected exact manifest")
            return snapshot.manifest
        finally:
            snapshot.close()

    async def replace_tree(
        self,
        service_id: str,
        root: str,
        files: Mapping[str, bytes],
        *,
        expected_current: FileTreeManifest | None = None,
        require_stopped: Callable[[], Awaitable[None]] | None = None,
        mutation_started: Callable[[], None] | None = None,
    ) -> FileOperationResult:
        """Replace one bounded tree through FTP with verified in-memory rollback."""

        self._ensure_open()
        proposed = {canonical_relative_path(path, allow_root=False): bytes(blob) for path, blob in files.items()}
        expected = FileTreeManifest.from_files(proposed)
        _validate_manifest_limits(expected)
        if require_stopped is not None:
            await require_stopped()
        transaction_id = new_transaction_id(service_id, "replace_tree")
        async with self.mutation(service_id):
            await self._ensure_no_unresolved_transaction(service_id)
            if require_stopped is not None:
                await require_stopped()
            async with self._ftp_session(service_id) as ftp_session:
                previous = await self.snapshot_tree(service_id, root, _session=ftp_session)
                try:
                    if expected_current is not None and previous.manifest != expected_current:
                        raise FileConcurrentModificationError(
                            "Remote tree changed after planning; replacement was refused"
                        )
                    record = await self._journal_prepare_tree(service_id, root, previous)
                    if record is not None:
                        transaction_id = record.transaction_id
                    await self._journal_mark_mutating(record)
                    if mutation_started is not None:
                        try:
                            mutation_started()
                        except BaseException as err:
                            # Authority was revoked before the first provider
                            # mutation. Resolve the prepared journal without
                            # touching the remote tree.
                            await self._journal_record_tree_rollback(record, previous.manifest, err)
                            raise
                    try:
                        await ftp_session.delete_tree(root, keep_root=True)
                        await self._upload_tree(service_id, root, proposed, _session=ftp_session)
                        await self.verify_tree(service_id, root, expected, _session=ftp_session)
                    except BaseException as err:
                        try:
                            await asyncio.shield(ftp_session.delete_tree(root, keep_root=True))
                            await asyncio.shield(
                                self._upload_tree(service_id, root, previous.files, _session=ftp_session)
                            )
                            await asyncio.shield(
                                self.verify_tree(service_id, root, previous.manifest, _session=ftp_session)
                            )
                        except BaseException as restore_err:
                            await self._journal_mark_rollback_required(record, restore_err)
                            raise FileRecoveryError(
                                f"Tree replacement {transaction_id} failed and rollback could not be verified"
                            ) from restore_err
                        await self._journal_record_tree_rollback(record, previous.manifest, err)
                        raise err
                    await self._journal_record_tree_commit(record, expected)
                finally:
                    previous.close()
        result = FileOperationResult(
            transaction_id,
            "replace_tree",
            self.ftp._observed_transport.get(str(service_id), FileTransportKind.FTP),
            VerificationStrength.SAME_TRANSPORT,
            expected,
            recovery_available=True,
        )
        self.last_operation = operation_diagnostics(result)
        return result

    async def _upload_tree(
        self,
        service_id: str,
        root: str,
        files: Mapping[str, bytes],
        *,
        _session: _ScopedFtpSession | _LegacyScopedFtp | None = None,
    ) -> None:
        if _session is None:
            async with self._ftp_session(service_id) as ftp_session:
                await self._upload_tree(service_id, root, files, _session=ftp_session)
            return
        runner = getattr(_session, "run", None)
        if callable(runner):
            await runner(_ftp_upload_tree, ftp_relative_path(root), files)
            return
        created: set[str] = set()
        for relative, content in sorted(files.items()):
            parent = posixpath.dirname(relative)
            if parent and parent not in created:
                await _session.make_directory(join_remote_path(root, parent), parents=True)
                created.add(parent)
            await _session.write_bytes(join_remote_path(root, relative), content)

    async def recover_transactions(
        self,
        service_id: str,
        *,
        require_stopped: Callable[[], Awaitable[None]],
    ) -> tuple[str, ...]:
        """Restore exact pre-mutation state for durable unfinished transactions.

        Prepared records are verification-only because durable ordering proves
        provider mutation had not begun. Later states are rolled back through
        FTPS and completed only after exact readback/manifest verification.
        """

        reference = self._service_reference(service_id)
        if reference is None or self.journal is None:
            return ()
        completed: list[str] = []
        async with self.mutation(service_id):
            await require_stopped()
            candidates = await self.journal.async_recovery_candidates()
            for record in candidates:
                if record.service != reference:
                    continue
                await require_stopped()
                recovery = await self.journal.async_read_recovery(record.transaction_id)
                async with self._ftp_session(service_id) as ftp_session:
                    if record.operation is FileTransactionOperation.WRITE_FILE:
                        previous = decode_file_recovery(recovery)
                        if record.state is TransactionState.PREPARED:
                            await self._verify_recovered_file_state(
                                service_id,
                                record.target_path,
                                previous,
                                _session=ftp_session,
                            )
                        else:
                            await self._begin_journal_recovery(record)
                            await self._restore_recovered_file(
                                service_id,
                                record.target_path,
                                previous,
                                _session=ftp_session,
                            )
                        await self._cleanup_transaction_artifacts(
                            record,
                            _session=ftp_session,
                        )
                        digest = file_recovery_sha256(previous)
                        kind = (
                            VerificationKind.EXACT_MANIFEST_SHA256
                            if previous is None
                            else VerificationKind.EXACT_READBACK_SHA256
                        )
                    elif record.operation is FileTransactionOperation.REPLACE_TREE:
                        previous_tree = await asyncio.to_thread(decode_tree_snapshot, recovery)
                        try:
                            if record.state is TransactionState.PREPARED:
                                await self.verify_tree(
                                    service_id,
                                    record.target_path,
                                    previous_tree.manifest,
                                    _session=ftp_session,
                                )
                            else:
                                await self._begin_journal_recovery(record)
                                await ftp_session.delete_tree(record.target_path, keep_root=True)
                                await self._upload_tree(
                                    service_id,
                                    record.target_path,
                                    previous_tree.files,
                                    _session=ftp_session,
                                )
                                await self.verify_tree(
                                    service_id,
                                    record.target_path,
                                    previous_tree.manifest,
                                    _session=ftp_session,
                                )
                            digest = tree_manifest_sha256(previous_tree.manifest)
                            kind = VerificationKind.EXACT_MANIFEST_SHA256
                        finally:
                            previous_tree.close()
                    else:
                        # Provider-native restores use their own recovery workflow.
                        continue

                if record.state is TransactionState.PREPARED:
                    record = await self.journal.async_mark_mutating(record.transaction_id)
                    record = await self.journal.async_mark_rollback_required(
                        record.transaction_id,
                        JournalFailureCode.CANCELLED,
                    )
                await self.journal.async_mark_verifying(
                    record.transaction_id,
                    VerificationExpectation(VerificationPurpose.ROLLBACK, digest),
                )
                await self.journal.async_complete(
                    record.transaction_id,
                    VerificationEvidence(kind, digest, digest),
                    CompletionDisposition.ROLLED_BACK,
                )
                completed.append(record.transaction_id)
        return tuple(completed)

    async def _begin_journal_recovery(
        self,
        record: FilesystemTransactionRecord,
    ) -> FilesystemTransactionRecord:
        if self.journal is None:
            raise RuntimeError("Filesystem recovery journal is unavailable")
        if record.state is TransactionState.ROLLBACK_REQUIRED:
            return record
        if record.state is TransactionState.PREPARED:
            record = await self.journal.async_mark_mutating(record.transaction_id)
        return await self.journal.async_mark_rollback_required(
            record.transaction_id,
            JournalFailureCode.CANCELLED,
        )

    async def _verify_recovered_file_state(
        self,
        service_id: str,
        target: str,
        previous: bytes | None,
        *,
        _session: _ScopedFtpSession | _LegacyScopedFtp | None = None,
    ) -> None:
        if _session is None:
            async with self._ftp_session(service_id) as ftp_session:
                await self._verify_recovered_file_state(
                    service_id,
                    target,
                    previous,
                    _session=ftp_session,
                )
            return
        if previous is None:
            await self._verify_file_absent(service_id, target, _session=_session)
            return
        observed = await _session.read_bytes(target, max_bytes=len(previous) + 1)
        if observed != previous:
            raise FileRecoveryError("Prepared transaction target changed externally; automatic recovery refused")

    async def _restore_recovered_file(
        self,
        service_id: str,
        target: str,
        previous: bytes | None,
        *,
        _session: _ScopedFtpSession | _LegacyScopedFtp | None = None,
    ) -> None:
        if _session is None:
            async with self._ftp_session(service_id) as ftp_session:
                await self._restore_recovered_file(
                    service_id,
                    target,
                    previous,
                    _session=ftp_session,
                )
            return
        if previous is None:
            entries = await _session.list_directory(posixpath.dirname(target))
            if any(entry.name == posixpath.basename(target) for entry in entries):
                await _session.delete_file(target)
            await self._verify_file_absent(service_id, target, _session=_session)
            return
        await _session.write_bytes(target, previous)
        await self._verify_recovered_file_state(service_id, target, previous, _session=_session)

    async def _verify_file_absent(
        self,
        service_id: str,
        target: str,
        *,
        _session: _ScopedFtpSession | _LegacyScopedFtp | None = None,
    ) -> None:
        if _session is None:
            async with self._ftp_session(service_id) as ftp_session:
                await self._verify_file_absent(service_id, target, _session=ftp_session)
            return
        entries = await _session.list_directory(posixpath.dirname(target))
        if any(entry.name == posixpath.basename(target) for entry in entries):
            raise FileRecoveryError("Recovery target should be absent but still exists")

    async def _cleanup_transaction_artifacts(
        self,
        record: FilesystemTransactionRecord,
        *,
        _session: _ScopedFtpSession | _LegacyScopedFtp,
    ) -> None:
        suffix = record.transaction_id[:12]
        artifacts = (
            _sibling_path(record.target_path, f".nitrado-upload-{suffix}"),
            _sibling_path(record.target_path, f".nitrado-backup-{suffix}"),
        )
        parent = posixpath.dirname(record.target_path)
        entries = await _session.list_directory(parent)
        names = {entry.name for entry in entries}
        for artifact in artifacts:
            if posixpath.basename(artifact) not in names:
                continue
            with suppress(FileTransportError):
                await _session.delete_file(artifact)

    def _service_reference(self, service_id: str) -> ServiceReference | None:
        if self.journal is None:
            return None
        if self.account_entry_id is None:
            raise FileTransportError("Filesystem journal requires the owning account entry ID")
        return ServiceReference(self.account_entry_id, str(service_id))

    async def _ensure_no_unresolved_transaction(self, service_id: str) -> None:
        reference = self._service_reference(service_id)
        if reference is None or self.journal is None:
            return
        candidates = await self.journal.async_recovery_candidates()
        if any(candidate.service == reference for candidate in candidates):
            raise FileRecoveryError("An unresolved filesystem transaction blocks further mutation")

    async def _journal_prepare_file(
        self,
        service_id: str,
        path: str,
        previous: bytes | None,
    ) -> FilesystemTransactionRecord | None:
        reference = self._service_reference(service_id)
        if reference is None or self.journal is None:
            return None
        return await self.journal.async_prepare(
            reference,
            FileTransactionOperation.WRITE_FILE,
            path,
            recovery_content=encode_file_recovery(previous),
        )

    async def _journal_prepare_tree(
        self,
        service_id: str,
        root: str,
        previous: FileTreeSnapshot,
    ) -> FilesystemTransactionRecord | None:
        reference = self._service_reference(service_id)
        if reference is None or self.journal is None:
            return None
        recovery_content = await asyncio.to_thread(encode_tree_snapshot, previous)
        if len(recovery_content) > MAX_TREE_RECOVERY_BLOB_BYTES:
            raise FileTransportError("Encoded recovery tree exceeds its bounded storage limit")
        return await self.journal.async_prepare(
            reference,
            FileTransactionOperation.REPLACE_TREE,
            canonical_relative_path(root),
            recovery_content=recovery_content,
        )

    async def _journal_mark_mutating(self, record: FilesystemTransactionRecord | None) -> None:
        if record is not None and self.journal is not None:
            await self.journal.async_mark_mutating(record.transaction_id)

    async def _journal_mark_rollback_required(
        self,
        record: FilesystemTransactionRecord | None,
        error: BaseException,
    ) -> None:
        if record is not None and self.journal is not None:
            await self.journal.async_mark_rollback_required(
                record.transaction_id,
                _journal_failure_code(error),
            )

    async def _journal_record_commit(
        self,
        record: FilesystemTransactionRecord | None,
        content: bytes,
    ) -> None:
        if record is None or self.journal is None:
            return
        digest = sha256_bytes(content)
        await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.COMMIT, digest),
        )
        await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, digest, digest),
            CompletionDisposition.COMMITTED,
        )

    async def _journal_record_rollback(
        self,
        record: FilesystemTransactionRecord | None,
        previous: bytes | None,
        error: BaseException,
    ) -> None:
        if record is None or self.journal is None:
            return
        await self._journal_mark_rollback_required(record, error)
        if isinstance(error, FileRecoveryError):
            return
        digest = file_recovery_sha256(previous)
        kind = VerificationKind.EXACT_MANIFEST_SHA256 if previous is None else VerificationKind.EXACT_READBACK_SHA256
        await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.ROLLBACK, digest),
        )
        await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(kind, digest, digest),
            CompletionDisposition.ROLLED_BACK,
        )

    async def _journal_record_tree_commit(
        self,
        record: FilesystemTransactionRecord | None,
        manifest: FileTreeManifest,
    ) -> None:
        if record is None or self.journal is None:
            return
        digest = tree_manifest_sha256(manifest)
        await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.COMMIT, digest),
        )
        await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(VerificationKind.EXACT_MANIFEST_SHA256, digest, digest),
            CompletionDisposition.COMMITTED,
        )

    async def _journal_record_tree_rollback(
        self,
        record: FilesystemTransactionRecord | None,
        manifest: FileTreeManifest,
        error: BaseException,
    ) -> None:
        if record is None or self.journal is None:
            return
        await self._journal_mark_rollback_required(record, error)
        digest = tree_manifest_sha256(manifest)
        await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.ROLLBACK, digest),
        )
        await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(VerificationKind.EXACT_MANIFEST_SHA256, digest, digest),
            CompletionDisposition.ROLLED_BACK,
        )


def canonical_relative_path(path: str | RemotePath, *, allow_root: bool = True) -> str:
    """Return a strict confined service-root-relative path."""

    return RemotePath.parse(path, allow_root=allow_root).value


def legacy_provider_path_to_relative(
    path: str,
    *,
    root_prefix: str,
    allow_root: bool = True,
) -> str:
    """Translate an absolute path only after its service root was proven."""

    prefix = _validate_http_root_prefix(root_prefix)
    raw = str(path)
    if raw == prefix:
        return canonical_relative_path("", allow_root=allow_root)
    marker = f"{prefix}/"
    if not raw.startswith(marker):
        raise FileTransportSecurityError("Absolute provider path is outside the proven service FTP root")
    return canonical_relative_path(raw[len(marker) :], allow_root=allow_root)


def ftp_relative_path(path: str | RemotePath, *, allow_root: bool = True) -> str:
    """Validate a strict FTP-root-relative path."""

    return canonical_relative_path(path, allow_root=allow_root)


def provider_api_path(path: str | RemotePath, *, root_prefix: str) -> str:
    """Translate a strict path with a proven provider HTTP root prefix."""

    prefix = _validate_http_root_prefix(root_prefix)
    relative = canonical_relative_path(path)
    return f"{prefix}/{relative}" if relative else prefix


def join_remote_path(root: str | RemotePath, relative: str | RemotePath) -> str:
    """Join two confined service-root-relative paths."""

    base = ftp_relative_path(root)
    child = canonical_relative_path(relative)
    return RemotePath.parse(base).child(child).value


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encode_file_recovery(content: bytes | None) -> bytes:
    """Encode original content while preserving exact target absence."""

    return b"NGF1\x00" if content is None else b"NGF1\x01" + bytes(content)


def decode_file_recovery(content: bytes) -> bytes | None:
    """Decode exact original file state from the private recovery store."""

    value = bytes(content)
    if value == b"NGF1\x00":
        return None
    if value.startswith(b"NGF1\x01"):
        return value[5:]
    raise FileRecoveryError("Recovery file blob has an invalid format")


def file_recovery_sha256(content: bytes | None) -> str:
    """Return exact identity for original content or original absence."""

    return sha256_bytes(encode_file_recovery(content))


def tree_manifest_sha256(manifest: FileTreeManifest) -> str:
    """Return a deterministic identity for one exact tree manifest."""

    payload = [{"path": entry.path, "size": entry.size, "sha256": entry.sha256} for entry in manifest.entries]
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def encode_tree_snapshot(snapshot: FileTreeSnapshot) -> bytes:
    """Encode a bounded snapshot for the private recovery blob store."""

    output = bytearray(b"NGST1\x00")
    for path, content in sorted(snapshot.files.items()):
        canonical = canonical_relative_path(path, allow_root=False)
        path_bytes = canonical.encode("utf-8")
        blob = bytes(content)
        output.extend(len(path_bytes).to_bytes(4, "big"))
        output.extend(len(blob).to_bytes(8, "big"))
        output.extend(path_bytes)
        output.extend(blob)
    return bytes(output)


def decode_tree_snapshot(content: bytes) -> FileTreeSnapshot:
    """Decode and revalidate a private recovery snapshot."""

    data = memoryview(content if isinstance(content, bytes) else bytes(content))
    if len(data) < 6 or bytes(data[:6]) != b"NGST1\x00":
        raise FileRecoveryError("Recovery tree blob has an invalid format")
    offset = 6
    files = SpoolTreeFiles()
    entries: list[FileManifestEntry] = []
    total_bytes = 0
    while offset < len(data):
        if len(data) - offset < 12:
            raise FileRecoveryError("Recovery tree blob is truncated")
        path_size = int.from_bytes(data[offset : offset + 4], "big")
        blob_size = int.from_bytes(data[offset + 4 : offset + 12], "big")
        offset += 12
        if path_size < 1 or path_size > DEFAULT_MAX_PATH_BYTES:
            raise FileRecoveryError("Recovery tree path length is invalid")
        if blob_size > DEFAULT_MAX_TREE_BYTES or offset + path_size + blob_size > len(data):
            raise FileRecoveryError("Recovery tree blob length is invalid")
        try:
            path = bytes(data[offset : offset + path_size]).decode("utf-8")
        except UnicodeDecodeError as err:
            raise FileRecoveryError("Recovery tree path is not UTF-8") from err
        offset += path_size
        canonical = canonical_relative_path(path, allow_root=False)
        if canonical in files:
            raise FileRecoveryError("Recovery tree contains duplicate paths")
        blob = bytes(data[offset : offset + blob_size])
        entries.append(files.append(canonical, blob))
        total_bytes += blob_size
        offset += blob_size
        if len(files) > DEFAULT_MAX_TREE_FILES or total_bytes > DEFAULT_MAX_TREE_BYTES:
            raise FileRecoveryError("Recovery tree exceeds configured limits")
    manifest = FileTreeManifest(tuple(sorted(entries, key=lambda item: item.path)), total_bytes)
    try:
        _validate_manifest_limits(manifest)
        return FileTreeSnapshot(files, manifest)
    except BaseException:
        files.close()
        raise


def new_transaction_id(service_id: str, operation: str) -> str:
    seed = f"{service_id}:{operation}:{asyncio.get_running_loop().time()}".encode()
    return hashlib.sha256(seed).hexdigest()[:20]


def operation_diagnostics(result: FileOperationResult) -> dict[str, Any]:
    return {
        "transaction_id": result.transaction_id,
        "operation": result.operation,
        "transport": result.transport.value,
        "verification": result.verification.value,
        "recovery_available": result.recovery_available,
        "files": len(result.manifest.entries) if result.manifest else None,
        "bytes": result.manifest.total_bytes if result.manifest else None,
    }


def parse_http_file_entries(
    payload: Mapping[str, Any],
    *,
    root_prefix: str,
) -> tuple[FileEntry, ...]:
    """Normalize supported Nitrado file-list payload variants."""

    entries: Any = payload.get("entries")
    if not isinstance(entries, list):
        for key in ("files", "items", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                entries = candidate
                break
    if not isinstance(entries, list):
        raise FileTransportError("Nitrado file list did not contain entries")
    parsed: list[FileEntry] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path") or item.get("file") or item.get("name")
        if not isinstance(path, str) or not path:
            continue
        raw_type = str(item.get("type") or item.get("kind") or "").lower()
        if raw_type in {"dir", "directory", "folder"}:
            entry_type = FileEntryType.DIRECTORY
        elif raw_type in {"file", "regular"}:
            entry_type = FileEntryType.FILE
        else:
            continue
        canonical = legacy_provider_path_to_relative(path, root_prefix=root_prefix)
        name = posixpath.basename(canonical)
        size = item.get("size")
        parsed_size = size if isinstance(size, int) and size >= 0 else None
        parsed.append(FileEntry(canonical, name, entry_type, parsed_size, _optional_string(item.get("modified"))))
    return tuple(parsed)


def derive_http_root_prefix(payload: Mapping[str, Any]) -> str:
    """Prove the unique account-specific ``.../ftproot`` prefix."""

    prefixes: set[str] = set()
    for path in _iter_http_entry_paths(payload):
        marker_index = path.find(FTP_ROOT_MARKER)
        if marker_index >= 0:
            prefixes.add(_validate_http_root_prefix(path[: marker_index + len("/ftproot")]))
        elif path.endswith("/ftproot"):
            prefixes.add(_validate_http_root_prefix(path))
    if len(prefixes) != 1:
        raise FileTransportSecurityError("Nitrado HTTP root listing did not prove one unique FTP root")
    return next(iter(prefixes))


def _iter_http_entry_paths(value: Any) -> tuple[str, ...]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                if key in {"path", "file"} and isinstance(child, str):
                    found.append(child)
                elif isinstance(child, (Mapping, list, tuple)):
                    walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(value)
    return tuple(found)


def _validate_http_root_prefix(prefix: str) -> str:
    value = str(prefix)
    if not value.startswith("/") or not value.endswith("/ftproot"):
        raise FileTransportSecurityError("Nitrado HTTP file root has an invalid shape")
    if "\\" in value or "//" in value:
        raise FileTransportSecurityError("Nitrado HTTP file root is ambiguous")
    components = value.strip("/").split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise FileTransportSecurityError("Nitrado HTTP file root contains an unsafe component")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in component) for component in components):
        raise FileTransportSecurityError("Nitrado HTTP file root contains a control character")
    return value


def resolve_ftp_endpoint(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve and validate all FTP control endpoints before connecting."""

    if not hostname or not 1 <= int(port) <= 65535:
        raise FileTransportSecurityError("Nitrado returned an invalid FTP endpoint")
    lowered = hostname.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise FileTransportSecurityError("Nitrado FTP endpoint resolves to a local-only hostname")
    try:
        records = socket.getaddrinfo(hostname, int(port), type=socket.SOCK_STREAM)
    except OSError as err:
        raise FileTransportError("Nitrado FTP endpoint could not be resolved") from err
    addresses: list[str] = []
    for record in records:
        value = str(ipaddress.ip_address(record[4][0]))
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise FileTransportSecurityError("Nitrado FTP endpoint resolved to a non-public address")
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise FileTransportError("Nitrado FTP endpoint did not resolve to an address")
    return tuple(addresses)


def validate_ftp_endpoint(hostname: str, port: int) -> None:
    """Compatibility wrapper for endpoint validation."""

    resolve_ftp_endpoint(hostname, port)


async def _drain_future(future: asyncio.Future[Any], timeout: float) -> None:
    done, _pending = await asyncio.wait({future}, timeout=timeout)
    if done:
        with suppress(BaseException):
            next(iter(done)).result()


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    """Retrieve detached worker failures so asyncio never reports them later."""

    if future.cancelled():
        return
    with suppress(BaseException):
        future.exception()


def _finish_login(
    ftp: FTP,
    credentials: NitradoFtpCredentials,
    kind: FileTransportKind,
    *,
    already_logged_in: bool = False,
) -> _FtpSession:
    if not already_logged_in:
        try:
            ftp.login(credentials.username, credentials.password)
        except error_perm as err:
            if _is_login_rejection(err):
                raise _CredentialRejected from err
            raise
    ftp.set_pasv(True)
    root = _validate_ftp_root(ftp.pwd())
    return _FtpSession(ftp, root, kind)


def _validate_ftp_root(root: str) -> str:
    value = str(root)
    if not value.startswith("/") or "\\" in value:
        raise FileTransportSecurityError("Nitrado FTP returned an invalid service root")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise FileTransportSecurityError("Nitrado FTP service root contains a control character")
    components = [component for component in value.split("/") if component]
    if any(component in {".", ".."} for component in components):
        raise FileTransportSecurityError("Nitrado FTP service root is ambiguous")
    return "/" + "/".join(components) if components else "/"


def _is_login_rejection(error: BaseException) -> bool:
    return isinstance(error, error_perm) and str(error).lstrip().startswith(("530", "532"))


def _close_ftp(ftp: FTP) -> None:
    with suppress(*FTP_ERRORS, AttributeError, OSError):
        ftp.quit()
    with suppress(*FTP_ERRORS, AttributeError, OSError):
        ftp.close()


def _ftp_cwd_root(session: _FtpSession) -> None:
    session.ftp.cwd(session.root)
    if _validate_ftp_root(session.ftp.pwd()) != session.root:
        raise FileTransportSecurityError("FTP server did not remain inside the authenticated service root")


def _ftp_expected_directory(session: _FtpSession, parts: Iterable[str]) -> str:
    suffix = "/".join(parts)
    if not suffix:
        return session.root
    return f"{session.root.rstrip('/')}/{suffix}"


def _ftp_child_types(session: _FtpSession) -> dict[str, str]:
    entries: dict[str, str] = {}
    aliases: set[str] = set()
    # RFC 3659 lets servers choose their default MLSD fact set.  Nitrado does
    # not include ``type`` unless the client explicitly requests it, and type
    # is the security boundary that lets us reject links and special entries.
    # Request only the fact we require so regular files/directories can be
    # classified without weakening the fail-closed behavior below.
    for name, facts in session.ftp.mlsd(facts=["type"]):
        if name in {".", ".."}:
            continue
        RemotePath.parse(name, allow_root=False)
        if "/" in name:
            raise FileTransportSecurityError("FTP directory entry name must be one path component")
        kind = str(facts.get("type", "")).lower()
        if kind in {"cdir", "pdir"}:
            continue
        if kind not in {"dir", "file"}:
            raise FileTransportSecurityError(f"Refusing unsupported FTP entry type: {kind or 'unknown'}")
        if name in entries:
            raise FileTransportSecurityError("FTP directory contains duplicate entry names")
        alias = name.casefold()
        if alias in aliases:
            raise FileTransportSecurityError("FTP directory contains case-ambiguous entry names")
        entries[name] = kind
        aliases.add(alias)
        if len(entries) > DEFAULT_MAX_DIRECTORY_ENTRIES:
            raise FileTransportError("FTP directory exceeded the configured entry-count limit")
    return entries


def _ftp_mlst_type(session: _FtpSession, name: str) -> str:
    """Revalidate one child type over the FTP control channel.

    MLST avoids opening another passive data connection while closing the
    list-to-use window enough to preserve the prior fail-closed type check.
    """

    RemotePath.parse(name, allow_root=False)
    if "/" in name:
        raise FileTransportSecurityError("FTP MLST target must be one path component")
    response = session.ftp.sendcmd(f"MLST {name}")
    matches: list[str] = []
    for raw_line in str(response).splitlines():
        line = raw_line.strip()
        if line.startswith(("250-", "250 ")):
            line = line[4:].lstrip()
        fact_text = line.partition(" ")[0]
        facts: dict[str, str] = {}
        for raw_fact in fact_text.split(";"):
            key, separator, value = raw_fact.partition("=")
            if separator:
                facts[key.lower()] = value.lower()
        if "type" in facts:
            matches.append(facts["type"])
    if len(matches) != 1 or matches[0] not in {"dir", "file"}:
        observed = matches[0] if len(matches) == 1 else "unknown"
        raise FileTransportSecurityError(f"Refusing unsupported FTP entry type: {observed}")
    return matches[0]


def _ftp_cwd(session: _FtpSession, relative: str, *, create: bool = False) -> None:
    _ftp_cwd_root(session)
    traversed: list[str] = []
    for part in filter(None, relative.split("/")):
        kind = _ftp_child_types(session).get(part)
        if kind is None:
            if not create:
                raise FileTransportError("FTP path component does not exist")
            session.ftp.mkd(part)
        elif kind != "dir":
            raise FileTransportSecurityError("FTP path ancestor is not a regular directory")
        session.ftp.cwd(part)
        traversed.append(part)
        observed = _validate_ftp_root(session.ftp.pwd())
        if observed != _ftp_expected_directory(session, traversed):
            raise FileTransportSecurityError("FTP directory traversal escaped the authenticated service root")


def _ftp_list_directory(session: _FtpSession, relative: str) -> tuple[FileEntry, ...]:
    _ftp_cwd(session, relative)
    entries: list[FileEntry] = []
    for name, kind in _ftp_child_types(session).items():
        entry_type = FileEntryType.DIRECTORY if kind == "dir" else FileEntryType.FILE
        path = RemotePath.parse(relative).child(name).value
        entries.append(FileEntry(path, name, entry_type))
    return tuple(entries)


def _ftp_require_current_directory(session: _FtpSession, relative: str) -> None:
    """Prove the cursor is at exactly one service-root-relative directory."""

    expected = _ftp_expected_directory(session, filter(None, relative.split("/")))
    if _validate_ftp_root(session.ftp.pwd()) != expected:
        raise FileTransportSecurityError("FTP tree cursor left its verified directory")


def _ftp_tree_position(session: _FtpSession, relative: str) -> None:
    """Verify every ancestor once while positioning a tree cursor."""

    _ftp_cwd(session, relative)
    _ftp_require_current_directory(session, relative)
    session.tree_directory = relative
    session.tree_entries.clear()
    session.tree_ancestors.clear()


def _ftp_tree_list_current(session: _FtpSession, relative: str) -> tuple[FileEntry, ...]:
    """List the verified current cursor directory without re-walking ancestors."""

    _ftp_require_current_directory(session, relative)
    if session.tree_directory != relative:
        raise FileTransportSecurityError("FTP tree cursor state does not match its verified directory")
    entries: list[FileEntry] = []
    child_types = _ftp_child_types(session)
    session.tree_entries = child_types
    for name, kind in child_types.items():
        entry_type = FileEntryType.DIRECTORY if kind == "dir" else FileEntryType.FILE
        path = RemotePath.parse(relative).child(name).value
        entries.append(FileEntry(path, name, entry_type))
    return tuple(entries)


def _ftp_tree_read_current(session: _FtpSession, directory: str, name: str, max_bytes: int) -> bytes:
    """Read one previously classified regular file from the current directory."""

    _ftp_require_current_directory(session, directory)
    if session.tree_directory != directory or session.tree_entries.get(name) != "file":
        raise FileTransportSecurityError("FTP tree cursor target was not classified as a regular file")
    if _ftp_mlst_type(session, name) != "file":
        raise FileTransportSecurityError("FTP tree cursor target changed type before read")
    output = BytesIO()

    def receive(chunk: bytes) -> None:
        if output.tell() + len(chunk) > max_bytes:
            raise FileTransportError("FTP file read exceeded the configured limit")
        output.write(chunk)

    session.ftp.retrbinary(f"RETR {name}", receive)
    _ftp_require_current_directory(session, directory)
    return output.getvalue()


def _ftp_tree_enter_current(session: _FtpSession, parent: str, name: str) -> None:
    """Enter one child classified by the immediately preceding directory list."""

    _ftp_require_current_directory(session, parent)
    if session.tree_directory != parent or session.tree_entries.get(name) != "dir":
        raise FileTransportSecurityError("FTP tree cursor target was not classified as a regular directory")
    if _ftp_mlst_type(session, name) != "dir":
        raise FileTransportSecurityError("FTP tree cursor target changed type before traversal")
    session.ftp.cwd(name)
    child = join_remote_path(parent, name)
    _ftp_require_current_directory(session, child)
    session.tree_ancestors.append((parent, session.tree_entries))
    session.tree_directory = child
    session.tree_entries = {}


def _ftp_tree_leave_current(session: _FtpSession, child: str, parent: str) -> None:
    """Return to the exact verified parent of the current cursor directory."""

    _ftp_require_current_directory(session, child)
    if session.tree_directory != child or not session.tree_ancestors:
        raise FileTransportSecurityError("FTP tree cursor cannot leave an unverified directory")
    session.ftp.cwd("..")
    _ftp_require_current_directory(session, parent)
    previous_directory, previous_entries = session.tree_ancestors.pop()
    if previous_directory != parent:
        raise FileTransportSecurityError("FTP tree cursor parent stack is inconsistent")
    session.tree_directory = parent
    session.tree_entries = previous_entries


def _ftp_read_bytes(session: _FtpSession, relative: str, max_bytes: int) -> bytes:
    output = BytesIO()

    def receive(chunk: bytes) -> None:
        if output.tell() + len(chunk) > max_bytes:
            raise FileTransportError("FTP file read exceeded the configured limit")
        output.write(chunk)

    parent, name = posixpath.split(relative)
    _ftp_cwd(session, parent)
    if _ftp_child_types(session).get(name) != "file":
        raise FileTransportSecurityError("FTP target is not one unambiguous regular file")
    session.ftp.retrbinary(f"RETR {name}", receive)
    return output.getvalue()


def _ftp_read_optional_bytes(session: _FtpSession, relative: str, max_bytes: int) -> bytes | None:
    parent = posixpath.dirname(relative)
    name = posixpath.basename(relative)
    entries = _ftp_list_directory(session, parent)
    matches = [entry for entry in entries if entry.name == name]
    if not matches:
        return None
    if len(matches) != 1 or matches[0].entry_type is not FileEntryType.FILE:
        raise FileTransportSecurityError("FTP target is not one unambiguous regular file")
    return _ftp_read_bytes(session, relative, max_bytes)


def _ftp_write_bytes(session: _FtpSession, relative: str, content: bytes) -> None:
    parent = posixpath.dirname(relative)
    if parent:
        _ftp_cwd(session, parent, create=True)
        target = posixpath.basename(relative)
    else:
        _ftp_cwd_root(session)
        target = relative
    session.ftp.storbinary(f"STOR {target}", BytesIO(content))


def _ftp_make_directory(session: _FtpSession, relative: str, parents: bool) -> None:
    if not relative:
        return
    if parents:
        _ftp_cwd(session, relative, create=True)
        return
    parent, name = posixpath.split(relative)
    _ftp_cwd(session, parent)
    session.ftp.mkd(name)


def _ftp_delete_file(session: _FtpSession, relative: str) -> None:
    parent, name = posixpath.split(relative)
    _ftp_cwd(session, parent)
    if _ftp_child_types(session).get(name) != "file":
        raise FileTransportSecurityError("FTP delete target is not one regular file")
    session.ftp.delete(name)


def _ftp_delete_tree(session: _FtpSession, relative: str, keep_root: bool) -> None:
    for entry in _ftp_list_directory(session, relative):
        child = RemotePath.parse(relative).child(entry.name).value
        if entry.entry_type is FileEntryType.DIRECTORY:
            _ftp_delete_tree(session, child, False)
        else:
            _ftp_delete_file(session, child)
    if not keep_root:
        _ftp_cwd_root(session)
        session.ftp.rmd(relative)


def _ftp_rename(session: _FtpSession, source: str, target: str) -> None:
    source_parent, source_name = posixpath.split(source)
    target_parent, target_name = posixpath.split(target)
    _ftp_cwd(session, source_parent)
    if _ftp_child_types(session).get(source_name) != "file":
        raise FileTransportSecurityError("FTP rename source is not one regular file")
    # Validate the target parent independently before asking the provider to
    # cross directories.  Same-directory renames remain relative and simple.
    _ftp_cwd(session, target_parent)
    target_entries = _ftp_child_types(session)
    if target_name in target_entries:
        raise FileTransportSecurityError("FTP rename target already exists")
    if source_parent == target_parent:
        session.ftp.rename(source_name, target_name)
        return
    target_absolute = f"{session.root.rstrip('/')}/{target}" if session.root != "/" else f"/{target}"
    _ftp_cwd(session, source_parent)
    session.ftp.rename(source_name, target_absolute)


def _ftp_upload_tree(session: _FtpSession, root: str, files: Mapping[str, bytes]) -> None:
    """Upload one validated materialized tree on the existing FTP session."""

    created: set[str] = set()
    for relative, content in sorted(files.items()):
        canonical = canonical_relative_path(relative, allow_root=False)
        parent = posixpath.dirname(canonical)
        if parent and parent not in created:
            _ftp_make_directory(session, join_remote_path(root, parent), True)
            created.add(parent)
        _ftp_write_bytes(session, join_remote_path(root, canonical), bytes(content))


def _sibling_path(path: str, suffix: str) -> str:
    remote = RemotePath.parse(path, allow_root=False)
    name = remote.name
    temporary_name = f".{name}{suffix}"
    RemotePath.parse(temporary_name, allow_root=False)
    return remote.parent.child(temporary_name).value


def _validate_manifest_limits(manifest: FileTreeManifest) -> None:
    if len(manifest.entries) > DEFAULT_MAX_TREE_FILES:
        raise FileTransportError("Tree manifest exceeds the configured file-count limit")
    if manifest.total_bytes > DEFAULT_MAX_TREE_BYTES:
        raise FileTransportError("Tree manifest exceeds the configured byte limit")


def _validate_ftp_read_limit(max_bytes: int) -> None:
    if max_bytes < 1 or max_bytes > DEFAULT_MAX_BINARY_BYTES:
        raise FileTransportError("FTP read limit is outside the supported range")


def _journal_failure_code(error: BaseException) -> JournalFailureCode:
    if isinstance(error, asyncio.CancelledError):
        return JournalFailureCode.CANCELLED
    if isinstance(error, FileVerificationError):
        return JournalFailureCode.VERIFICATION_FAILED
    if isinstance(error, FileRecoveryError):
        return JournalFailureCode.ROLLBACK_FAILED
    if isinstance(error, FileTransportSecurityError):
        return JournalFailureCode.PROVIDER_REJECTED
    if isinstance(error, (FileTransportError, OSError, EOFError)):
        return JournalFailureCode.CONNECTION_LOST
    return JournalFailureCode.UNKNOWN


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
