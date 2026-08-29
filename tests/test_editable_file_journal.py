"""Tests for durable editable-file recovery history."""

from __future__ import annotations

import asyncio
import unittest

from custom_components.nitrado_gameserver.editable_file_journal import (
    EditableFileJournal,
    EditableFileJournalError,
    MemoryEditableFileJournalStore,
)


class EditableFileJournalTests(unittest.TestCase):
    def test_records_are_scoped_and_prune_only_old_metadata_beyond_ten(self) -> None:
        async def run() -> None:
            now = 200 * 24 * 60 * 60
            store = MemoryEditableFileJournalStore()
            journal = EditableFileJournal(store, now_fn=lambda: now)
            for index in range(12):
                await journal.async_record(
                    account_entry_id="entry-1",
                    service_id="123",
                    file_key="settings",
                    declared_path="/game/PalWorldSettings.ini",
                    backup_path=f"/game/PalWorldSettings.ini.nitrado_gameserver.backup.{index}",
                    backup_kind="apply",
                    source_revision=f"{index:064x}",
                    resulting_revision=f"{index + 1:064x}",
                    operation_id=f"operation-{index}",
                    created_at=index,
                )

            records = await journal.async_records("123", "settings")
            self.assertEqual(len(records), 10)
            self.assertEqual(records[0].operation_id, "operation-11")
            self.assertNotIn("backup.0", str(store.payload))
            self.assertEqual(await journal.async_records("other", "settings"), ())

        asyncio.run(run())

    def test_corrupt_storage_fails_closed(self) -> None:
        async def run() -> None:
            journal = EditableFileJournal(MemoryEditableFileJournalStore({"schema_version": 99, "records": []}))
            with self.assertRaises(EditableFileJournalError):
                await journal.async_initialize()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
