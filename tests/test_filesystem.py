"""Focused security and transaction tests for provider filesystem transport."""

from __future__ import annotations

import asyncio
import copy
import posixpath
import socket
import ssl
import threading
import time
import unittest
from ftplib import error_perm
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from custom_components.nitrado_gameserver.api.nitrado import NitradoApiError, NitradoFtpCredentials
from custom_components.nitrado_gameserver.filesystem import (
    DEFAULT_MAX_DIRECTORY_ENTRIES,
    FileConcurrentModificationError,
    FileEntry,
    FileEntryType,
    FileRecoveryError,
    FileTransportError,
    FileTransportKind,
    FileTransportSecurityError,
    FileTreeManifest,
    FileVerificationError,
    NitradoFilesystemService,
    NitradoFtpTransport,
    NitradoHttpFileTransport,
    RemotePath,
    SpoolTreeFiles,
    _PinnedFTP,
    canonical_relative_path,
    decode_tree_snapshot,
    derive_http_root_prefix,
    encode_file_recovery,
    encode_tree_snapshot,
    ftp_relative_path,
    legacy_provider_path_to_relative,
    provider_api_path,
    resolve_ftp_endpoint,
)
from custom_components.nitrado_gameserver.filesystem_journal import (
    FilesystemTransactionJournal,
    FileTransactionOperation,
    ServiceReference,
)


