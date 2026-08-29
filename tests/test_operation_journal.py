"""Focused tests for durable, composite-identity operation arbitration."""

from __future__ import annotations

import asyncio
import unittest

from custom_components.nitrado_gameserver.coordinator import NitradoAccountCoordinator, NitradoControlError
from custom_components.nitrado_gameserver.operation_journal import (
    MemoryOperationJournalStore,
    OperationIdentity,
    OperationIntent,
    OperationJournal,
    OperationJournalError,
    OperationPhase,
)


class OperationJournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_converts_dispatched_to_unknown_and_blocks_conflict(self) -> None:
        store = MemoryOperationJournalStore()
        identity = OperationIdentity("account-a", "100")
        journal = OperationJournal(store, now_fn=lambda: 10)
        await journal.async_initialize()
        reservation = await journal.async_reserve(
            identity,
            OperationIntent("start", "server-control"),
        )
        await reservation.async_mark_dispatched()

        restarted = OperationJournal(store, now_fn=lambda: 20)
        await restarted.async_initialize()
        records = await restarted.async_records(identity)
        self.assertEqual(records[0].phase, OperationPhase.UNKNOWN)
        self.assertEqual(records[0].result_code, "interrupted")
        with self.assertRaisesRegex(OperationJournalError, "unknown"):
            await restarted.async_reserve(identity, OperationIntent("stop", "server-control"))

    async def test_same_service_id_in_different_accounts_never_conflicts(self) -> None:
        journal = OperationJournal(MemoryOperationJournalStore(), now_fn=lambda: 10)
        await journal.async_initialize()
        first = await journal.async_reserve(
            OperationIdentity("account-a", "100"),
            OperationIntent("start", "server-control"),
        )
        second = await journal.async_reserve(
            OperationIdentity("account-b", "100"),
            OperationIntent("stop", "server-control"),
        )
        self.assertNotEqual(first.operation_id, second.operation_id)

    async def test_nested_token_reuses_outer_reservation_without_deadlock(self) -> None:
        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]
        intent = OperationIntent("profile-action", "safe-action")
        async with (
            coordinator.async_operation("100", intent) as outer,
            coordinator.async_operation("100", intent, operation_token=outer) as inner,
        ):
            self.assertIs(inner, outer)
            await inner.async_mark_dispatched()
            await inner.async_mark_verifying()
        records = await coordinator.operation_journal.async_records(OperationIdentity("standalone", "100"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].phase, OperationPhase.TERMINAL)
        self.assertEqual(records[0].result_code, "succeeded")

    async def test_cancellation_after_dispatch_is_retained_as_unknown(self) -> None:
        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]

        async def operation() -> None:
            async with coordinator.async_operation("100", OperationIntent("editable-apply", "settings")) as reservation:
                await reservation.async_mark_dispatched()
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await operation()
        records = await coordinator.operation_journal.async_records(OperationIdentity("standalone", "100"))
        self.assertEqual(records[0].phase, OperationPhase.UNKNOWN)
        self.assertEqual(records[0].result_code, "cancelled_after_dispatch")

    async def test_start_remains_verifying_until_operation_specific_reconciliation(self) -> None:
        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]
        identity = OperationIdentity("standalone", "100")
        async with coordinator.async_operation("100", OperationIntent("start", "server-control")) as reservation:
            await reservation.async_mark_dispatched()
            await reservation.async_mark_verifying()

        records = await coordinator.operation_journal.async_records(identity)
        self.assertEqual(records[0].phase, OperationPhase.VERIFYING)
        await coordinator.operation_journal.async_reconcile(
            identity,
            records[0].operation_id,
            "provider_status_observed",
        )
        records = await coordinator.operation_journal.async_records(identity)
        self.assertEqual(records[0].phase, OperationPhase.TERMINAL)

    async def test_shutdown_drains_every_general_operation_and_rejects_waiter(self) -> None:
        coordinator = NitradoAccountCoordinator(client=object())  # type: ignore[arg-type]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first() -> None:
            async with coordinator.async_operation("100", OperationIntent("profile-option", "first")) as reservation:
                await reservation.async_mark_dispatched()
                entered.set()
                await release.wait()
                await reservation.async_mark_verifying()

        async def waiter() -> None:
            async with coordinator.async_operation("100", OperationIntent("profile-option", "second")) as reservation:
                await reservation.async_mark_dispatched()
                await reservation.async_mark_verifying()

        first_task = asyncio.create_task(first())
        await entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(coordinator.async_shutdown())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await first_task
        with self.assertRaisesRegex(NitradoControlError, "unloading"):
            await waiter_task
        await shutdown
        self.assertEqual(coordinator._active_operation_tasks, {})

    async def test_loaded_account_fails_closed_until_durable_journal_is_bound(self) -> None:
        coordinator = NitradoAccountCoordinator(  # type: ignore[arg-type]
            client=object(),
            account_entry_id="account-a",
        )
        with self.assertRaisesRegex(NitradoControlError, "not initialized"):
            async with coordinator.async_operation("100", OperationIntent("start", "server-control")):
                self.fail("unbound account operation must not run")

        journal = OperationJournal(MemoryOperationJournalStore())
        await coordinator.async_bind_operation_journal(journal)
        async with coordinator.async_operation(
            "100", OperationIntent("profile-action", "server-control")
        ) as reservation:
            await reservation.async_mark_dispatched()
            await reservation.async_mark_verifying()
        records = await journal.async_records(OperationIdentity("account-a", "100"))
        self.assertEqual(records[0].phase, OperationPhase.TERMINAL)

    async def test_store_write_failure_poison_pills_future_mutations(self) -> None:
        class FailingStore(MemoryOperationJournalStore):
            async def async_save(self, payload):
                del payload
                raise OSError("disk full")

        journal = OperationJournal(FailingStore())
        await journal.async_initialize()
        identity = OperationIdentity("account-a", "100")
        with self.assertRaises(OSError):
            await journal.async_reserve(identity, OperationIntent("start", "server-control"))
        with self.assertRaisesRegex(OperationJournalError, "all mutations are blocked"):
            await journal.async_reserve(identity, OperationIntent("start", "server-control"))

    def test_intent_rejects_unbounded_or_secret_shaped_targets(self) -> None:
        with self.assertRaises(ValueError):
            OperationIntent("profile-action", "password=do not persist this")


if __name__ == "__main__":
    unittest.main()
