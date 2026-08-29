"""Tests for private Home Assistant recovery-blob persistence."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from custom_components.nitrado_gameserver.ha_filesystem_storage import HomeAssistantRecoveryBlobStore


class _Config:
    def __init__(self, root: str) -> None:
        self._root = root

    def path(self, *parts: str) -> str:
        return str(Path(self._root, *parts))


class _Hass:
    def __init__(self, root: str) -> None:
        self.config = _Config(root)

    async def async_add_executor_job(self, callback, *args):
        return await asyncio.to_thread(callback, *args)


class HomeAssistantRecoveryBlobStoreTests(unittest.TestCase):
    def test_entry_id_cannot_escape_recovery_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            for entry_id in ("..", ".", "entry/escape", "entry\\escape", "entry\nname"):
                with self.subTest(entry_id=entry_id), self.assertRaises(ValueError):
                    HomeAssistantRecoveryBlobStore(_Hass(root), entry_id)

    def test_round_trip_is_private_exclusive_and_bounded(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as root:
                store = HomeAssistantRecoveryBlobStore(_Hass(root), "entry-1", max_blob_bytes=8)
                await store.async_write("recovery-abc", b"original")
                self.assertEqual(await store.async_read("recovery-abc"), b"original")
                self.assertEqual(await store.async_list(), ("recovery-abc",))

                path = Path(root, ".storage", "nitrado_gameserver", "recovery", "entry-1", "recovery-abc")
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

                with self.assertRaises(FileExistsError):
                    await store.async_write("recovery-abc", b"changed")
                with self.assertRaises(ValueError):
                    await store.async_write("recovery-large", b"123456789")
                with self.assertRaises(ValueError):
                    await store.async_read("../escape")

                await store.async_delete("recovery-abc")
                self.assertFalse(path.exists())

                outside = Path(root, "outside")
                outside.mkdir()
                linked_root = Path(root, ".storage", "nitrado_gameserver", "recovery", "entry-2")
                linked_root.symlink_to(outside, target_is_directory=True)
                linked_store = HomeAssistantRecoveryBlobStore(_Hass(root), "entry-2", max_blob_bytes=8)
                with self.assertRaises(OSError):
                    await linked_store.async_write("recovery-link", b"blocked")
                self.assertEqual(list(outside.iterdir()), [])

            with tempfile.TemporaryDirectory() as ancestor_root:
                ancestor_outside = Path(ancestor_root, "ancestor-outside")
                ancestor_outside.mkdir()
                domain_root = Path(ancestor_root, ".storage", "nitrado_gameserver")
                domain_root.parent.mkdir(parents=True)
                domain_root.symlink_to(ancestor_outside, target_is_directory=True)
                ancestor_store = HomeAssistantRecoveryBlobStore(
                    _Hass(ancestor_root),
                    "entry-3",
                    max_blob_bytes=8,
                )
                with self.assertRaises(OSError):
                    await ancestor_store.async_write("recovery-link", b"blocked")
                self.assertEqual(list(ancestor_outside.iterdir()), [])

        asyncio.run(run())

    def test_remove_all_is_exact_idempotent_and_rejects_unexpected_entries(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as root:
                hass = _Hass(root)
                first = HomeAssistantRecoveryBlobStore(hass, "entry-1")
                sibling = HomeAssistantRecoveryBlobStore(hass, "entry-2")
                await first.async_write("recovery-first", b"private")
                await sibling.async_write("recovery-second", b"preserve")

                await first.async_remove_all()
                await first.async_remove_all()
                self.assertEqual(await sibling.async_read("recovery-second"), b"preserve")
                self.assertFalse(Path(root, ".storage", "nitrado_gameserver", "recovery", "entry-1").exists())

                guarded = HomeAssistantRecoveryBlobStore(hass, "entry-3")
                await guarded.async_write("recovery-safe", b"preserve-on-failure")
                guarded_root = Path(root, ".storage", "nitrado_gameserver", "recovery", "entry-3")
                (guarded_root / "unexpected.txt").write_bytes(b"foreign")
                with self.assertRaises(OSError):
                    await guarded.async_remove_all()
                self.assertEqual(await guarded.async_read("recovery-safe"), b"preserve-on-failure")
                self.assertEqual((guarded_root / "unexpected.txt").read_bytes(), b"foreign")

                outside = Path(root, "outside-private")
                outside.write_bytes(b"outside")
                (guarded_root / "recovery-link").symlink_to(outside)
                with self.assertRaises(OSError):
                    await guarded.async_remove_all()
                self.assertEqual(outside.read_bytes(), b"outside")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
