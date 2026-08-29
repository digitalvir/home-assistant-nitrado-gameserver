from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct
import threading
import unittest
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.nitrado_gameserver.extensions import ProfileExtensionError
from custom_components.nitrado_gameserver.filesystem import (
    FileTreeManifest,
    FileTreeSnapshot,
    SpoolTreeFiles,
    tree_manifest_sha256,
)
from custom_components.nitrado_gameserver.plugins.base import BaseGameProfile, SaveBundleDeclaration
from custom_components.nitrado_gameserver.plugins.registry import profile_registry_generation
from custom_components.nitrado_gameserver.save_bundles import (
    MAX_ZIP_CENTRAL_DIRECTORY_BYTES,
    SAVE_BUNDLE_MANIFEST,
    SAVE_BUNDLE_SCHEMA,
    _async_run_zip_worker,
    _overlay_manifest,
    _read_upload_zip,
    _write_export_zip,
    async_export_save_bundle,
    async_inspect_save_bundle,
)


def declaration() -> SaveBundleDeclaration:
    return SaveBundleDeclaration(
        key="world",
        name="World",
        root_fn=lambda _context: "game/save",
        required_files=("Level.sav",),
        allowed_suffixes=(".sav",),
        editor_root_files=("Level.sav",),
        editor_root_directories=("Players",),
    )


def editor_declaration() -> SaveBundleDeclaration:
    return SaveBundleDeclaration(
        key="world",
        name="World",
        root_fn=lambda _context: "game/save",
        required_files=("Level.sav",),
        allowed_suffixes=(".sav",),
        excluded_paths=("backup",),
        editor_root_files=("Level.sav",),
        editor_root_directories=("Players",),
    )


def generic_dat_declaration() -> SaveBundleDeclaration:
    return SaveBundleDeclaration(
        key="world",
        name="Generic World",
        root_fn=lambda _context: "game/world",
        required_files=("level.dat",),
        allowed_suffixes=(".dat",),
        editor_root_files=("level.dat",),
        editor_root_directories=("region",),
    )


def archive_bytes(files: dict[str, bytes], *, manifest: dict | None = None) -> io.BytesIO:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        if manifest is not None:
            archive.writestr(SAVE_BUNDLE_MANIFEST, json.dumps(manifest))
        for path, content in files.items():
            archive.writestr(path, content)
    stream.seek(0)
    return stream


def read_upload_zip(stream: io.BytesIO, bundle: SaveBundleDeclaration | None = None):
    """Inspect one Palworld fixture archive through the profile-bound API."""

    return _read_upload_zip(stream, bundle or declaration(), "palworld")


