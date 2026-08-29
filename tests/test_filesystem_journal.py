"""Tests for the durable filesystem transaction journal."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import IsolatedAsyncioTestCase

from custom_components.nitrado_gameserver.filesystem_journal import (
    CompletionDisposition,
    FilesystemTransactionJournal,
    FileTransactionOperation,
    JournalFailureCode,
    JournalIntegrityError,
    JournalIssue,
    JournalLimitError,
    JournalTransitionError,
    RecoveryBlobError,
    ServiceReference,
    TransactionState,
    VerificationEvidence,
    VerificationExpectation,
    VerificationKind,
    VerificationPurpose,
)

ZERO_DIGEST = "0" * 64
ONE_DIGEST = "1" * 64


class MemoryMetadataStore:
    """Atomic in-memory metadata persistence for tests."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = copy.deepcopy(payload)
        self.fail_save = False
        self.save_count = 0

    async def async_load(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.payload)

    async def async_save(self, payload: dict[str, Any]) -> None:
        if self.fail_save:
            raise OSError("disk full")
        self.save_count += 1
        self.payload = copy.deepcopy(payload)


class BlockingMetadataStore(MemoryMetadataStore):
    """Metadata store that exposes cancellation at the durable commit boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_save(self, payload: dict[str, Any]) -> None:
        self.started.set()
        await self.release.wait()
        await super().async_save(payload)


class MemoryBlobStore:
    """Private in-memory recovery blob storage for tests."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def async_write(self, blob_id: str, content: bytes) -> None:
        if blob_id in self.blobs:
            raise FileExistsError(blob_id)
        self.blobs[blob_id] = bytes(content)

    async def async_read(self, blob_id: str) -> bytes:
        return self.blobs[blob_id]

    async def async_delete(self, blob_id: str) -> None:
        self.deleted.append(blob_id)
        self.blobs.pop(blob_id, None)

    async def async_list(self) -> tuple[str, ...]:
        return tuple(sorted(self.blobs))


