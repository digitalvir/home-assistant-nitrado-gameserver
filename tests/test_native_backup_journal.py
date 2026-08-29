"""Adversarial tests for durable provider-native restore tracking."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.nitrado_gameserver.native_backup_journal import (
    NativeBackupJournalError,
    NativeBackupJournalIntegrityError,
    NativeBackupRestoreJournal,
    NativeRestoreFailureCode,
    NativeRestoreIssue,
    NativeRestoreState,
    NativeRestoreTarget,
)


class MemoryStore:
    def __init__(self, payload: Any = None) -> None:
        self.payload = payload
        self.quarantined: list[Any] = []
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()
        self.block = False

    async def async_load(self) -> Any:
        return self.payload

    async def async_save(self, payload: Any) -> None:
        self.save_started.set()
        if self.block:
            await self.release_save.wait()
        self.payload = dict(payload)

    async def async_quarantine(self, payload: Any) -> None:
        self.quarantined.append(payload)


def target() -> NativeRestoreTarget:
    return NativeRestoreTarget(
        account_entry_id="entry_1",
        service_id="900001",
        folder="palworldxb",
        backup_id="35",
        expected_size_bytes=429280246,
        expected_created_at="1542522769",
        expected_status=None,
        expected_backup_type="incremental",
        expected_file_size_bytes=45,
    )


class NativeBackupJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_identity_and_preconditions_are_durable_before_request(self) -> None:
        store = MemoryStore()
        journal = NativeBackupRestoreJournal(store)

        record = await journal.async_prepare(target())

        durable = store.payload["records"][0]  # type: ignore[index]
        self.assertEqual(durable["account_entry_id"], "entry_1")
        self.assertEqual(durable["service_id"], "900001")
        self.assertEqual(durable["folder"], "palworldxb")
        self.assertEqual(durable["backup_id"], "35")
        self.assertEqual(durable["expected_backup_type"], "incremental")
        self.assertEqual(durable["expected_size_bytes"], 429280246)
        self.assertEqual(durable["expected_file_size_bytes"], 45)
        self.assertEqual(durable["state"], NativeRestoreState.PREPARED)
        self.assertEqual(record.target, target())

    async def test_unresolved_restore_blocks_second_restore_for_exact_service(self) -> None:
        journal = NativeBackupRestoreJournal(MemoryStore())
        await journal.async_prepare(target())

        with self.assertRaisesRegex(NativeBackupJournalError, "already exists"):
            await journal.async_prepare(
                NativeRestoreTarget(
                    "entry_1",
                    "900001",
                    "palworldxb",
                    "36",
                    10,
                    "200",
                    None,
                )
            )

    async def test_restart_aborts_prepared_but_marks_requesting_unknown(self) -> None:
        safe_store = MemoryStore()
        safe = NativeBackupRestoreJournal(safe_store)
        await safe.async_prepare(target())
        restarted_safe = NativeBackupRestoreJournal(safe_store)
        await restarted_safe.async_initialize()
        self.assertEqual(await restarted_safe.async_unresolved_facts(), ())

        risky_store = MemoryStore()
        risky = NativeBackupRestoreJournal(risky_store)
        record = await risky.async_prepare(target())
        await risky.async_mark_requesting(record.operation_id)
        restarted_risky = NativeBackupRestoreJournal(risky_store)
        await restarted_risky.async_initialize()
        facts = await restarted_risky.async_unresolved_facts()
        self.assertEqual(facts[0].issue, NativeRestoreIssue.RESTORE_REVIEW_REQUIRED)
        self.assertEqual(facts[0].state, NativeRestoreState.OUTCOME_UNKNOWN)
        self.assertEqual(facts[0].failure_code, NativeRestoreFailureCode.PROCESS_RESTARTED)

    async def test_acceptance_is_unresolved_until_full_transition_observed(self) -> None:
        journal = NativeBackupRestoreJournal(MemoryStore())
        record = await journal.async_prepare(target())
        await journal.async_mark_requesting(record.operation_id)
        await journal.async_mark_accepted(record.operation_id)
        facts = await journal.async_unresolved_facts()
        self.assertEqual(facts[0].state, NativeRestoreState.ACCEPTED)

        await journal.async_mark_observing(record.operation_id)
        await journal.async_mark_observed(record.operation_id, provider_status="started")
        self.assertEqual(await journal.async_unresolved_facts(), ())
        diagnostics = await journal.async_diagnostics()
        self.assertFalse(diagnostics["records"][0]["game_health_verified"])

    async def test_cancelled_durable_commit_finishes_before_cancellation_escapes(self) -> None:
        store = MemoryStore()
        journal = NativeBackupRestoreJournal(store)
        await journal.async_initialize()
        store.block = True
        task = asyncio.create_task(journal.async_prepare(target()))
        await store.save_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        store.release_save.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(  # type: ignore[index]
            store.payload["records"][0]["state"],
            NativeRestoreState.ABORTED_BEFORE_REQUEST,
        )
        self.assertEqual(  # type: ignore[index]
            store.payload["records"][0]["failure_code"],
            NativeRestoreFailureCode.CANCELLED,
        )

    async def test_corrupt_metadata_fails_closed_and_emits_fact(self) -> None:
        store = MemoryStore({"schema_version": 999, "records": []})
        journal = NativeBackupRestoreJournal(store)
        await journal.async_initialize()

        facts = await journal.async_unresolved_facts()
        self.assertEqual(facts[0].issue, NativeRestoreIssue.CORRUPT_METADATA)
        with self.assertRaises(NativeBackupJournalIntegrityError):
            await journal.async_prepare(target())

    async def test_unknown_outcome_can_only_be_closed_as_acknowledged_unverified(self) -> None:
        journal = NativeBackupRestoreJournal(MemoryStore())
        record = await journal.async_prepare(target())
        await journal.async_mark_requesting(record.operation_id)
        await journal.async_mark_outcome_unknown(
            record.operation_id,
            NativeRestoreFailureCode.TRANSPORT_UNCONFIRMED,
            provider_status="backup_restore",
        )

        closed = await journal.async_acknowledge_unverified(record.operation_id)

        self.assertEqual(closed.state, NativeRestoreState.ACKNOWLEDGED_UNVERIFIED)
        self.assertEqual(closed.failure_code, NativeRestoreFailureCode.TRANSPORT_UNCONFIRMED)
        self.assertEqual(closed.provider_status, "backup_restore")
        self.assertEqual(await journal.async_unresolved_facts(), ())
        diagnostics = await journal.async_diagnostics()
        self.assertFalse(diagnostics["records"][0]["game_health_verified"])

    async def test_corrupt_payload_is_quarantined_before_explicit_reset(self) -> None:
        corrupt = ["not", "a", "journal"]
        store = MemoryStore(corrupt)
        journal = NativeBackupRestoreJournal(store)
        await journal.async_initialize()

        await journal.async_quarantine_and_reset_corrupt()

        self.assertEqual(store.quarantined, [corrupt])
        self.assertEqual(store.payload, {"schema_version": 1, "records": []})
        self.assertEqual(await journal.async_unresolved_facts(), ())
        await journal.async_prepare(target())

    async def test_semantically_impossible_terminal_record_fails_closed(self) -> None:
        store = MemoryStore()
        journal = NativeBackupRestoreJournal(store)
        record = await journal.async_prepare(target())
        await journal.async_mark_requesting(record.operation_id)
        await journal.async_mark_accepted(record.operation_id)
        await journal.async_mark_observing(record.operation_id)
        await journal.async_mark_observed(record.operation_id, provider_status="started")
        assert isinstance(store.payload, dict)
        store.payload["records"][0]["provider_status"] = None
        store.payload["records"][0]["failure_code"] = NativeRestoreFailureCode.TRANSPORT_UNCONFIRMED

        restarted = NativeBackupRestoreJournal(store)
        await restarted.async_initialize()

        facts = await restarted.async_unresolved_facts()
        self.assertEqual(facts[0].issue, NativeRestoreIssue.CORRUPT_METADATA)

    async def test_durable_identity_rejects_traversal_and_non_numeric_backup_number(self) -> None:
        with self.assertRaises(ValueError):
            NativeRestoreTarget("entry_1", "900001", "../palworldxb", "35", 1, "100", None)
        with self.assertRaises(ValueError):
            NativeRestoreTarget("entry_1", "900001", "palworldxb", "latest", 1, "100", None)

    async def test_only_old_terminal_records_are_pruned(self) -> None:
        now = datetime(2026, 8, 16, tzinfo=UTC)
        store = MemoryStore()
        journal = NativeBackupRestoreJournal(
            store,
            completed_retention=timedelta(days=1),
            clock=lambda: now,
        )
        record = await journal.async_prepare(target())
        await journal.async_mark_requesting(record.operation_id)
        await journal.async_mark_accepted(record.operation_id)
        await journal.async_mark_observing(record.operation_id)
        await journal.async_mark_observed(record.operation_id, provider_status="started")

        later = NativeBackupRestoreJournal(
            store,
            completed_retention=timedelta(days=1),
            clock=lambda: now + timedelta(days=2),
        )
        await later.async_initialize()
        diagnostics = await later.async_diagnostics()
        self.assertEqual(diagnostics["record_count"], 0)


if __name__ == "__main__":
    unittest.main()