class SaveBundleTests(unittest.TestCase):
    def test_non_palworld_profile_owns_editor_zip_recognition(self) -> None:
        upload = _read_upload_zip(
            archive_bytes({"editor/world/level.dat": b"level"}),
            generic_dat_declaration(),
            "minecraft_like",
        )
        try:
            self.assertEqual(upload.source_prefix, "editor/world")
            self.assertEqual(tuple(upload.files), ("level.dat",))
        finally:
            upload.close()

    def test_portable_manifest_rejects_another_profile_identity(self) -> None:
        manifest = {
            "schema": SAVE_BUNDLE_SCHEMA,
            "profile_id": "another_game",
            "bundle_key": "world",
            "files": [{"path": "Level.sav"}],
        }
        with self.assertRaisesRegex(ProfileExtensionError, "incompatible save-bundle manifest"):
            read_upload_zip(archive_bytes({"Level.sav": b"level"}, manifest=manifest))

    def test_editor_zip_selects_shallow_world_and_preserves_relative_paths(self) -> None:
        upload = read_upload_zip(
            archive_bytes(
                {
                    "editor-output/world/Level.sav": b"level",
                    "editor-output/world/Players/abc.sav": b"player",
                }
            ),
            declaration(),
        )
        try:
            self.assertEqual(upload.source_prefix, "editor-output/world")
            self.assertEqual(tuple(upload.files), ("Level.sav", "Players/abc.sav"))
            self.assertFalse(upload.portable_manifest)
        finally:
            upload.close()

    def test_editor_bundle_rejects_excluded_backup_history(self) -> None:
        with self.assertRaisesRegex(ProfileExtensionError, "profile-excluded history"):
            read_upload_zip(
                archive_bytes(
                    {
                        "editor-output/world/Level.sav": b"level",
                        "editor-output/world/backup/old/Level.sav": b"old",
                    }
                ),
                editor_declaration(),
            )

    def test_portable_bundle_manifest_is_recognized_without_trusting_remote_paths(self) -> None:
        manifest = {
            "schema": SAVE_BUNDLE_SCHEMA,
            "profile_id": "palworld",
            "bundle_key": "world",
            "world_id": "ignored-by-restore",
            "files": [
                {"path": "Level.sav", "size": 5, "sha256": "stale-after-edit"},
                {"path": "Players/abc.sav", "size": 6, "sha256": "stale-after-edit"},
            ],
        }
        upload = read_upload_zip(
            archive_bytes({"Level.sav": b"edited", "Players/abc.sav": b"player"}, manifest=manifest),
            declaration(),
        )
        try:
            self.assertTrue(upload.portable_manifest)
            self.assertEqual(upload.source_prefix, "")
        finally:
            upload.close()

    def test_upload_overlay_never_deletes_files_omitted_by_editor(self) -> None:
        current = FileTreeManifest.from_files(
            {"Level.sav": b"old", "Players/one.sav": b"one", "Players/two.sav": b"two"}
        )
        upload = FileTreeManifest.from_files({"Level.sav": b"new", "Players/three.sav": b"three"})
        proposed, added, replaced, unchanged, preserved = _overlay_manifest(current, upload)
        self.assertEqual(
            {item.path for item in proposed.entries},
            {
                "Level.sav",
                "Players/one.sav",
                "Players/two.sav",
                "Players/three.sav",
            },
        )
        self.assertEqual([item.path for item in replaced], ["Level.sav"])
        self.assertEqual([item.path for item in added], ["Players/three.sav"])
        self.assertEqual(unchanged, ())
        self.assertEqual([item.path for item in preserved], ["Players/one.sav", "Players/two.sav"])

    def test_player_only_editor_zip_is_accepted_as_a_partial_overlay(self) -> None:
        upload = read_upload_zip(
            archive_bytes({"editor/world/Players/abc.sav": b"edited-player"}),
            declaration(),
        )
        try:
            self.assertEqual(upload.source_prefix, "editor/world")
            self.assertEqual(tuple(upload.files), ("Players/abc.sav",))
        finally:
            upload.close()

    def test_export_round_trips_as_an_editor_compatible_bundle(self) -> None:
        files = {"Level.sav": b"level", "Players/abc.sav": b"player"}
        snapshot = FileTreeSnapshot(files, FileTreeManifest.from_files(files))
        runtime = SimpleNamespace(profile=SimpleNamespace(profile_id="palworld"))
        stream = io.BytesIO()
        _write_export_zip(stream, snapshot, declaration(), runtime, "game/SaveGames/0/world-id", 1_800_000_000)
        upload = read_upload_zip(stream)
        try:
            self.assertTrue(upload.portable_manifest)
            self.assertEqual(dict(upload.files), files)
        finally:
            upload.close()

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaisesRegex(ProfileExtensionError, "unsafe or ambiguous path"):
            read_upload_zip(archive_bytes({"../Level.sav": b"evil"}))

    def test_rejects_links(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("Level.sav")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"target")
        stream.seek(0)
        with self.assertRaisesRegex(ProfileExtensionError, "links or unsupported"):
            read_upload_zip(stream)

    def test_rejects_case_collisions_and_ambiguous_worlds(self) -> None:
        with self.assertRaisesRegex(ProfileExtensionError, "case-colliding"):
            read_upload_zip(
                archive_bytes({"Level.sav": b"one", "LEVEL.SAV": b"two"}),
                declaration(),
            )
        with self.assertRaisesRegex(ProfileExtensionError, "multiple ambiguous"):
            read_upload_zip(
                archive_bytes({"one/Level.sav": b"one", "two/Level.sav": b"two"}),
                declaration(),
            )

    def test_rejects_non_save_content_even_with_spoofed_manifest(self) -> None:
        manifest = {
            "schema": SAVE_BUNDLE_SCHEMA,
            "profile_id": "palworld",
            "bundle_key": "world",
            "files": [
                {"path": "Level.sav"},
                {"path": "run-me.exe"},
            ],
        }
        with self.assertRaisesRegex(ProfileExtensionError, "unsupported content"):
            read_upload_zip(
                archive_bytes({"Level.sav": b"level", "run-me.exe": b"evil"}, manifest=manifest),
                declaration(),
            )

    def test_zip_comment_with_eocd_signature_is_rejected_safely(self) -> None:
        stream = archive_bytes({"Level.sav": b"level"})
        with zipfile.ZipFile(stream, "a") as archive:
            archive.comment = b"harmless-comment-PK\x05\x06-not-an-eocd"
        stream.seek(0)
        # Python's own ZipFile parser false-matches this signature in a ZIP
        # comment.  Safe rejection is preferable to maintaining a second ZIP
        # parser merely for an exotic comment shape.
        with self.assertRaisesRegex(ProfileExtensionError, "not a valid, supported ZIP"):
            read_upload_zip(stream)

    def test_appended_fake_eocd_cannot_bypass_geometry(self) -> None:
        stream = archive_bytes({"Level.sav": b"level", "Players/one.sav": b"one"})
        stream.seek(0, 2)
        stream.write(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, 46, 0, 0))
        stream.seek(0)
        with self.assertRaisesRegex(ProfileExtensionError, "geometry"):
            read_upload_zip(stream)

    def test_oversized_central_directory_is_rejected_before_zipfile_materializes_it(self) -> None:
        stream = archive_bytes({"Level.sav": b"level"})
        raw = bytearray(stream.getvalue())
        marker = raw.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(marker, 0)
        struct.pack_into("<L", raw, marker + 12, MAX_ZIP_CENTRAL_DIRECTORY_BYTES + 1)
        with self.assertRaisesRegex(ProfileExtensionError, "central directory exceeds"):
            read_upload_zip(io.BytesIO(raw))

    def test_forged_eocd_count_cannot_bypass_physical_entry_limit(self) -> None:
        stream = archive_bytes({f"Players/{index:04d}.sav": b"x" for index in range(100)})
        raw = bytearray(stream.getvalue())
        marker = raw.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(marker, 0)
        struct.pack_into("<HH", raw, marker + 8, 1, 1)
        with self.assertRaisesRegex(ProfileExtensionError, "entry count is inconsistent"):
            read_upload_zip(io.BytesIO(raw))

    def test_portable_manifest_has_a_dedicated_bounded_read(self) -> None:
        manifest = {
            "schema": SAVE_BUNDLE_SCHEMA,
            "profile_id": "palworld",
            "bundle_key": "world",
            "files": [{"path": "Level.sav"}],
            "padding": "x" * 100,
        }
        with (
            patch("custom_components.nitrado_gameserver.save_bundles.MAX_SAVE_BUNDLE_MANIFEST_BYTES", 32),
            self.assertRaisesRegex(ProfileExtensionError, "manifest exceeds"),
        ):
            read_upload_zip(archive_bytes({"Level.sav": b"level"}, manifest=manifest))


class IntegratedSaveBundleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_zip_worker_is_drained_before_task_releases_ownership(self) -> None:
        entered = threading.Event()
        exited = threading.Event()

        def worker(cancel_event: threading.Event) -> None:
            entered.set()
            cancel_event.wait(1)
            exited.set()

        task = asyncio.create_task(_async_run_zip_worker(worker))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())

    async def test_spooled_snapshot_export_and_same_zip_inspection_are_zero_change(self) -> None:
        files = {
            "Level.sav": b"level",
            "LevelMeta.sav": b"meta",
            "Players/one.sav": b"player-one",
        }

        class BundleProfile(BaseGameProfile):
            profile_id = "bundle_test"
            display_name = "Bundle Test"

            def save_bundles(self):
                return (declaration(),)

        profile = BundleProfile()
        runtime = SimpleNamespace(
            profile=profile,
            profile_manifest=None,
            profile_generation=0,
            profile_registry_generation_seen=profile_registry_generation(),
            state=SimpleNamespace(identity=SimpleNamespace(service_id="service-1")),
            status_fresh=True,
            using_cached_data=False,
            server=SimpleNamespace(raw_status="stopped"),
            service=None,
            extra={},
            profile_extra=lambda: {},
        )

        class Client:
            @asynccontextmanager
            async def authoritative_read(self, service_id: str):
                self.service_id = service_id
                yield

            async def snapshot_tree(self, service_id: str, root: str, **limits):
                del service_id, root, limits
                spool = SpoolTreeFiles()
                entries = tuple(spool.append(path, content) for path, content in sorted(files.items()))
                return FileTreeSnapshot(spool, FileTreeManifest(entries, sum(len(value) for value in files.values())))

        client = Client()
        exported = await async_export_save_bundle(runtime, client, "world", now=1_800_000_000)
        try:
            exported.stream.seek(0)
            with zipfile.ZipFile(exported.stream) as archive:
                self.assertIsNone(archive.testzip())
                metadata = json.loads(archive.read(SAVE_BUNDLE_MANIFEST))
                self.assertEqual(metadata["manifest_sha256"], tree_manifest_sha256(exported.manifest))
                for item in metadata["files"]:
                    content = archive.read(item["path"])
                    self.assertEqual(len(content), item["size"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
            exported.stream.seek(0)
            preview = await async_inspect_save_bundle(
                runtime,
                client,
                "world",
                exported.stream,
                now=1_800_000_000,
            )
            try:
                self.assertFalse(preview.changed)
                self.assertEqual(preview.added, ())
                self.assertEqual(preview.replaced, ())
                self.assertEqual(preview.preserved, ())
                self.assertEqual(len(preview.unchanged), len(files))
                self.assertEqual(tree_manifest_sha256(preview.current), tree_manifest_sha256(preview.proposed))
                self.assertTrue(preview.upload.portable_manifest)
            finally:
                preview.close()
        finally:
            exported.close()


if __name__ == "__main__":
    unittest.main()
