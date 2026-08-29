"""Tests for typed provider-native Nitrado backup handling."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from custom_components.nitrado_gameserver.api.nitrado import NitradoApiError, NitradoClient
from custom_components.nitrado_gameserver.backups import (
    NativeBackupError,
    NativeBackupIdentity,
    NativeBackupRestoreObservation,
    NativeBackupRestoreOutcome,
    NativeBackupRestoreRequest,
    NativeBackupRestoreUnconfirmedError,
    NitradoNativeBackupService,
    parse_native_backup_inventory,
)
from custom_components.nitrado_gameserver.native_backup_journal import (
    NativeBackupRestoreJournal,
    NativeRestoreFailureCode,
    NativeRestoreState,
)


class MemoryJournalStore:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def async_load(self) -> dict[str, Any] | None:
        return self.payload

    async def async_save(self, payload: Any) -> None:
        self.payload = dict(payload)


class FakeBackupClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.restores: list[tuple[str, str, str]] = []
        self.restore_possible = True

    async def list_native_backups(self, service_id: str) -> dict[str, Any]:
        return self.payload

    async def native_backup_restore_possible(self, service_id: str) -> bool:
        return self.restore_possible

    async def restore_native_backup(
        self,
        service_id: str,
        game: str,
        backup_id: str,
    ) -> dict[str, Any]:
        self.restores.append((service_id, game, backup_id))
        return {"message": "scheduled"}


class FakeObserver:
    def __init__(self, observation: NativeBackupRestoreObservation) -> None:
        self.observation = observation

    async def async_observe_native_restore(
        self,
        service_id: str,
        identity: NativeBackupIdentity,
    ) -> NativeBackupRestoreObservation:
        return self.observation


def tracked_service(
    client: FakeBackupClient,
    *,
    observer: FakeObserver | None = None,
    store: MemoryJournalStore | None = None,
) -> tuple[NitradoNativeBackupService, NativeBackupRestoreJournal, MemoryJournalStore]:
    store = store or MemoryJournalStore()
    journal = NativeBackupRestoreJournal(store)
    return (
        NitradoNativeBackupService(
            client,
            "900001",
            account_entry_id="account_entry_1",
            journal=journal,
            observer=observer,
        ),
        journal,
        store,
    )


class RecordingNitradoClient(NitradoClient):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(None, "secret")  # type: ignore[arg-type]
        self.response = response
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.requests.append((method, path, kwargs))
        return self.response


class NativeBackupTests(unittest.TestCase):
    def test_parser_accepts_grouped_and_flat_inventory_without_health_claims(self) -> None:
        grouped = parse_native_backup_inventory(
            {
                "backups": {
                    "gameserver": {
                        "folder": "palworldxb",
                        "backup": {
                            "master": [
                                {"id": 35, "timestamp": "2026-08-14T13:56:00Z", "size": 99_000_000},
                                {"backup": "36", "created_at": "2026-08-15T13:35:00Z", "size": "43000"},
                            ]
                        },
                    }
                }
            }
        )
        flat = parse_native_backup_inventory(
            {"backups": [{"backup_id": "7", "folder": "minecraft", "status": "ready"}]}
        )

        self.assertEqual(grouped[0].identity, NativeBackupIdentity("palworldxb", "35"))
        self.assertEqual(grouped[0].size_bytes, 99_000_000)
        self.assertEqual(grouped[1].identity.backup_id, "36")
        self.assertEqual(flat[0].identity, NativeBackupIdentity("minecraft", "7"))
        self.assertEqual(flat[0].status, "ready")

    def test_parser_accepts_exact_documented_gameserver_shape(self) -> None:
        parsed = parse_native_backup_inventory(
            {
                "backups": {
                    "gameserver": {
                        "battalionwindows": [
                            {
                                "backup_type": "master",
                                "backup_timestamp": 1542438292,
                                "backup_number": 858,
                                "backup_size": 429280156,
                                "backup_file_size": 429280156,
                            }
                        ]
                    }
                }
            }
        )

        self.assertEqual(parsed[0].identity, NativeBackupIdentity("battalionwindows", "858"))
        self.assertEqual(parsed[0].created_at, "1542438292")
        self.assertEqual(parsed[0].size_bytes, 429280156)
        self.assertEqual(parsed[0].file_size_bytes, 429280156)
        self.assertEqual(parsed[0].backup_type, "master")
        self.assertIsNone(parsed[0].status)

    def test_parser_rejects_missing_malformed_and_conflicting_inventory(self) -> None:
        for payload in ({}, {"backups": None}, {"backups": "garbage"}):
            with self.subTest(payload=payload), self.assertRaises(NativeBackupError):
                parse_native_backup_inventory(payload)

        duplicate = {
            "backups": {
                "gameserver": {
                    "palworldxb": [
                        {
                            "backup_number": 35,
                            "backup_timestamp": 100,
                            "backup_size": 99,
                            "backup_type": "master",
                        },
                        {
                            "backup_number": 35,
                            "backup_timestamp": 101,
                            "backup_size": 43,
                            "backup_type": "incremental",
                        },
                    ]
                }
            }
        }
        with self.assertRaisesRegex(NativeBackupError, "conflicting duplicate"):
            parse_native_backup_inventory(duplicate)
        with self.assertRaisesRegex(NativeBackupError, "size field is malformed"):
            parse_native_backup_inventory(
                {
                    "backups": [
                        {
                            "backup_number": 35,
                            "folder": "palworldxb",
                            "backup_timestamp": 100,
                            "backup_size": "definitely-not-a-size",
                        }
                    ]
                }
            )

    def test_restore_rechecks_exact_inventory_identity_and_preconditions(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "id": "35",
                            "folder": "palworldxb",
                            "size": 99_000_000,
                            "timestamp": "2026-08-14T13:56:00Z",
                            "status": "ready",
                        }
                    ]
                }
            )
            service, journal, _ = tracked_service(
                client,
                observer=FakeObserver(NativeBackupRestoreObservation(True, True, "started")),
            )
            self.assertFalse(service.capabilities.create)
            result = await service.async_restore(
                NativeBackupRestoreRequest(
                    NativeBackupIdentity("palworldxb", "35"),
                    expected_size_bytes=99_000_000,
                    expected_created_at="2026-08-14T13:56:00Z",
                    expected_status="ready",
                )
            )

            self.assertEqual(client.restores, [("900001", "palworldxb", "35")])
            self.assertTrue(result.provider_accepted)
            self.assertTrue(result.provider_returned_data)
            self.assertTrue(result.provider_restore_observed)
            self.assertEqual(result.outcome, NativeBackupRestoreOutcome.PROVIDER_TRANSITION_OBSERVED)
            self.assertFalse(result.game_health_verified)
            self.assertEqual(await journal.async_unresolved_facts(), ())

        asyncio.run(run())

    def test_unknown_provider_status_is_bound_but_not_invented_as_policy(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "backup_number": 35,
                            "folder": "palworldxb",
                            "backup_size": 99,
                            "backup_timestamp": 100,
                            "status": "valid",
                        }
                    ]
                }
            )
            service, _, _ = tracked_service(
                client,
                observer=FakeObserver(NativeBackupRestoreObservation(True, True, "stopped")),
            )
            result = await service.async_restore(
                NativeBackupRestoreRequest(
                    NativeBackupIdentity("palworldxb", "35"),
                    expected_size_bytes=99,
                    expected_created_at="100",
                    expected_status="valid",
                )
            )
            self.assertTrue(result.provider_restore_observed)

        asyncio.run(run())

    def test_pre_transport_check_aborts_without_unknown_provider_outcome(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "backup_number": 35,
                            "folder": "palworldxb",
                            "backup_size": 99,
                            "backup_timestamp": 100,
                        }
                    ]
                }
            )

            async def changed() -> None:
                raise RuntimeError("server started elsewhere")

            store = MemoryJournalStore()
            journal = NativeBackupRestoreJournal(store)
            service = NitradoNativeBackupService(
                client,
                "900001",
                account_entry_id="account_entry_1",
                journal=journal,
                pre_restore_check=changed,
            )
            with self.assertRaisesRegex(NativeBackupError, "preconditions changed"):
                await service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "35"),
                        expected_size_bytes=99,
                        expected_created_at="100",
                    )
                )
            self.assertEqual(client.restores, [])
            self.assertEqual(await journal.async_unresolved_facts(), ())
            diagnostics = await journal.async_diagnostics()
            self.assertEqual(diagnostics["records"][0]["state"], NativeRestoreState.ABORTED_BEFORE_REQUEST)
            self.assertEqual(
                diagnostics["records"][0]["failure_code"],
                NativeRestoreFailureCode.PRECONDITION_FAILED,
            )

        asyncio.run(run())

    def test_restore_refuses_changed_or_missing_backup_before_provider_mutation(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "id": "35",
                            "game": "palworldxb",
                            "size": 43_000,
                            "timestamp": "2026-08-14T13:56:00Z",
                            "status": "ready",
                        }
                    ]
                }
            )
            service, _, _ = tracked_service(client)

            with self.assertRaisesRegex(NativeBackupError, "size changed"):
                await service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "35"),
                        expected_size_bytes=99_000_000,
                        expected_created_at="2026-08-14T13:56:00Z",
                        expected_status="ready",
                    )
                )
            with self.assertRaisesRegex(NativeBackupError, "no longer present"):
                await service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "34"),
                        expected_size_bytes=43_000,
                        expected_created_at="2026-08-14T13:56:00Z",
                        expected_status="ready",
                    )
                )
            self.assertEqual(client.restores, [])

        asyncio.run(run())

    def test_transport_uses_documented_inventory_and_restore_endpoints(self) -> None:
        async def run() -> None:
            inventory_client = RecordingNitradoClient({"status": "success", "data": {"backups": []}})
            data = await inventory_client.list_native_backups("900001")
            self.assertEqual(data, {"backups": []})
            self.assertEqual(
                inventory_client.requests,
                [("GET", "/services/900001/gameservers/backups", {})],
            )

            restore_client = RecordingNitradoClient({"status": "success", "data": {"message": "scheduled"}})
            data = await restore_client.restore_native_backup("900001", "palworldxb", "35")
            self.assertEqual(data, {"message": "scheduled"})
            self.assertEqual(
                restore_client.requests,
                [
                    (
                        "POST",
                        "/services/900001/gameservers/backups/gameserver",
                        {"json": {"folder": "palworldxb", "backup": "35"}},
                    )
                ],
            )

            possible_client = RecordingNitradoClient({"status": "success", "data": {"restore_possible": True}})
            self.assertTrue(await possible_client.native_backup_restore_possible("900001"))
            self.assertEqual(
                possible_client.requests,
                [("GET", "/services/900001/gameservers/backups/restore_possible", {})],
            )

        asyncio.run(run())

    def test_transport_rejects_injection_selectors(self) -> None:
        async def run() -> None:
            client = RecordingNitradoClient({"status": "success", "data": {}})
            with self.assertRaises(NitradoApiError):
                await client.restore_native_backup("900001", "palworldxb\nother", "35")
            with self.assertRaises(NitradoApiError):
                await client.restore_native_backup("900001", "palworldxb", "")
            with self.assertRaises(NitradoApiError):
                await client.restore_native_backup("900001", "../palworldxb", "35")
            with self.assertRaises(NitradoApiError):
                await client.restore_native_backup("900001", "palworldxb", "latest")
            self.assertEqual(client.requests, [])

    def test_restore_requires_provider_restore_possible_before_durable_mutation(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "backup_number": 35,
                            "folder": "palworldxb",
                            "backup_size": 99,
                            "backup_timestamp": 100,
                        }
                    ]
                }
            )
            client.restore_possible = False
            service, journal, _ = tracked_service(client)
            with self.assertRaisesRegex(NativeBackupError, "not currently possible"):
                await service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "35"),
                        expected_size_bytes=99,
                        expected_created_at="100",
                        expected_status=None,
                    )
                )
            self.assertEqual(client.restores, [])
            self.assertEqual(await journal.async_unresolved_facts(), ())

        asyncio.run(run())

    def test_restore_rechecks_documented_type_and_physical_file_size(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": {
                        "gameserver": {
                            "palworldxb": [
                                {
                                    "backup_number": 35,
                                    "backup_timestamp": 100,
                                    "backup_size": 429280246,
                                    "backup_file_size": 45,
                                    "backup_type": "incremental",
                                }
                            ]
                        }
                    }
                }
            )
            service, _, _ = tracked_service(client)
            with self.assertRaisesRegex(NativeBackupError, "file size changed"):
                await service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "35"),
                        expected_size_bytes=429280246,
                        expected_created_at="100",
                        expected_backup_type="incremental",
                        expected_file_size_bytes=46,
                    )
                )
            self.assertEqual(client.restores, [])

        asyncio.run(run())

    def test_documented_inventory_entry_restores_with_exact_documented_preconditions(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": {
                        "gameserver": {
                            "palworldxb": [
                                {
                                    "backup_number": 35,
                                    "backup_timestamp": 100,
                                    "backup_size": 429280246,
                                    "backup_file_size": 45,
                                    "backup_type": "incremental",
                                }
                            ]
                        }
                    }
                }
            )
            service, journal, _ = tracked_service(
                client,
                observer=FakeObserver(NativeBackupRestoreObservation(True, True, "stopped")),
            )
            result = await service.async_restore(
                NativeBackupRestoreRequest(
                    NativeBackupIdentity("palworldxb", "35"),
                    expected_size_bytes=429280246,
                    expected_created_at="100",
                    expected_backup_type="incremental",
                    expected_file_size_bytes=45,
                )
            )
            self.assertTrue(result.provider_restore_observed)
            self.assertFalse(result.game_health_verified)
            self.assertEqual(await journal.async_unresolved_facts(), ())

        asyncio.run(run())

    def test_restore_acceptance_without_observer_remains_actionable_and_unverified(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "backup_number": 35,
                            "folder": "palworldxb",
                            "backup_size": 99,
                            "backup_timestamp": 100,
                        }
                    ]
                }
            )
            service, journal, _ = tracked_service(client)
            result = await service.async_restore(
                NativeBackupRestoreRequest(
                    NativeBackupIdentity("palworldxb", "35"),
                    expected_size_bytes=99,
                    expected_created_at="100",
                    expected_status=None,
                )
            )
            self.assertEqual(result.outcome, NativeBackupRestoreOutcome.ACCEPTED)
            self.assertFalse(result.provider_restore_observed)
            self.assertFalse(result.game_health_verified)
            facts = await journal.async_unresolved_facts()
            self.assertEqual(facts[0].state, NativeRestoreState.ACCEPTED)
            self.assertEqual(facts[0].target.account_entry_id, "account_entry_1")
            self.assertEqual(facts[0].target.folder, "palworldxb")

        asyncio.run(run())

    def test_incomplete_observation_becomes_unknown_and_never_false_success(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "id": "35",
                            "folder": "palworldxb",
                            "size": 99,
                            "timestamp": "100",
                            "status": "ready",
                        }
                    ]
                }
            )
            service, journal, _ = tracked_service(
                client,
                observer=FakeObserver(NativeBackupRestoreObservation(True, False, "backup_restore")),
            )
            with self.assertRaises(NativeBackupRestoreUnconfirmedError):
                await service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "35"),
                        expected_size_bytes=99,
                        expected_created_at="100",
                        expected_status="ready",
                    )
                )
            facts = await journal.async_unresolved_facts()
            self.assertEqual(facts[0].state, NativeRestoreState.OUTCOME_UNKNOWN)
            self.assertEqual(facts[0].failure_code, NativeRestoreFailureCode.OBSERVATION_TIMEOUT)

        asyncio.run(run())

    def test_cancellation_during_provider_request_is_durably_unknown(self) -> None:
        async def run() -> None:
            client = FakeBackupClient(
                {
                    "backups": [
                        {
                            "id": "35",
                            "folder": "palworldxb",
                            "size": 99,
                            "timestamp": "100",
                            "status": "ready",
                        }
                    ]
                }
            )
            entered = asyncio.Event()

            async def blocking_restore(service_id: str, folder: str, backup_id: str) -> dict[str, Any]:
                entered.set()
                await asyncio.Event().wait()
                return {}

            client.restore_native_backup = blocking_restore  # type: ignore[method-assign]
            service, journal, _ = tracked_service(client)
            task = asyncio.create_task(
                service.async_restore(
                    NativeBackupRestoreRequest(
                        NativeBackupIdentity("palworldxb", "35"),
                        expected_size_bytes=99,
                        expected_created_at="100",
                        expected_status="ready",
                    )
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            facts = await journal.async_unresolved_facts()
            self.assertEqual(facts[0].state, NativeRestoreState.OUTCOME_UNKNOWN)
            self.assertEqual(facts[0].failure_code, NativeRestoreFailureCode.CANCELLED)

        asyncio.run(run())

    def test_restart_converts_inflight_restore_to_unknown_repair_fact(self) -> None:
        async def run() -> None:
            store = MemoryJournalStore()
            first = NativeBackupRestoreJournal(store)
            from custom_components.nitrado_gameserver.native_backup_journal import NativeRestoreTarget

            record = await first.async_prepare(
                NativeRestoreTarget(
                    "account_entry_1",
                    "900001",
                    "palworldxb",
                    "35",
                    99,
                    "100",
                    None,
                )
            )
            await first.async_mark_requesting(record.operation_id)

            restarted = NativeBackupRestoreJournal(store)
            await restarted.async_initialize()
            facts = await restarted.async_unresolved_facts()
            self.assertEqual(facts[0].state, NativeRestoreState.OUTCOME_UNKNOWN)
            self.assertEqual(facts[0].failure_code, NativeRestoreFailureCode.PROCESS_RESTARTED)
            self.assertEqual(facts[0].target.backup_id, "35")

        asyncio.run(run())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