class MemoryFtp:
    """Small ftplib-shaped server rooted below ``/home/account``."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        password: str = "secret",
        fail_reads: bool = False,
        block_reads: bool = False,
    ) -> None:
        self.files = files if files is not None else {}
        self.expected_password = password
        self.fail_reads = fail_reads
        self.block_reads = block_reads
        self.closed = threading.Event()
        self.cwd_value = "/home/account"
        self.cwd_calls: list[str] = []
        self.mlsd_calls: list[tuple[str, tuple[str, ...]]] = []
        self.mlst_calls: list[str] = []
        self.retrbinary_calls: list[str] = []

    def login(self, username: str, password: str) -> str:
        del username
        if password != self.expected_password:
            raise error_perm("530 invalid credentials")
        return "230 logged in"

    def set_pasv(self, enabled: bool) -> None:
        assert enabled

    def pwd(self) -> str:
        return self.cwd_value

    def cwd(self, path: str) -> None:
        self.cwd_calls.append(path)
        if path.startswith("/"):
            candidate = posixpath.normpath(path)
        else:
            candidate = posixpath.normpath(posixpath.join(self.cwd_value, path))
        if candidate != "/home/account" and not candidate.startswith("/home/account/"):
            raise error_perm("550 outside test root")
        self.cwd_value = candidate

    def mkd(self, path: str) -> None:
        del path

    def retrbinary(self, command: str, callback: Any) -> None:
        if self.block_reads:
            self.closed.wait(2)
            raise OSError("socket closed")
        if self.fail_reads:
            raise error_perm("530 expired after operation started")
        name = command.removeprefix("RETR ")
        self.retrbinary_calls.append(name)
        parent = self.cwd_value.removeprefix("/home/account").strip("/")
        path = f"{parent}/{name}" if parent else name
        callback(self.files[path])

    def storbinary(self, command: str, stream: BytesIO) -> None:
        name = command.removeprefix("STOR ")
        parent = self.cwd_value.removeprefix("/home/account").strip("/")
        path = f"{parent}/{name}" if parent else name
        self.files[path] = stream.read()

    def rename(self, source: str, target: str) -> None:
        self.files[target] = self.files.pop(source)

    def delete(self, path: str) -> None:
        if path not in self.files:
            raise error_perm("550 missing")
        del self.files[path]

    def rmd(self, path: str) -> None:
        del path

    def mlsd(self, path: str = "", facts: list[str] | None = None):
        requested_facts = tuple(facts or ())
        self.mlsd_calls.append((path, requested_facts))
        parent = self.cwd_value.removeprefix("/home/account").strip("/")
        prefix = f"{parent}/" if parent else ""
        entries: dict[str, dict[str, str]] = {}
        for path, content in self.files.items():
            if not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            name, separator, _rest = remainder.partition("/")
            entry_facts = {"type": "dir"} if separator else {"type": "file", "size": str(len(content))}
            entries[name] = {
                fact_name: fact_value for fact_name, fact_value in entry_facts.items() if fact_name in requested_facts
            }
        return iter(entries.items())

    def sendcmd(self, command: str) -> str:
        if not command.startswith("MLST "):
            raise AssertionError(f"unexpected FTP command: {command}")
        name = command.removeprefix("MLST ")
        self.mlst_calls.append(name)
        parent = self.cwd_value.removeprefix("/home/account").strip("/")
        prefix = f"{parent}/" if parent else ""
        candidate = f"{prefix}{name}"
        if candidate in self.files:
            kind = "file"
        elif any(path.startswith(f"{candidate}/") for path in self.files):
            kind = "dir"
        else:
            raise error_perm("550 missing")
        return f"250-Listing {name}\n type={kind}; {name}\n250 End"

    def quit(self) -> None:
        self.closed.set()

    def close(self) -> None:
        self.closed.set()


class MemoryMetadataStore:
    def __init__(self) -> None:
        self.payload = None

    async def async_load(self):
        return copy.deepcopy(self.payload)

    async def async_save(self, payload):
        self.payload = copy.deepcopy(payload)


class MemoryBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def async_write(self, blob_id: str, content: bytes) -> None:
        self.blobs[blob_id] = bytes(content)

    async def async_read(self, blob_id: str) -> bytes:
        return self.blobs[blob_id]

    async def async_delete(self, blob_id: str) -> None:
        self.blobs.pop(blob_id, None)


class MemoryTreeTransport:
    """Service-scoped async filesystem used to prove tree transactions."""

    def __init__(self, files: dict[str, bytes], *, corrupt_new_writes: bool = False) -> None:
        self.files = dict(files)
        self.corrupt_new_writes = corrupt_new_writes
        self.delete_tree_calls = 0
        self._observed_transport = {"100": FileTransportKind.FTPS}

    async def list_directory(self, service_id: str, path: str) -> tuple[FileEntry, ...]:
        del service_id
        prefix = f"{path.rstrip('/')}/" if path else ""
        children: dict[str, FileEntry] = {}
        for file_path, content in self.files.items():
            if not file_path.startswith(prefix):
                continue
            remainder = file_path[len(prefix) :]
            name, separator, _tail = remainder.partition("/")
            child_path = f"{prefix}{name}" if prefix else name
            if separator:
                children[name] = FileEntry(child_path, name, FileEntryType.DIRECTORY)
            else:
                children[name] = FileEntry(child_path, name, FileEntryType.FILE, len(content))
        return tuple(children[name] for name in sorted(children))

    async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
        del service_id
        content = self.files[path]
        if len(content) > max_bytes:
            raise FileTransportError("test read exceeded bound")
        return content

    async def read_optional_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes | None:
        del service_id
        content = self.files.get(path)
        if content is not None and len(content) > max_bytes:
            raise FileTransportError("test read exceeded bound")
        return content

    async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
        del service_id
        data = bytes(content)
        if self.corrupt_new_writes and data.startswith(b"new"):
            data = b"X" + data[1:]
        self.files[path] = data

    async def make_directory(self, service_id: str, path: str, *, parents: bool = True) -> None:
        del service_id, path, parents

    async def delete_file(self, service_id: str, path: str) -> None:
        del service_id
        self.files.pop(path, None)

    async def delete_tree(self, service_id: str, path: str, *, keep_root: bool = True) -> None:
        del service_id, keep_root
        self.delete_tree_calls += 1
        prefix = f"{path.rstrip('/')}/"
        self.files = {name: content for name, content in self.files.items() if not name.startswith(prefix)}

    async def rename(self, service_id: str, source: str, target: str) -> None:
        del service_id
        self.files[target] = self.files.pop(source)


class PathContractTests(unittest.TestCase):
    def test_strict_relative_path_accepts_only_canonical_spelling(self) -> None:
        self.assertEqual(canonical_relative_path("Pal/Saved/config.ini"), "Pal/Saved/config.ini")
        for unsafe in (
            "/Pal/Saved",
            "../Saved",
            "Pal/../Saved",
            "Pal//Saved",
            "Pal/./Saved",
            "Pal\\Saved",
            " Pal/Saved",
            "Pal/Saved ",
            "Pal/\nSaved",
            "Pal/\x00Saved",
            "Pal/Cafe\u0301",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(FileTransportSecurityError):
                canonical_relative_path(unsafe)

    def test_remote_path_child_cannot_escape(self) -> None:
        root = RemotePath.parse("Pal/Saved")
        self.assertEqual(str(root.child("Config/LinuxServer")), "Pal/Saved/Config/LinuxServer")
        with self.assertRaises(FileTransportSecurityError):
            root.child("../Players")

    def test_legacy_provider_adapter_is_explicit_and_confined(self) -> None:
        root = "/games/ni9352260_116902/ftproot"
        provider = f"{root}/Pal/Saved/Config.ini"
        self.assertEqual(
            legacy_provider_path_to_relative(provider, root_prefix=root),
            "Pal/Saved/Config.ini",
        )
        self.assertEqual(provider_api_path("Pal/Saved/Config.ini", root_prefix=root), provider)
        with self.assertRaises(FileTransportSecurityError):
            ftp_relative_path(provider)
        with self.assertRaises(FileTransportSecurityError):
            legacy_provider_path_to_relative("/etc/passwd", root_prefix=root)

    def test_http_root_prefix_requires_one_proven_root(self) -> None:
        payload = {
            "entries": [
                {"path": "/games/ni9352260_116902/ftproot/palworldxb", "type": "dir"},
                {"path": "/games/ni9352260_116902/ftproot/readme.txt", "type": "file"},
            ]
        }
        self.assertEqual(
            derive_http_root_prefix(payload),
            "/games/ni9352260_116902/ftproot",
        )
        with self.assertRaises(FileTransportSecurityError):
            derive_http_root_prefix({"entries": []})

    def test_credentials_do_not_render_password(self) -> None:
        credentials = NitradoFtpCredentials("ftp.example.test", 21, "user", "do-not-print", False)
        self.assertNotIn("do-not-print", repr(credentials))

    def test_endpoint_resolution_rejects_any_private_address(self) -> None:
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", 21))]
        with (
            patch("socket.getaddrinfo", return_value=records),
            self.assertRaises(FileTransportSecurityError),
        ):
            resolve_ftp_endpoint("ftp.example.test", 21)

    def test_epsv_fallback_ignores_server_supplied_pasv_host(self) -> None:
        class Peer:
            def getpeername(self):
                return ("198.51.100.20", 21)

        ftp = _PinnedFTP()
        ftp.sock = Peer()  # type: ignore[assignment]
        ftp.af = socket.AF_INET
        replies = iter((error_perm("500 EPSV unavailable"), "227 Entering Passive Mode (10,0,0,5,7,138)"))

        def sendcmd(command: str) -> str:
            del command
            reply = next(replies)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        ftp.sendcmd = sendcmd  # type: ignore[method-assign]
        self.assertEqual(ftp.makepasv(), ("198.51.100.20", 1930))


class FtpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.transports: list[NitradoFtpTransport] = []

    async def asyncTearDown(self) -> None:
        for transport in self.transports:
            await transport.async_close()

    def transport(self, *args: Any, **kwargs: Any) -> NitradoFtpTransport:
        transport = NitradoFtpTransport(*args, **kwargs)
        self.transports.append(transport)
        return transport

    async def test_plaintext_requires_per_service_consent_and_updates_live(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        consent: set[str] = set()
        ftp = MemoryFtp({"status.txt": b"okay"})
        transport = self.transport(
            credentials,
            plaintext_consent=consent,
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )
        with self.assertRaises(FileTransportSecurityError):
            await transport.read_bytes("100", "status.txt", max_bytes=32)
        transport.update_plaintext_consent({"100"})
        self.assertEqual(await transport.read_bytes("100", "status.txt", max_bytes=32), b"okay")
        self.assertEqual(transport.observed_status("100")["observed_transport"], "ftp")
        self.assertIs(transport.observed_capabilities("100").transport, FileTransportKind.FTP)
        self.assertFalse(transport.observed_status("200")["plaintext_consent"])

    async def test_login_rejection_refreshes_credentials_exactly_once(self) -> None:
        calls = 0

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            nonlocal calls
            del service_id
            calls += 1
            password = "stale" if calls == 1 else "fresh"
            return NitradoFtpCredentials("ftp.example.test", 21, "user", password, False)

        files = {"status.txt": b"okay"}
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: MemoryFtp(files, password="fresh"),
        )
        self.assertEqual(await transport.read_bytes("100", "status.txt", max_bytes=32), b"okay")
        self.assertEqual(calls, 2)
        self.assertEqual(transport.observed_status("100")["failure_count"], 0)

    async def test_capability_probe_is_cached_after_secure_transport_is_proven(self) -> None:
        calls = 0

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", True)

        def connection_factory(creds, secure):
            nonlocal calls
            del creds, secure
            calls += 1
            return MemoryFtp()

        transport = self.transport(
            credentials,
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=connection_factory,
        )
        first = await transport.capabilities("100")
        second = await transport.capabilities("100")
        self.assertTrue(first.secure_transport)
        self.assertEqual(second, first)
        self.assertEqual(calls, 1)

    async def test_failed_capability_probe_uses_bounded_backoff(self) -> None:
        calls = 0

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            nonlocal calls
            del service_id
            calls += 1
            raise FileTransportError("provider unavailable")

        transport = self.transport(credentials)
        with self.assertRaisesRegex(FileTransportError, "provider unavailable"):
            await transport.capabilities("100")
        with self.assertRaisesRegex(FileTransportError, "temporarily backed off"):
            await transport.capabilities("100")
        self.assertEqual(calls, 1)

    async def test_repeated_refreshed_credential_rejection_is_persistent_and_secret_free(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "still-stale", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: MemoryFtp(password="different"),
        )
        for _ in range(2):
            with self.assertRaisesRegex(FileTransportError, "rejected refreshed"):
                await transport.read_bytes("100", "status.txt", max_bytes=32)
        status = transport.observed_status("100")
        self.assertEqual(status["failure_count"], 2)
        self.assertEqual(status["last_failure_code"], "credentials_rejected")
        self.assertNotIn("still-stale", repr(status))

    async def test_repeated_mid_transfer_failures_are_persistent(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: MemoryFtp(fail_reads=True),
        )
        for _ in range(2):
            with self.assertRaises(FileTransportError):
                await transport.read_bytes("100", "status.txt", max_bytes=32)
        status = transport.observed_status("100")
        self.assertEqual(status["failure_count"], 2)
        self.assertEqual(status["last_failure_code"], "transport_operation_failed")

    async def test_auth_failure_after_callback_starts_is_not_retried(self) -> None:
        calls = 0

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            nonlocal calls
            del service_id
            calls += 1
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: MemoryFtp(fail_reads=True),
        )
        with self.assertRaises(FileTransportError):
            await transport.read_bytes("100", "status.txt", max_bytes=32)
        self.assertEqual(calls, 1)

    async def test_worker_timeout_closes_socket_before_returning(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        ftp = MemoryFtp({"status.txt": b"okay"}, block_reads=True)
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
            operation_timeout=0.02,
            drain_timeout=0.5,
        )
        with self.assertRaisesRegex(FileTransportError, "execution budget"):
            await transport.read_bytes("100", "status.txt", max_bytes=32)
        self.assertTrue(ftp.closed.is_set())

    async def test_cancellation_during_connection_discards_active_record_after_worker_exits(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        entered = threading.Event()
        release = threading.Event()
        ftp = MemoryFtp({"status.txt": b"okay"})

        def connect(creds, secure):
            del creds, secure
            entered.set()
            release.wait(1)
            return ftp

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=connect,
            operation_timeout=1,
            drain_timeout=0.01,
        )
        task = asyncio.create_task(transport.read_bytes("100", "status.txt", max_bytes=32))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(transport.observed_status("100")["active_operations"], 1)

        release.set()
        for _ in range(100):
            if transport.observed_status("100")["active_operations"] == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)
        self.assertTrue(ftp.closed.is_set())

    async def test_cancelled_connection_rejection_cannot_retry_or_write(self) -> None:
        credential_calls = 0
        connection_calls = 0
        entered = threading.Event()
        release = threading.Event()
        files: dict[str, bytes] = {}

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            nonlocal credential_calls
            del service_id
            credential_calls += 1
            password = "stale" if credential_calls == 1 else "fresh"
            return NitradoFtpCredentials("ftp.example.test", 21, "user", password, False)

        def connect(creds: NitradoFtpCredentials, secure: bool) -> MemoryFtp:
            nonlocal connection_calls
            del creds, secure
            connection_calls += 1
            if connection_calls == 1:
                entered.set()
                release.wait(1)
            return MemoryFtp(files, password="fresh")

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=connect,
            operation_timeout=1,
            drain_timeout=0.01,
        )
        task = asyncio.create_task(transport.write_bytes("100", "danger.txt", b"must-not-write"))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if transport.observed_status("100")["active_operations"] == 0:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(credential_calls, 1)
        self.assertEqual(connection_calls, 1)
        self.assertNotIn("danger.txt", files)
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)

    async def test_operations_begin_at_authenticated_pwd_root(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        ftp = MemoryFtp({"status.txt": b"okay"})
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )
        await transport.read_bytes("100", "status.txt", max_bytes=32)
        self.assertEqual(ftp.cwd_calls[0], "/home/account")

    async def test_tree_snapshot_applies_timeout_per_file_not_to_whole_tree(self) -> None:
        class SlowFtp(MemoryFtp):
            def retrbinary(self, command: str, callback: Any) -> None:
                time.sleep(0.04)
                name = command.removeprefix("RETR ")
                parent = self.cwd_value.removeprefix("/home/account").strip("/")
                path = f"{parent}/{name}" if parent else name
                callback(self.files[path])

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: SlowFtp(
                {
                    "World/Level.sav": b"level",
                    "World/LevelMeta.sav": b"meta",
                    "World/WorldOption.sav": b"option",
                }
            ),
            operation_timeout=0.08,
            drain_timeout=0.5,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        snapshot = await service.snapshot_tree("100", "World")
        try:
            self.assertEqual(
                tuple(item.path for item in snapshot.manifest.entries),
                ("Level.sav", "LevelMeta.sav", "WorldOption.sav"),
            )
        finally:
            snapshot.close()

    async def test_tree_snapshot_uses_one_passive_listing_per_directory(self) -> None:
        """Model the production cost: every MLSD/RETR opens a data channel."""

        class BudgetFtp(MemoryFtp):
            data_channel_limit = 266

            def _require_budget(self) -> None:
                if len(self.mlsd_calls) + len(self.retrbinary_calls) >= self.data_channel_limit:
                    raise TimeoutError("simulated passive data-channel exhaustion")

            def mlsd(self, path: str = "", facts: list[str] | None = None):
                self._require_budget()
                return super().mlsd(path, facts)

            def retrbinary(self, command: str, callback: Any) -> None:
                self._require_budget()
                super().retrbinary(command, callback)

        root = "game/Pal/Saved/SaveGames/0/world-id"
        player_files = {f"{root}/Players/{index:04d}.sav": f"player-{index}".encode() for index in range(250)}
        ftp = BudgetFtp(
            {
                f"{root}/Level.sav": b"level",
                f"{root}/LevelMeta.sav": b"meta",
                f"{root}/Guilds/guild.sav": b"guild",
                f"{root}/Nested/a/b/deep.sav": b"deep",
                **player_files,
            }
        )

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport

        snapshot = await service.snapshot_tree("100", root)
        try:
            self.assertEqual(len(snapshot.manifest.entries), 254)
            self.assertEqual(snapshot.manifest.entries[0].path, "Guilds/guild.sav")
            self.assertEqual(snapshot.manifest.entries[-1].path, "Players/0249.sav")
            # Positioning proves each root ancestor once.  The cursor then
            # lists each of six visited directories exactly once, regardless
            # of the 254 files spread across siblings and four levels.
            self.assertEqual(len(ftp.mlsd_calls), len(root.split("/")) + 6)
            self.assertEqual(len(ftp.retrbinary_calls), 254)
            self.assertEqual(len(ftp.mlst_calls), 259)
            self.assertEqual(
                len(ftp.mlsd_calls) + len(ftp.retrbinary_calls),
                len(root.split("/")) + 260,
            )
        finally:
            snapshot.close()

    async def test_tree_snapshot_excludes_backup_subtree_before_descent_or_download(self) -> None:
        root = "game/Pal/Saved/SaveGames/0/world-id"
        ftp = MemoryFtp(
            {
                f"{root}/Level.sav": b"level",
                f"{root}/Players/one.sav": b"player",
                f"{root}/backup/world/2026.08.24/Level.sav": b"old-level",
                f"{root}/backup/world/2026.08.24/Players/one.sav": b"old-player",
            }
        )

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        events: list[tuple[str, dict[str, Any]]] = []
        snapshot = await service.snapshot_tree(
            "100",
            root,
            excluded_paths=("backup",),
            progress=lambda event, details: events.append((event, dict(details))),
        )
        try:
            self.assertEqual(
                tuple(item.path for item in snapshot.manifest.entries),
                ("Level.sav", "Players/one.sav"),
            )
            self.assertEqual(len(ftp.retrbinary_calls), 2)
            self.assertFalse(any("backup" in call.casefold() for call in ftp.retrbinary_calls))
            self.assertTrue(any(event == "path_excluded" for event, _details in events))
            self.assertEqual(sum(event == "file_downloaded" for event, _details in events), 2)
        finally:
            snapshot.close()

    async def test_tree_cursor_refuses_file_not_in_current_mlsd_result(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        ftp = MemoryFtp({"World/Level.sav": b"level", "World/unlisted.sav": b"secret"})
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )
        async with transport.session("100") as session:
            await session.position_tree_cursor("World")
            entries = await session.list_tree_cursor("World")
            self.assertEqual({entry.name for entry in entries}, {"Level.sav", "unlisted.sav"})
            session.session.tree_entries.pop("unlisted.sav")
            with self.assertRaisesRegex(FileTransportSecurityError, "not classified"):
                await session.read_tree_cursor_file("World", "unlisted.sav", max_bytes=32)

    async def test_tree_cursor_rechecks_type_before_retr(self) -> None:
        class SwappedFtp(MemoryFtp):
            def sendcmd(self, command: str) -> str:
                name = command.removeprefix("MLST ")
                self.mlst_calls.append(name)
                return f"250-Listing {name}\n type=OS.unix=slink; {name}\n250 End"

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        ftp = SwappedFtp({"World/Level.sav": b"level"})
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )
        async with transport.session("100") as session:
            await session.position_tree_cursor("World")
            await session.list_tree_cursor("World")
            with self.assertRaisesRegex(FileTransportSecurityError, "unsupported FTP entry type"):
                await session.read_tree_cursor_file("World", "Level.sav", max_bytes=32)
        self.assertEqual(ftp.retrbinary_calls, [])

    async def test_tree_cursor_rechecks_directory_type_before_cwd(self) -> None:
        class SwappedDirectoryFtp(MemoryFtp):
            def sendcmd(self, command: str) -> str:
                name = command.removeprefix("MLST ")
                self.mlst_calls.append(name)
                kind = "OS.unix=slink" if name == "Players" else "file"
                return f"250-Listing {name}\n type={kind}; {name}\n250 End"

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        ftp = SwappedDirectoryFtp({"World/Level.sav": b"level", "World/Players/one.sav": b"player"})
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with self.assertRaisesRegex(FileTransportSecurityError, "unsupported FTP entry type"):
            await service.snapshot_tree("100", "World")
        self.assertNotIn("Players", ftp.cwd_calls)

    async def test_snapshot_timeout_closes_session_and_fresh_work_succeeds(self) -> None:
        class TimeoutMlsdFtp(MemoryFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                del path, facts
                raise TimeoutError("passive MLSD timed out")

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        first = TimeoutMlsdFtp({"World/Level.sav": b"level"})
        second = MemoryFtp({"World/Level.sav": b"level"})
        instances = iter((first, second))
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: next(instances),
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with self.assertRaisesRegex(FileTransportError, "TimeoutError"):
            await service.snapshot_tree("100", "World")
        self.assertTrue(first.closed.is_set())
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)
        snapshot = await service.snapshot_tree("100", "World")
        try:
            self.assertEqual(snapshot.files["Level.sav"], b"level")
        finally:
            snapshot.close()

    async def test_mid_retr_timeout_returns_no_partial_snapshot_and_recovers(self) -> None:
        class TimeoutRetrFtp(MemoryFtp):
            def retrbinary(self, command: str, callback: Any) -> None:
                callback(b"partial")
                raise TimeoutError("passive RETR timed out")

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        first = TimeoutRetrFtp({"World/Level.sav": b"level"})
        second = MemoryFtp({"World/Level.sav": b"level"})
        instances = iter((first, second))
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: next(instances),
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with self.assertRaisesRegex(FileTransportError, "TimeoutError"):
            await service.snapshot_tree("100", "World")
        self.assertTrue(first.closed.is_set())
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)
        snapshot = await service.snapshot_tree("100", "World")
        try:
            self.assertEqual(snapshot.manifest.total_bytes, 5)
        finally:
            snapshot.close()

    async def test_snapshot_operation_budget_aborts_blocked_mlsd_and_drains_worker(self) -> None:
        class BlockingMlsdFtp(MemoryFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                del path, facts
                self.closed.wait(1)
                raise OSError("socket closed")

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        first = BlockingMlsdFtp({"World/Level.sav": b"level"})
        second = MemoryFtp({"World/Level.sav": b"level"})
        instances = iter((first, second))
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: next(instances),
            operation_timeout=0.03,
            drain_timeout=0.5,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with self.assertRaisesRegex(FileTransportError, "execution budget"):
            await service.snapshot_tree("100", "World")
        self.assertTrue(first.closed.is_set())
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)
        snapshot = await service.snapshot_tree("100", "World")
        snapshot.close()

    async def test_snapshot_operation_budget_aborts_mid_retr_and_drains_worker(self) -> None:
        class BlockingRetrFtp(MemoryFtp):
            def retrbinary(self, command: str, callback: Any) -> None:
                callback(b"partial")
                self.closed.wait(1)
                raise OSError("socket closed")

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        first = BlockingRetrFtp({"World/Level.sav": b"level"})
        second = MemoryFtp({"World/Level.sav": b"level"})
        instances = iter((first, second))
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: next(instances),
            operation_timeout=0.03,
            drain_timeout=0.5,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with self.assertRaisesRegex(FileTransportError, "execution budget"):
            await service.snapshot_tree("100", "World")
        self.assertTrue(first.closed.is_set())
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)
        snapshot = await service.snapshot_tree("100", "World")
        try:
            self.assertEqual(snapshot.files["Level.sav"], b"level")
        finally:
            snapshot.close()

    async def test_snapshot_operation_budget_aborts_blocked_mlst_and_drains_worker(self) -> None:
        class BlockingMlstFtp(MemoryFtp):
            def sendcmd(self, command: str) -> str:
                if command == "MLST Level.sav":
                    self.closed.wait(1)
                    raise OSError("socket closed")
                return super().sendcmd(command)

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        first = BlockingMlstFtp({"World/Level.sav": b"level"})
        second = MemoryFtp({"World/Level.sav": b"level"})
        instances = iter((first, second))
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: next(instances),
            operation_timeout=0.03,
            drain_timeout=0.5,
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with self.assertRaisesRegex(FileTransportError, "execution budget"):
            await service.snapshot_tree("100", "World")
        self.assertTrue(first.closed.is_set())
        self.assertEqual(transport.observed_status("100")["active_operations"], 0)
        snapshot = await service.snapshot_tree("100", "World")
        snapshot.close()

    async def test_nested_snapshot_rejects_hostile_mlsd_entries(self) -> None:
        variants = {
            "symlink": (("evil.sav", {"type": "OS.unix=slink"}),),
            "missing": (("evil.sav", {}),),
            "collision": (("evil.sav", {"type": "file"}), ("EVIL.SAV", {"type": "file"})),
        }
        for label, hostile in variants.items():
            with self.subTest(label=label):

                class HostileNestedFtp(MemoryFtp):
                    hostile_entries = hostile

                    def mlsd(self, path: str = "", facts: list[str] | None = None):
                        if self.cwd_value.endswith("/Players"):
                            return iter(self.hostile_entries)
                        return super().mlsd(path, facts)

                async def credentials(service_id: str) -> NitradoFtpCredentials:
                    del service_id
                    return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

                transport = self.transport(
                    credentials,
                    plaintext_consent={"100"},
                    endpoint_resolver=lambda host, port: ("203.0.113.10",),
                    connection_factory=lambda creds, secure: HostileNestedFtp(
                        {"World/Level.sav": b"level", "World/Players/one.sav": b"player"}
                    ),
                )

                class Client:
                    pass

                service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
                await service.ftp.async_close()
                service.ftp = transport
                with self.assertRaises(FileTransportSecurityError):
                    await service.snapshot_tree("100", "World")

    async def test_snapshot_bounds_directories_and_single_directory_entries(self) -> None:
        class VirtualDirectoriesFtp(MemoryFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                if self.cwd_value == "/home/account":
                    return iter((("World", {"type": "dir"}),))
                if self.cwd_value == "/home/account/World":
                    return iter((f"dir-{index}", {"type": "dir"}) for index in range(3))
                return iter(())

            def sendcmd(self, command: str) -> str:
                name = command.removeprefix("MLST ")
                return f"250-Listing {name}\n type=dir; {name}\n250 End"

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        closed_spools: list[SpoolTreeFiles] = []

        class TrackingSpool(SpoolTreeFiles):
            def close(self) -> None:
                super().close()
                closed_spools.append(self)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: VirtualDirectoriesFtp(),
        )

        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport
        with (
            patch("custom_components.nitrado_gameserver.filesystem.SpoolTreeFiles", TrackingSpool),
            self.assertRaisesRegex(FileTransportError, "directory-count limit"),
        ):
            await service.snapshot_tree("100", "World", max_directories=2)
        self.assertTrue(closed_spools and all(spool.closed for spool in closed_spools))

        class HugeListingFtp(VirtualDirectoriesFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                if self.cwd_value == "/home/account":
                    return iter((("World", {"type": "dir"}),))
                return iter((f"dir-{index}", {"type": "dir"}) for index in range(DEFAULT_MAX_DIRECTORY_ENTRIES + 1))

        huge = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: HugeListingFtp(),
        )
        service.ftp = huge
        closed_spools.clear()
        with (
            patch("custom_components.nitrado_gameserver.filesystem.SpoolTreeFiles", TrackingSpool),
            self.assertRaisesRegex(FileTransportError, "entry-count limit"),
        ):
            await service.snapshot_tree("100", "World")
        self.assertTrue(closed_spools and all(spool.closed for spool in closed_spools))

    async def test_mlsd_explicitly_requests_type_fact(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        ftp = MemoryFtp({"status.txt": b"okay"})
        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: ftp,
        )

        self.assertEqual(await transport.read_bytes("100", "status.txt", max_bytes=32), b"okay")
        self.assertEqual(ftp.mlsd_calls, [("", ("type",))])

    async def test_mlsd_symlink_type_fails_closed(self) -> None:
        class SymlinkFtp(MemoryFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                del path, facts
                return iter((("world", {"type": "OS.unix=slink"}),))

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: SymlinkFtp(),
        )
        with self.assertRaises(FileTransportSecurityError):
            await transport.list_directory("100", "")

    async def test_mlsd_missing_type_fact_fails_closed(self) -> None:
        class MissingTypeFtp(MemoryFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                del path, facts
                return iter((("restart.log", {}),))

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: MissingTypeFtp(),
        )
        with self.assertRaisesRegex(FileTransportSecurityError, "unsupported FTP entry type: unknown"):
            await transport.list_directory("100", "")

    async def test_mlsd_case_ambiguous_names_fail_closed(self) -> None:
        class AmbiguousFtp(MemoryFtp):
            def mlsd(self, path: str = "", facts: list[str] | None = None):
                del path, facts
                return iter((("Level.sav", {"type": "file"}), ("level.sav", {"type": "file"})))

        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", False)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
            connection_factory=lambda creds, secure: AmbiguousFtp(),
        )
        with self.assertRaisesRegex(FileTransportSecurityError, "case-ambiguous"):
            await transport.list_directory("100", "")

    async def test_certificate_failure_never_downgrades(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", None)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
        )
        with (
            patch.object(
                transport,
                "_connect_ftps",
                side_effect=ssl.SSLCertVerificationError("bad certificate"),
            ),
            patch.object(transport, "_connect_plaintext") as plaintext,
            self.assertRaises(FileTransportSecurityError),
        ):
            await transport.read_bytes("100", "status.txt", max_bytes=32)
        plaintext.assert_not_called()

    async def test_protocol_failure_downgrades_only_with_explicit_consent(self) -> None:
        async def credentials(service_id: str) -> NitradoFtpCredentials:
            del service_id
            return NitradoFtpCredentials("ftp.example.test", 21, "user", "secret", None)

        transport = self.transport(
            credentials,
            plaintext_consent={"100"},
            endpoint_resolver=lambda host, port: ("203.0.113.10",),
        )
        session_ftp = MemoryFtp({"status.txt": b"okay"})
        session = type(
            "Session",
            (),
            {"ftp": session_ftp, "root": "/home/account", "kind": FileTransportKind.FTP},
        )()
        with (
            patch.object(transport, "_connect_ftps", side_effect=ssl.SSLError("unsupported")),
            patch.object(transport, "_connect_plaintext", return_value=session) as plaintext,
        ):
            self.assertEqual(await transport.read_bytes("100", "status.txt", max_bytes=32), b"okay")
        plaintext.assert_called_once()


class HttpMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_construction_does_not_require_ftp_capability(self) -> None:
        class HttpOnlyClient:
            pass

        service = NitradoFilesystemService(HttpOnlyClient())  # type: ignore[arg-type]
        self.assertFalse(service.observed_status("100")["ftp"]["secure_transport_observed"])
        with self.assertRaisesRegex(FileTransportError, "FTP is unavailable"):
            await service.ftp.read_bytes("100", "Config.ini", max_bytes=32)
        await service.async_close()

    async def test_transport_discovers_and_reuses_account_specific_root(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.list_calls: list[str | None] = []
                self.download_paths: list[str] = []

            async def list_files(self, service_id: str, directory: str | None = None):
                self.assert_service(service_id)
                self.list_calls.append(directory)
                return {
                    "entries": [
                        {
                            "path": "/games/ni9352260_116902/ftproot/palworldxb",
                            "type": "dir",
                        }
                    ]
                }

            async def download_file(self, service_id: str, path: str) -> str:
                self.assert_service(service_id)
                self.download_paths.append(path)
                return "content"

            @staticmethod
            def assert_service(service_id: str) -> None:
                if service_id != "100":
                    raise AssertionError(service_id)

        client = Client()
        transport = NitradoHttpFileTransport(client)  # type: ignore[arg-type]
        self.assertEqual(await transport.read_text("100", "palworldxb/Config.ini"), "content")
        self.assertEqual(client.list_calls, [None])
        self.assertEqual(
            client.download_paths,
            ["/games/ni9352260_116902/ftproot/palworldxb/Config.ini"],
        )
        self.assertTrue(transport.observed_status("100")["root_mapping_proven"])
        self.assertEqual(
            await transport.read_text(
                "100",
                "/games/ni9352260_116902/ftproot/palworldxb/Other.ini",
            ),
            "content",
        )
        self.assertEqual(client.list_calls, [None])

    async def test_profile_v1_text_read_falls_back_to_authoritative_ftp(self) -> None:
        class Client:
            async def list_files(self, service_id: str, directory: str | None = None):
                del service_id, directory
                return {
                    "entries": [
                        {
                            "path": "/games/account/ftproot/palworldxb",
                            "type": "dir",
                        }
                    ]
                }

            async def download_file(self, service_id: str, path: str) -> str:
                del service_id, path
                raise NitradoApiError("HTTP 502")

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = MemoryTreeTransport({"palworldxb/Config.ini": b"ftp-content"})  # type: ignore[assignment]

        self.assertEqual(
            await service.download_file("100", "palworldxb/Config.ini"),
            "ftp-content",
        )

    async def test_absolute_path_outside_proven_root_is_rejected(self) -> None:
        class Client:
            async def list_files(self, service_id: str, directory: str | None = None):
                del service_id, directory
                return {
                    "entries": [
                        {
                            "path": "/games/ni9352260_116902/ftproot/palworldxb",
                            "type": "dir",
                        }
                    ]
                }

        transport = NitradoHttpFileTransport(Client())  # type: ignore[arg-type]
        with self.assertRaises(FileTransportSecurityError):
            await transport.provider_path("100", "/games/someone_else/ftproot/secrets")

    async def test_compatibility_upload_rejects_provider_absolute_path(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.content = "original"
                self.uploads: list[str] = []

            async def list_files(self, service_id: str, directory: str | None = None):
                del service_id, directory
                return {
                    "entries": [
                        {
                            "path": "/games/ni9352260_116902/ftproot/palworldxb",
                            "type": "dir",
                        }
                    ]
                }

            async def download_file(self, service_id: str, path: str) -> str:
                del service_id, path
                return self.content

            async def upload_text_file(self, service_id: str, path: str, content: str) -> None:
                del service_id
                self.uploads.append(path)
                self.content = content

            async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
                raise AssertionError(service_id)

        client = Client()
        service = NitradoFilesystemService(client)  # type: ignore[arg-type]
        await service.ftp.async_close()

        class TransactionFtp:
            def __init__(self) -> None:
                self.files: dict[str, bytes] = {}
                self._observed_transport = {"100": FileTransportKind.FTPS}

            async def read_optional_bytes(self, service_id: str, path: str, *, max_bytes: int):
                del service_id, max_bytes
                return self.files.get(path)

            async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
                del service_id, max_bytes
                return self.files[path]

            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                del service_id
                self.files[path] = content

            async def rename(self, service_id: str, source: str, target: str) -> None:
                del service_id
                if source not in self.files:
                    raise FileTransportError("missing")
                self.files[target] = self.files.pop(source)

            async def delete_file(self, service_id: str, path: str) -> None:
                del service_id
                if path not in self.files:
                    raise FileTransportError("missing")
                del self.files[path]

        fake = TransactionFtp()
        service.ftp = fake  # type: ignore[assignment]
        with self.assertRaises(FileTransportSecurityError):
            await service.upload_text_file(
                "100",
                "/games/ni9352260_116902/ftproot/palworldxb/Config.ini",
                "updated",
            )
        self.assertEqual(fake.files, {})
        self.assertEqual(client.uploads, [])


class VerifiedWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_ftp_mismatch_restores_original_exact_content(self) -> None:
        class TransactionFtp:
            def __init__(self) -> None:
                self.files = {"Config.ini": b"original"}
                self._corrupt_final_read = True
                self._observed_transport = {"100": FileTransportKind.FTPS}

            async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
                del service_id, max_bytes
                if path == "Config.ini" and self.files[path] == b"proposed" and self._corrupt_final_read:
                    self._corrupt_final_read = False
                    return b"corrupt"
                return self.files[path]

            async def read_optional_bytes(self, service_id: str, path: str, *, max_bytes: int):
                del service_id, max_bytes
                return self.files.get(path)

            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                del service_id
                self.files[path] = content

            async def rename(self, service_id: str, source: str, target: str) -> None:
                del service_id
                if source not in self.files:
                    raise FileTransportError("missing")
                self.files[target] = self.files.pop(source)

            async def delete_file(self, service_id: str, path: str) -> None:
                del service_id
                if path not in self.files:
                    raise FileTransportError("missing")
                del self.files[path]

        class Client:
            async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
                raise AssertionError(service_id)

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        fake = TransactionFtp()
        service.ftp = fake  # type: ignore[assignment]
        with self.assertRaises(FileVerificationError):
            await service.write_bytes_verified("100", "Config.ini", b"proposed", require_ftp=True)
        self.assertEqual(fake.files, {"Config.ini": b"original"})

    async def test_http_mismatch_restores_original_exact_content(self) -> None:
        class Client:
            async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
                raise AssertionError(service_id)

        class TransactionHttp:
            def __init__(self) -> None:
                self.content = b"original"
                self.fail_once = True

            async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
                del service_id, path, max_bytes
                if self.content == b"proposed" and self.fail_once:
                    self.fail_once = False
                    return b"corrupt"
                return self.content

            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                del service_id, path
                self.content = content

            async def relative_path(self, service_id: str, path: str) -> str:
                del service_id
                return canonical_relative_path(path)

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        fake = TransactionHttp()
        service.http = fake  # type: ignore[assignment]
        with self.assertRaises(FileVerificationError):
            await service.write_bytes_verified("100", "Config.ini", b"proposed")
        self.assertEqual(fake.content, b"original")

    async def test_journal_records_verified_commit_and_blocks_unresolved_work(self) -> None:
        class Client:
            async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
                raise AssertionError(service_id)

        class TransactionHttp:
            def __init__(self) -> None:
                self.content = b"original"

            async def relative_path(self, service_id: str, path: str) -> str:
                del service_id
                return canonical_relative_path(path)

            async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
                del service_id, path, max_bytes
                return self.content

            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                del service_id, path
                self.content = content

        journal = FilesystemTransactionJournal(MemoryMetadataStore(), MemoryBlobStore())
        service = NitradoFilesystemService(
            Client(),  # type: ignore[arg-type]
            account_entry_id="entry-1",
            journal=journal,
        )
        await service.ftp.async_close()
        fake = TransactionHttp()
        service.http = fake  # type: ignore[assignment]
        await service.write_bytes_verified("100", "Config.ini", b"proposed")
        self.assertEqual(fake.content, b"proposed")
        self.assertEqual(await journal.async_recovery_candidates(), ())
        self.assertEqual((await journal.async_diagnostics())["unresolved_count"], 0)

        await journal.async_prepare(
            ServiceReference("entry-1", "100"),
            FileTransactionOperation.WRITE_FILE,
            "Config.ini",
            recovery_content=b"proposed",
        )
        with self.assertRaisesRegex(FileTransportError, "unresolved"):
            await service.write_bytes_verified("100", "Config.ini", b"other")
        self.assertEqual(fake.content, b"proposed")

    async def test_journal_records_exact_rollback_after_failed_verification(self) -> None:
        class Client:
            async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
                raise AssertionError(service_id)

        class MismatchHttp:
            def __init__(self) -> None:
                self.content = b"original"
                self.fail_once = True

            async def relative_path(self, service_id: str, path: str) -> str:
                del service_id
                return canonical_relative_path(path)

            async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
                del service_id, path, max_bytes
                if self.content == b"proposed" and self.fail_once:
                    self.fail_once = False
                    return b"corrupt"
                return self.content

            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                del service_id, path
                self.content = content

        journal = FilesystemTransactionJournal(MemoryMetadataStore(), MemoryBlobStore())
        service = NitradoFilesystemService(
            Client(),  # type: ignore[arg-type]
            account_entry_id="entry-1",
            journal=journal,
        )
        await service.ftp.async_close()
        fake = MismatchHttp()
        service.http = fake  # type: ignore[assignment]
        with self.assertRaises(FileVerificationError):
            await service.write_bytes_verified("100", "Config.ini", b"proposed")
        diagnostics = await journal.async_diagnostics()
        self.assertEqual(fake.content, b"original")
        self.assertEqual(diagnostics["unresolved_count"], 0)
        self.assertEqual(diagnostics["records"][0]["disposition"], "rolled_back")

    async def test_restart_recovery_restores_mutating_file_exactly(self) -> None:
        class Client:
            async def fetch_ftp_credentials(self, service_id: str) -> NitradoFtpCredentials:
                raise AssertionError(service_id)

        class RecoveryFtp:
            def __init__(self) -> None:
                self.files = {"Config.ini": b"partial-new-value"}

            async def list_directory(self, service_id: str, directory: str):
                del service_id, directory
                return tuple(SimpleNamespace(name=path) for path in self.files)

            async def read_bytes(self, service_id: str, path: str, *, max_bytes: int) -> bytes:
                del service_id, max_bytes
                return self.files[path]

            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                del service_id
                self.files[path] = bytes(content)

            async def delete_file(self, service_id: str, path: str) -> None:
                del service_id
                self.files.pop(path)

        journal = FilesystemTransactionJournal(MemoryMetadataStore(), MemoryBlobStore())
        record = await journal.async_prepare(
            ServiceReference("entry-1", "100"),
            FileTransactionOperation.WRITE_FILE,
            "Config.ini",
            recovery_content=encode_file_recovery(b"original"),
        )
        await journal.async_mark_mutating(record.transaction_id)
        service = NitradoFilesystemService(
            Client(),  # type: ignore[arg-type]
            account_entry_id="entry-1",
            journal=journal,
        )
        await service.ftp.async_close()
        recovery_ftp = RecoveryFtp()
        service.ftp = recovery_ftp  # type: ignore[assignment]
        stopped_checks = 0

        async def require_stopped() -> None:
            nonlocal stopped_checks
            stopped_checks += 1

        completed = await service.recover_transactions("100", require_stopped=require_stopped)

        self.assertEqual(completed, (record.transaction_id,))
        self.assertEqual(recovery_ftp.files, {"Config.ini": b"original"})
        self.assertGreaterEqual(stopped_checks, 2)
        self.assertEqual(await journal.async_recovery_candidates(), ())

    def test_tree_recovery_codec_round_trips_exact_manifest(self) -> None:
        from custom_components.nitrado_gameserver.filesystem import (
            FileTreeManifest,
            FileTreeSnapshot,
        )

        files = {"World/Level.sav": b"level", "World/Players/one.sav": b"player"}
        snapshot = FileTreeSnapshot(files, FileTreeManifest.from_files(files))
        decoded = decode_tree_snapshot(encode_tree_snapshot(snapshot))
        self.assertEqual(decoded, snapshot)


class VerifiedTreeTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def _service(self, transport: MemoryTreeTransport) -> NitradoFilesystemService:
        class Client:
            pass

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        await service.ftp.async_close()
        service.ftp = transport  # type: ignore[assignment]
        return service

    async def test_tree_snapshot_counts_all_nested_files_and_exact_bytes(self) -> None:
        transport = MemoryTreeTransport(
            {
                "World/Level.sav": b"level",
                "World/Players/one.sav": b"player",
                "World/Players/two.sav": b"other",
            }
        )
        service = await self._service(transport)

        snapshot = await service.snapshot_tree("100", "World", max_files=3, max_bytes=16)
        try:
            self.assertEqual(
                tuple(entry.path for entry in snapshot.manifest.entries),
                ("Level.sav", "Players/one.sav", "Players/two.sav"),
            )
            self.assertEqual(snapshot.manifest.total_bytes, 16)
            self.assertEqual(snapshot.files["Players/one.sav"], b"player")
        finally:
            snapshot.close()

    async def test_authoritative_read_waits_until_partial_mutation_finishes(self) -> None:
        service = await self._service(MemoryTreeTransport({"World/Level.sav": b"old"}))
        mutation_entered = asyncio.Event()
        release_mutation = asyncio.Event()
        read_entered = asyncio.Event()

        async def mutation() -> None:
            async with service.mutation("100"):
                mutation_entered.set()
                await release_mutation.wait()

        async def read() -> None:
            async with service.authoritative_read("100"):
                read_entered.set()

        mutation_task = asyncio.create_task(mutation())
        await mutation_entered.wait()
        read_task = asyncio.create_task(read())
        await asyncio.sleep(0)
        self.assertFalse(read_entered.is_set())
        release_mutation.set()
        await asyncio.gather(mutation_task, read_task)
        self.assertTrue(read_entered.is_set())

    async def test_profile_v1_download_waits_until_partial_mutation_finishes(self) -> None:
        class Client:
            async def download_file(self, service_id: str, path: str) -> str:
                del service_id, path
                return "complete"

        service = NitradoFilesystemService(Client())  # type: ignore[arg-type]
        mutation_entered = asyncio.Event()
        release_mutation = asyncio.Event()

        async def mutation() -> None:
            async with service.mutation("100"):
                mutation_entered.set()
                await release_mutation.wait()

        mutation_task = asyncio.create_task(mutation())
        await mutation_entered.wait()
        read_task = asyncio.create_task(service.download_file("100", "World/Level.sav"))
        await asyncio.sleep(0)
        self.assertFalse(read_task.done())
        release_mutation.set()
        await mutation_task
        self.assertEqual(await read_task, "complete")
        await service.async_close()

    async def test_tree_snapshot_enforces_global_file_count_across_directories(self) -> None:
        transport = MemoryTreeTransport(
            {
                "World/one/a.sav": b"a",
                "World/two/b.sav": b"b",
            }
        )
        service = await self._service(transport)

        with self.assertRaisesRegex(FileTransportError, "file-count limit"):
            await service.snapshot_tree("100", "World", max_files=1)

    async def test_replace_tree_requires_two_stopped_checks_and_exact_verification(self) -> None:
        transport = MemoryTreeTransport(
            {
                "World/old.sav": b"old",
                "Outside/untouched.txt": b"safe",
            }
        )
        service = await self._service(transport)
        stopped_checks = 0

        async def require_stopped() -> None:
            nonlocal stopped_checks
            stopped_checks += 1

        result = await service.replace_tree(
            "100",
            "World",
            {"Level.sav": b"new-level", "Players/one.sav": b"new-player"},
            require_stopped=require_stopped,
        )

        self.assertEqual(stopped_checks, 2)
        self.assertEqual(result.transport, FileTransportKind.FTPS)
        self.assertEqual(
            transport.files,
            {
                "World/Level.sav": b"new-level",
                "World/Players/one.sav": b"new-player",
                "Outside/untouched.txt": b"safe",
            },
        )

    async def test_replace_tree_verification_failure_restores_exact_previous_tree(self) -> None:
        original = {
            "World/Level.sav": b"old-level",
            "World/Players/one.sav": b"old-player",
            "Outside/untouched.txt": b"safe",
        }
        transport = MemoryTreeTransport(original, corrupt_new_writes=True)
        service = await self._service(transport)

        with self.assertRaises(FileVerificationError):
            await service.replace_tree(
                "100",
                "World",
                {"Level.sav": b"new-level", "Players/two.sav": b"new-player"},
            )

        self.assertEqual(transport.files, original)

    async def test_replace_tree_refuses_stale_expected_manifest_inside_mutation_lock(self) -> None:
        original = {"World/Level.sav": b"changed-after-plan"}
        transport = MemoryTreeTransport(original)
        service = await self._service(transport)
        planned = FileTreeManifest.from_files({"Level.sav": b"old-at-plan-time"})

        with self.assertRaisesRegex(FileConcurrentModificationError, "changed after planning"):
            await service.replace_tree(
                "100",
                "World",
                {"Level.sav": b"new"},
                expected_current=planned,
            )

        self.assertEqual(transport.files, original)

    async def test_revocation_before_first_mutation_does_not_touch_remote_tree(self) -> None:
        original = {"World/Level.sav": b"old"}
        transport = MemoryTreeTransport(original)
        service = await self._service(transport)

        def revoked() -> None:
            raise RuntimeError("revoked")

        with self.assertRaisesRegex(RuntimeError, "revoked"):
            await service.replace_tree(
                "100",
                "World",
                {"Level.sav": b"new"},
                mutation_started=revoked,
            )

        self.assertEqual(transport.files, original)
        self.assertEqual(transport.delete_tree_calls, 0)

    async def test_replace_tree_rollback_failure_is_never_reported_as_success(self) -> None:
        class BrokenRollbackTransport(MemoryTreeTransport):
            async def write_bytes(self, service_id: str, path: str, content: bytes) -> None:
                await super().write_bytes(service_id, path, content)
                self.files[path] += b"-always-corrupt"

        transport = BrokenRollbackTransport({"World/Level.sav": b"old"})
        service = await self._service(transport)

        with self.assertRaisesRegex(FileRecoveryError, "rollback could not be verified"):
            await service.replace_tree("100", "World", {"Level.sav": b"new"})


if __name__ == "__main__":
    unittest.main()
