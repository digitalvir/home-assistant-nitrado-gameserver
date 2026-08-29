"""Focused tests for the Home Assistant provider backend boundary."""

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.nitrado_gameserver.backups import (
    NativeBackupIdentity,
    NativeBackupRestoreRequest,
)
from custom_components.nitrado_gameserver.const import DOMAIN, STOPPED_STATUS
from custom_components.nitrado_gameserver.filesystem import FileOperationResult, FileTransportKind, VerificationStrength
from custom_components.nitrado_gameserver.provider_api import (
    ProviderApiError,
    ProviderAuthorizationError,
    ProviderScope,
    ProviderServiceRef,
    ProviderTreeFile,
    ProviderTreeManifest,
    ProviderTreeReplaceRequest,
)
from custom_components.nitrado_gameserver.provider_runtime import (
    HomeAssistantProviderBackend,
    HomeAssistantProviderRuntime,
)


class FakeFilesystem:
    def __init__(self) -> None:
        self.expected_current = None
        self.invalidated: list[str] = []

    async def verify_tree(self, *args, **kwargs) -> None:
        raise AssertionError("backend must not perform a separate stale pre-verification")

    async def replace_tree(
        self,
        service_id,
        root,
        files,
        *,
        expected_current,
        require_stopped,
        mutation_started,
    ):
        del root, files
        self.expected_current = expected_current
        await require_stopped()
        mutation_started()
        return FileOperationResult(
            "transaction-1",
            "replace_tree",
            FileTransportKind.FTPS,
            VerificationStrength.SAME_TRANSPORT,
        )

    def invalidate_provider_state(self, service_id: str) -> None:
        self.invalidated.append(str(service_id))


class FakeReservation:
    def __init__(self) -> None:
        self.phases: list[str] = []

    async def async_mark_dispatched(self) -> None:
        self.phases.append("dispatched")

    async def async_mark_verifying(self) -> None:
        self.phases.append("verifying")


class FakeCoordinator:
    def __init__(self, statuses: list[str]) -> None:
        self.shutting_down = False
        self.services = {"100": object()}
        self.profile_transport = FakeFilesystem()
        self._statuses = list(statuses)
        self.refresh_count = 0
        self.operation_phases: list[str] = []

    @asynccontextmanager
    async def async_operation(self, service_id: str, intent):
        self.assert_service(service_id)
        self.operation_intent = intent
        reservation = FakeReservation()
        yield reservation
        self.operation_phases.extend(reservation.phases)

    def ensure_filesystem_mutation_allowed(self, service_id: str) -> None:
        self.assert_service(service_id)

    async def async_refresh_service(self, service_id: str, *, handle_idle_shutdown: bool):
        self.assert_service(service_id)
        assert handle_idle_shutdown is False
        self.refresh_count += 1
        status = self._statuses.pop(0) if self._statuses else STOPPED_STATUS
        return SimpleNamespace(server=SimpleNamespace(raw_status=status))

    async def async_refresh_filesystem_recovery_facts(self) -> None:
        return None

    @staticmethod
    def assert_service(service_id: str) -> None:
        if str(service_id) != "100":
            raise AssertionError(service_id)


class FakeOwner:
    def __init__(self) -> None:
        self.repair_refreshes: list[str] = []
        self.scope_validations: list[tuple[ProviderServiceRef, frozenset[ProviderScope]]] = []

    async def async_validate_scopes(self, service_ref, coordinator, scopes) -> None:
        del coordinator
        self.scope_validations.append((service_ref, scopes))

    async def async_refresh_native_backup_repair(self, account_entry_id: str) -> None:
        self.repair_refreshes.append(str(account_entry_id))


class ProviderRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, coordinator: FakeCoordinator) -> tuple[HomeAssistantProviderBackend, FakeOwner]:
        owner = FakeOwner()
        hass = SimpleNamespace(data={DOMAIN: {"entry": coordinator}})
        backend = HomeAssistantProviderBackend(
            hass,
            ProviderServiceRef("entry", "100"),
            coordinator,
            SimpleNamespace(),
            owner,
        )
        return backend, owner

    async def test_native_scopes_require_bounded_live_inventory_probe(self) -> None:
        coordinator = FakeCoordinator([STOPPED_STATUS])
        backend, owner = self.backend(coordinator)
        scopes = frozenset({ProviderScope.NATIVE_BACKUP_READ, ProviderScope.NATIVE_BACKUP_RESTORE})

        await backend.async_validate_scopes(scopes)

        self.assertEqual(owner.scope_validations, [(ProviderServiceRef("entry", "100"), scopes)])

    async def test_capability_probe_cache_and_failure_backoff_are_bounded(self) -> None:
        runtime = object.__new__(HomeAssistantProviderRuntime)
        runtime._capability_probe_success = {}
        runtime._capability_probe_failures = {}
        service_ref = ProviderServiceRef("entry", "100")
        calls = 0

        async def succeeds() -> None:
            nonlocal calls
            calls += 1

        await runtime._async_cached_capability_probe(service_ref, "ftp", succeeds)
        await runtime._async_cached_capability_probe(service_ref, "ftp", succeeds)
        self.assertEqual(calls, 1)

        async def fails() -> None:
            raise ProviderApiError("provider unavailable")

        with self.assertRaisesRegex(ProviderApiError, "provider unavailable"):
            await runtime._async_cached_capability_probe(service_ref, "native", fails)
        with self.assertRaisesRegex(ProviderApiError, "temporarily backed off"):
            await runtime._async_cached_capability_probe(service_ref, "native", fails)

    async def test_failed_registry_close_cannot_poison_runtime_recreation(self) -> None:
        hass = SimpleNamespace(data={})
        runtime = object.__new__(HomeAssistantProviderRuntime)
        runtime.hass = hass
        runtime._closed = False
        runtime.registry = SimpleNamespace(async_close=AsyncMock(side_effect=RuntimeError("close failed")))
        runtime._native_backup_journals = {"entry": object()}
        runtime._operation_journals = {}
        runtime._account_coordinators = {}
        runtime._capability_probe_locks = {}
        runtime._capability_probe_success = {}
        runtime._capability_probe_failures = {}
        runtime_key = f"{DOMAIN}_provider_runtime"
        hass.data[runtime_key] = runtime

        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await runtime.async_close()

        self.assertNotIn(runtime_key, hass.data)
        self.assertEqual(runtime._native_backup_journals, {})

    async def test_tree_expected_current_is_checked_inside_filesystem_transaction(self) -> None:
        coordinator = FakeCoordinator([STOPPED_STATUS, STOPPED_STATUS])
        backend, _owner = self.backend(coordinator)
        current = ProviderTreeManifest.from_files((ProviderTreeFile.from_bytes("Level.sav", b"old"),))
        request = ProviderTreeReplaceRequest(
            root="World",
            files=(ProviderTreeFile.from_bytes("Level.sav", b"new"),),
            expected_current=current,
        )

        with patch(
            "custom_components.nitrado_gameserver.provider_runtime.FILESYSTEM_TREE_REPLACE_ENABLED",
            True,
        ):
            result = await backend.async_replace_tree(
                request,
                pre_mutation_check=lambda: None,
                mutation_started=lambda: None,
            )

        self.assertEqual(result.manifest, request.proposed)
        self.assertEqual(coordinator.profile_transport.expected_current.entries[0].sha256, current.entries[0].sha256)
        self.assertEqual(coordinator.operation_intent.kind, "provider-tree-replace")
        self.assertEqual(coordinator.operation_phases, ["dispatched", "verifying"])

    async def test_disabled_tree_replace_rejects_before_check_journal_or_filesystem(self) -> None:
        coordinator = FakeCoordinator([STOPPED_STATUS])
        backend, _owner = self.backend(coordinator)
        current = ProviderTreeManifest.from_files((ProviderTreeFile.from_bytes("Level.sav", b"old"),))
        request = ProviderTreeReplaceRequest(
            root="World",
            files=(ProviderTreeFile.from_bytes("Level.sav", b"new"),),
            expected_current=current,
        )
        checks = 0
        starts = 0

        def pre_mutation_check() -> None:
            nonlocal checks
            checks += 1

        def mutation_started() -> None:
            nonlocal starts
            starts += 1

        with self.assertRaisesRegex(ProviderAuthorizationError, "disabled in this release"):
            await backend.async_replace_tree(
                request,
                pre_mutation_check=pre_mutation_check,
                mutation_started=mutation_started,
            )

        self.assertEqual(checks, 0)
        self.assertEqual(starts, 0)
        self.assertFalse(hasattr(coordinator, "operation_intent"))
        self.assertIsNone(coordinator.profile_transport.expected_current)
        self.assertEqual(coordinator.operation_phases, [])

    async def test_native_restore_rechecks_stopped_and_invalidates_provider_state(self) -> None:
        coordinator = FakeCoordinator([STOPPED_STATUS, "started", STOPPED_STATUS])
        backend, owner = self.backend(coordinator)
        captured_check = None

        class RestoreService:
            async def async_restore(self, request):
                del request
                assert captured_check is not None
                await captured_check()

        def service_factory(_coordinator, *, pre_restore_check=None, mutation_started=None):
            nonlocal captured_check
            captured_check = pre_restore_check
            return RestoreService()

        request = NativeBackupRestoreRequest(
            NativeBackupIdentity("palworldxb", "35"),
            expected_size_bytes=1,
            expected_created_at="2026-08-16T12:00:00Z",
            expected_status="ready",
        )
        with (
            patch.object(backend, "_native_backup_service", side_effect=service_factory),
            patch(
                "custom_components.nitrado_gameserver.provider_runtime.NATIVE_BACKUP_RESTORE_ENABLED",
                True,
            ),
            self.assertRaisesRegex(ProviderApiError, "stably stopped"),
        ):
            await backend.async_restore_native_backup(
                request,
                pre_mutation_check=lambda: None,
                mutation_started=lambda: None,
            )

        self.assertEqual(coordinator.refresh_count, 3)
        self.assertEqual(coordinator.profile_transport.invalidated, ["100"])
        self.assertEqual(owner.repair_refreshes, ["entry"])


if __name__ == "__main__":
    unittest.main()