class BlockingBlobStore(MemoryBlobStore):
    """Blob store that exposes cancellation during atomic creation."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_write(self, blob_id: str, content: bytes) -> None:
        self.started.set()
        await self.release.wait()
        await super().async_write(blob_id, content)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FilesystemTransactionJournalTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.metadata = MemoryMetadataStore()
        self.blobs = MemoryBlobStore()
        self.clock = MutableClock()
        self.journal = FilesystemTransactionJournal(
            self.metadata,
            self.blobs,
            clock=self.clock,
        )
        self.service = ServiceReference("entry-1", "900001")

    async def _prepared(self, *, recovery: bytes | None = b"previous"):
        return await self.journal.async_prepare(
            self.service,
            FileTransactionOperation.WRITE_FILE,
            "gameserver/Pal/Saved/Config/settings.ini",
            recovery_content=recovery,
        )

    async def test_recovery_bytes_are_separate_and_metadata_is_secret_safe(self) -> None:
        record = await self._prepared(recovery=b"AdminPassword=super-secret")

        self.assertEqual(record.state, TransactionState.PREPARED)
        self.assertEqual(len(self.blobs.blobs), 1)
        serialized = repr(self.metadata.payload)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("settings.ini", repr(record))
        self.assertNotIn(record.recovery.sha256, repr(record))
        self.assertEqual(await self.journal.async_read_recovery(record.transaction_id), b"AdminPassword=super-secret")

    async def test_exact_verified_commit_lifecycle(self) -> None:
        record = await self._prepared()
        record = await self.journal.async_mark_mutating(record.transaction_id)
        self.assertEqual(record.state, TransactionState.MUTATING)
        record = await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.COMMIT, ZERO_DIGEST),
        )
        self.assertEqual(record.state, TransactionState.VERIFYING)
        record = await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, ZERO_DIGEST, ZERO_DIGEST),
            CompletionDisposition.COMMITTED,
        )
        self.assertEqual(record.state, TransactionState.COMPLETED)
        self.assertEqual(record.disposition, CompletionDisposition.COMMITTED)
        self.assertEqual(await self.journal.async_recovery_candidates(), ())

    async def test_provider_ack_cannot_complete_transaction(self) -> None:
        record = await self._prepared()
        record = await self.journal.async_mark_mutating(record.transaction_id)

        with self.assertRaises(JournalTransitionError):
            await self.journal.async_complete(
                record.transaction_id,
                VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, ZERO_DIGEST, ZERO_DIGEST),
                CompletionDisposition.COMMITTED,
            )

    async def test_mismatched_readback_cannot_complete_transaction(self) -> None:
        record = await self._prepared()
        await self.journal.async_mark_mutating(record.transaction_id)
        await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.COMMIT, ZERO_DIGEST),
        )

        with self.assertRaises(JournalTransitionError):
            await self.journal.async_complete(
                record.transaction_id,
                VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, ZERO_DIGEST, ONE_DIGEST),
                CompletionDisposition.COMMITTED,
            )

    async def test_rollback_requires_rollback_identity_and_exact_verification(self) -> None:
        record = await self._prepared()
        await self.journal.async_mark_mutating(record.transaction_id)
        record = await self.journal.async_mark_rollback_required(
            record.transaction_id,
            JournalFailureCode.TRANSFER_INCOMPLETE,
        )
        self.assertEqual(record.state, TransactionState.ROLLBACK_REQUIRED)
        record = await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.ROLLBACK, ONE_DIGEST),
        )
        with self.assertRaises(JournalTransitionError):
            await self.journal.async_complete(
                record.transaction_id,
                VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, ONE_DIGEST, ONE_DIGEST),
                CompletionDisposition.COMMITTED,
            )
        record = await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, ONE_DIGEST, ONE_DIGEST),
            CompletionDisposition.ROLLED_BACK,
        )
        self.assertEqual(record.disposition, CompletionDisposition.ROLLED_BACK)

    async def test_restart_enumerates_unresolved_transactions(self) -> None:
        record = await self._prepared()
        await self.journal.async_mark_mutating(record.transaction_id)

        restarted = FilesystemTransactionJournal(self.metadata, self.blobs, clock=self.clock)
        candidates = await restarted.async_recovery_candidates()

        self.assertEqual([candidate.transaction_id for candidate in candidates], [record.transaction_id])
        self.assertEqual(candidates[0].state, TransactionState.MUTATING)
        self.assertEqual(await restarted.async_read_recovery(record.transaction_id), b"previous")

    async def test_repairs_facts_distinguish_missing_and_corrupt_recovery(self) -> None:
        missing = await self._prepared(recovery=b"missing")
        corrupt = await self._prepared(recovery=b"corrupt")
        self.blobs.blobs.pop(missing.recovery.blob_id)
        self.blobs.blobs[corrupt.recovery.blob_id] = b"tampered"

        facts = {fact.transaction_id: fact for fact in await self.journal.async_unresolved_facts()}

        self.assertEqual(facts[missing.transaction_id].issue, JournalIssue.RECOVERY_BLOB_MISSING)
        self.assertEqual(facts[corrupt.transaction_id].issue, JournalIssue.RECOVERY_BLOB_CORRUPT)
        self.assertFalse(facts[missing.transaction_id].recovery_available)
        self.assertFalse(facts[corrupt.transaction_id].recovery_available)

    async def test_corrupt_metadata_blocks_new_mutations_and_surfaces_fact(self) -> None:
        metadata = MemoryMetadataStore({"schema_version": 999, "records": []})
        journal = FilesystemTransactionJournal(metadata, self.blobs, clock=self.clock)

        facts = await journal.async_unresolved_facts()
        diagnostics = await journal.async_diagnostics()

        self.assertEqual(facts[0].issue, JournalIssue.CORRUPT_METADATA)
        self.assertTrue(diagnostics["integrity_blocked"])
        with self.assertRaises(JournalIntegrityError):
            await journal.async_prepare(
                self.service,
                FileTransactionOperation.WRITE_FILE,
                "safe/file.txt",
            )

    async def test_diagnostics_never_expose_path_or_content_hash(self) -> None:
        record = await self._prepared(recovery=b"private bytes")
        diagnostics = await self.journal.async_diagnostics()
        rendered = repr(diagnostics)

        self.assertNotIn("settings.ini", rendered)
        self.assertNotIn("private bytes", rendered)
        self.assertNotIn(record.recovery.sha256, rendered)
        self.assertEqual(diagnostics["records"][0]["target_path_depth"], 5)
        self.assertEqual(len(diagnostics["records"][0]["target_path_sha256_prefix"]), 16)

    async def test_failed_metadata_commit_removes_new_recovery_blob(self) -> None:
        self.metadata.fail_save = True

        with self.assertRaises(OSError):
            await self._prepared(recovery=b"do not orphan")

        self.assertEqual(self.blobs.blobs, {})
        self.assertEqual(len(self.blobs.deleted), 1)

    async def test_bounds_single_and_total_recovery_content(self) -> None:
        journal = FilesystemTransactionJournal(
            self.metadata,
            self.blobs,
            max_blob_bytes=4,
            max_total_blob_bytes=6,
            clock=self.clock,
        )
        await journal.async_prepare(
            self.service,
            FileTransactionOperation.WRITE_FILE,
            "first/file.txt",
            recovery_content=b"1234",
        )
        with self.assertRaises(JournalLimitError):
            await journal.async_prepare(
                self.service,
                FileTransactionOperation.WRITE_FILE,
                "second/file.txt",
                recovery_content=b"123",
            )
        with self.assertRaises(JournalLimitError):
            await journal.async_prepare(
                self.service,
                FileTransactionOperation.WRITE_FILE,
                "third/file.txt",
                recovery_content=b"12345",
            )

    async def test_completed_retention_prunes_metadata_then_blob(self) -> None:
        journal = FilesystemTransactionJournal(
            self.metadata,
            self.blobs,
            completed_retention=timedelta(hours=1),
            clock=self.clock,
        )
        record = await journal.async_prepare(
            self.service,
            FileTransactionOperation.WRITE_FILE,
            "safe/file.txt",
            recovery_content=b"old",
        )
        await journal.async_mark_mutating(record.transaction_id)
        await journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.COMMIT, ZERO_DIGEST),
        )
        await journal.async_complete(
            record.transaction_id,
            VerificationEvidence(VerificationKind.EXACT_READBACK_SHA256, ZERO_DIGEST, ZERO_DIGEST),
            CompletionDisposition.COMMITTED,
        )
        self.clock.now += timedelta(hours=2)

        await journal.async_prepare(
            self.service,
            FileTransactionOperation.WRITE_FILE,
            "new/file.txt",
        )

        ids = {item["transaction_id"] for item in self.metadata.payload["records"]}
        self.assertNotIn(record.transaction_id, ids)
        self.assertEqual(self.blobs.blobs, {})

    async def test_concurrent_prepares_are_serialized_and_bounded(self) -> None:
        journal = FilesystemTransactionJournal(
            self.metadata,
            self.blobs,
            max_records=2,
            clock=self.clock,
        )

        async def prepare(index: int):
            return await journal.async_prepare(
                self.service,
                FileTransactionOperation.WRITE_FILE,
                f"safe/file-{index}.txt",
            )

        results = await asyncio.gather(*(prepare(index) for index in range(3)), return_exceptions=True)

        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 2)
        self.assertEqual(sum(isinstance(result, JournalLimitError) for result in results), 1)

    async def test_cancellation_during_metadata_save_has_unambiguous_durable_result(self) -> None:
        metadata = BlockingMetadataStore()
        journal = FilesystemTransactionJournal(metadata, self.blobs, clock=self.clock)
        task = asyncio.create_task(
            journal.async_prepare(
                self.service,
                FileTransactionOperation.WRITE_FILE,
                "safe/file.txt",
                recovery_content=b"recover me",
            )
        )
        await metadata.started.wait()
        task.cancel()
        metadata.release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task

        restarted = FilesystemTransactionJournal(metadata, self.blobs, clock=self.clock)
        candidates = await restarted.async_recovery_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].state, TransactionState.PREPARED)
        self.assertEqual(await restarted.async_read_recovery(candidates[0].transaction_id), b"recover me")

    async def test_cancellation_during_blob_write_removes_unreferenced_blob(self) -> None:
        blobs = BlockingBlobStore()
        journal = FilesystemTransactionJournal(self.metadata, blobs, clock=self.clock)
        task = asyncio.create_task(
            journal.async_prepare(
                self.service,
                FileTransactionOperation.WRITE_FILE,
                "safe/file.txt",
                recovery_content=b"recover me",
            )
        )
        await blobs.started.wait()
        task.cancel()
        blobs.release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(blobs.blobs, {})
        self.assertEqual(await journal.async_recovery_candidates(), ())

    async def test_noncanonical_or_control_character_paths_are_rejected(self) -> None:
        bad_paths = (
            "/absolute/file.txt",
            "parent/../file.txt",
            "double//file.txt",
            "windows\\file.txt",
            "trailing/",
            "control/line\nfeed",
        )
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                await self.journal.async_prepare(
                    self.service,
                    FileTransactionOperation.WRITE_FILE,
                    path,
                )

    async def test_corrupt_or_missing_recovery_raises_without_returning_bytes(self) -> None:
        record = await self._prepared(recovery=b"known")
        self.blobs.blobs[record.recovery.blob_id] = b"wrong"
        with self.assertRaises(RecoveryBlobError):
            await self.journal.async_read_recovery(record.transaction_id)

    async def test_rollback_required_cannot_be_completed_as_commit(self) -> None:
        record = await self._prepared(recovery=b"known")
        await self.journal.async_mark_mutating(record.transaction_id)
        await self.journal.async_mark_rollback_required(
            record.transaction_id,
            JournalFailureCode.VERIFICATION_FAILED,
        )

        with self.assertRaises(JournalTransitionError):
            await self.journal.async_mark_verifying(
                record.transaction_id,
                VerificationExpectation(VerificationPurpose.COMMIT, ZERO_DIGEST),
            )

        rollback_digest = hashlib.sha256(b"known").hexdigest()
        await self.journal.async_mark_verifying(
            record.transaction_id,
            VerificationExpectation(VerificationPurpose.ROLLBACK, rollback_digest),
        )
        completed = await self.journal.async_complete(
            record.transaction_id,
            VerificationEvidence(
                VerificationKind.EXACT_READBACK_SHA256,
                rollback_digest,
                rollback_digest,
            ),
            CompletionDisposition.ROLLED_BACK,
        )
        self.assertEqual(completed.disposition, CompletionDisposition.ROLLED_BACK)

    async def test_restart_reconciles_only_unreferenced_owned_blobs(self) -> None:
        record = await self._prepared(recovery=b"known")
        self.blobs.blobs["recovery-orphan"] = b"orphan"

        restarted = FilesystemTransactionJournal(self.metadata, self.blobs, clock=self.clock)
        await restarted.async_initialize()

        self.assertNotIn("recovery-orphan", self.blobs.blobs)
        self.assertIn(record.recovery.blob_id, self.blobs.blobs)
