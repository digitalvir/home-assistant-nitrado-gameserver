"""Adversarial tests for the versioned provider connector contract."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.nitrado_gameserver import provider_api
from custom_components.nitrado_gameserver.backups import (
    NativeBackup,
    NativeBackupIdentity,
    NativeBackupInventory,
    NativeBackupRestoreRequest,
    NativeBackupRestoreResult,
)
from custom_components.nitrado_gameserver.provider_api import (
    MAX_CONNECTORS_PER_LEASE,
    MAX_CONSUMER_LEASES,
    MAX_CONTENT_HANDLES_PER_CONNECTOR,
    MAX_PLANS_PER_CONNECTOR,
    PROVIDER_CONNECTOR_API_VERSION,
    ProviderApiError,
    ProviderAuthorizationError,
    ProviderCapabilities,
    ProviderConnectorRegistry,
    ProviderFileContent,
    ProviderFileReadRequest,
    ProviderGrant,
    ProviderGrantRequest,
    ProviderLeaseRevokedError,
    ProviderMutationApproval,
    ProviderMutationExecutionError,
    ProviderMutationState,
    ProviderPlanError,
    ProviderScope,
    ProviderServiceRef,
    ProviderTreeContentChunk,
    ProviderTreeContentHandle,
    ProviderTreeFile,
    ProviderTreeManifest,
    ProviderTreeReplaceRequest,
    ProviderTreeReplaceResult,
    ProviderTreeRequest,
    ProviderTreeSnapshot,
    ProviderTreeVerifyRequest,
    ProviderTreeVerifyResult,
    async_register_provider_consumer,
    install_provider_connector_registry,
)


class FakeConfigEntries:
    def __init__(self, domain: str, entry_id: str) -> None:
        self._entry = SimpleNamespace(domain=domain, entry_id=entry_id)

    def async_get_entry(self, entry_id: str):
        return self._entry if entry_id == self._entry.entry_id else None


def installed_hass(
    registry: ProviderConnectorRegistry,
    *,
    domain: str = "save_monitor",
    entry_id: str = "entry",
):
    hass = SimpleNamespace(data={}, config_entries=FakeConfigEntries(domain, entry_id))
    install_provider_connector_registry(hass, registry)
    return hass


class FakeBackend:
    def __init__(self, service_ref: ProviderServiceRef) -> None:
        self.service_ref = service_ref
        self.capabilities = ProviderCapabilities(frozenset(ProviderScope))
        self.files = {
            "world/Level.sav": b"level",
            "world/Players/one.sav": b"player",
        }
        self.restores: list[NativeBackupRestoreRequest] = []
        self.replacements: list[ProviderTreeReplaceRequest] = []
        self.read_started: asyncio.Event | None = None
        self.read_release: asyncio.Event | None = None
        self.snapshot_started: asyncio.Event | None = None
        self.snapshot_release: asyncio.Event | None = None
        self.pre_mutation_started: asyncio.Event | None = None
        self.pre_mutation_release: asyncio.Event | None = None
        self.mutation_started: asyncio.Event | None = None
        self.mutation_release: asyncio.Event | None = None
        self.scope_validation_started: asyncio.Event | None = None
        self.scope_validation_release: asyncio.Event | None = None

    async def async_validate_scopes(self, scopes: frozenset[ProviderScope]) -> None:
        if self.scope_validation_started is not None:
            self.scope_validation_started.set()
        if self.scope_validation_release is not None:
            await self.scope_validation_release.wait()
        if not scopes <= self.capabilities.scopes:
            raise ProviderApiError("unsupported scope")

    async def async_read_file(self, request: ProviderFileReadRequest) -> ProviderFileContent:
        if self.read_started is not None:
            self.read_started.set()
        if self.read_release is not None:
            await self.read_release.wait()
        content = self.files[request.path]
        return ProviderFileContent(
            self.service_ref,
            request.path,
            content,
            ProviderTreeFile.from_bytes(request.path, content).sha256,
        )

    async def async_snapshot_tree(self, request: ProviderTreeRequest) -> ProviderTreeSnapshot:
        if self.snapshot_started is not None:
            self.snapshot_started.set()
        if self.snapshot_release is not None:
            await self.snapshot_release.wait()
        prefix = f"{request.root}/" if request.root else ""
        files = tuple(
            ProviderTreeFile.from_bytes(path.removeprefix(prefix), content)
            for path, content in self.files.items()
            if path.startswith(prefix)
        )
        manifest = ProviderTreeManifest.from_files(files)

        async def chunks(chunk_bytes: int):
            for item in files:
                if not item.content:
                    yield ProviderTreeContentChunk(item.path, 0, b"", True)
                    continue
                for offset in range(0, len(item.content), chunk_bytes):
                    content = item.content[offset : offset + chunk_bytes]
                    yield ProviderTreeContentChunk(
                        item.path,
                        offset,
                        content,
                        offset + len(content) == len(item.content),
                    )

        return ProviderTreeSnapshot(
            self.service_ref,
            request.root,
            manifest,
            ProviderTreeContentHandle(manifest, chunks, lambda: None),
        )

    async def async_verify_tree(self, request: ProviderTreeVerifyRequest) -> ProviderTreeVerifyResult:
        snapshot = await self.async_snapshot_tree(ProviderTreeRequest(request.root))
        try:
            if snapshot.manifest != request.expected:
                raise RuntimeError("mismatch")
            return ProviderTreeVerifyResult(self.service_ref, request.root, snapshot.manifest)
        finally:
            await snapshot.content.async_close()

    async def async_replace_tree(
        self,
        request: ProviderTreeReplaceRequest,
        *,
        pre_mutation_check,
        mutation_started,
    ) -> ProviderTreeReplaceResult:
        if self.pre_mutation_started is not None:
            self.pre_mutation_started.set()
        if self.pre_mutation_release is not None:
            await self.pre_mutation_release.wait()
        pre_mutation_check()
        mutation_started()
        if self.mutation_started is not None:
            self.mutation_started.set()
        if self.mutation_release is not None:
            await self.mutation_release.wait()
        self.replacements.append(request)
        prefix = f"{request.root}/"
        self.files = {path: content for path, content in self.files.items() if not path.startswith(prefix)}
        self.files.update({f"{request.root}/{item.path}": item.content for item in request.files})
        return ProviderTreeReplaceResult(
            self.service_ref,
            request.root,
            request.proposed,
            "operation-1",
            True,
        )

    async def async_list_native_backups(self, *, limit: int) -> NativeBackupInventory:
        return NativeBackupInventory(
            self.service_ref.service_id,
            (
                NativeBackup(
                    NativeBackupIdentity("palworldxb", "35"),
                    created_at="2026-08-16T12:00:00Z",
                    size_bytes=99_000_000,
                    status="ready",
                ),
            )[:limit],
        )

    async def async_restore_native_backup(
        self,
        request: NativeBackupRestoreRequest,
        *,
        pre_mutation_check,
        mutation_started,
    ) -> NativeBackupRestoreResult:
        if self.pre_mutation_started is not None:
            self.pre_mutation_started.set()
        if self.pre_mutation_release is not None:
            await self.pre_mutation_release.wait()
        pre_mutation_check()
        mutation_started()
        if self.mutation_started is not None:
            self.mutation_started.set()
        if self.mutation_release is not None:
            await self.mutation_release.wait()
        self.restores.append(request)
        return NativeBackupRestoreResult(
            self.service_ref.service_id,
            request.identity,
            provider_accepted=True,
            provider_returned_data=False,
        )


def grant(request: ProviderGrantRequest) -> ProviderGrant:
    return ProviderGrant(
        "grant-1",
        request.consumer_domain,
        request.consumer_entry_id,
        request.service_ref,
        request.requested_scopes,
        permitted_roots=request.requested_roots,
    )


class TokenAuthorizer:
    def __init__(self) -> None:
        self.tokens = {"core-token"}

    def __call__(self, plan, approval) -> bool:
        if approval.approval_token not in self.tokens:
            return False
        self.tokens.remove(approval.approval_token)
        return True


async def connector_for(
    backend: FakeBackend,
    scopes: set[ProviderScope],
    *,
    authorizer=None,
    clock=None,
):
    registry = ProviderConnectorRegistry(
        backend_resolver=lambda ref: backend,
        grant_authorizer=grant,
        approval_authorizer=authorizer or TokenAuthorizer(),
        **({"clock": clock} if clock else {}),
    )
    lease = await async_register_provider_consumer(
        installed_hass(registry),
        api_version=PROVIDER_CONNECTOR_API_VERSION,
        consumer_domain="save_monitor",
        consumer_entry_id="entry",
    )
    roots = (
        {"world"}
        if scopes
        & {
            ProviderScope.FILESYSTEM_READ,
            ProviderScope.FILESYSTEM_SNAPSHOT,
            ProviderScope.FILESYSTEM_REPLACE,
        }
        else set()
    )
    connector = await lease.async_get_connector(backend.service_ref, scopes=scopes, roots=roots)
    return registry, lease, connector


def restore_request() -> NativeBackupRestoreRequest:
    return NativeBackupRestoreRequest(
        NativeBackupIdentity("palworldxb", "35"),
        expected_size_bytes=99_000_000,
        expected_created_at="2026-08-16T12:00:00Z",
        expected_status="ready",
    )


def approval_for(plan, token: str = "core-token") -> ProviderMutationApproval:
    return ProviderMutationApproval(plan.plan_id, plan.payload_digest, token)


class ProviderApiTests(unittest.TestCase):
    def test_requires_account_scoped_backend_binding(self) -> None:
        async def run() -> None:
            ref = ProviderServiceRef("account-one", "900001")
            wrong = FakeBackend(ProviderServiceRef("account-two", "900001"))
            registry = ProviderConnectorRegistry(
                backend_resolver=lambda requested: wrong,
                grant_authorizer=grant,
                approval_authorizer=TokenAuthorizer(),
            )
            lease = await async_register_provider_consumer(
                installed_hass(registry),
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry",
            )
            with self.assertRaisesRegex(ProviderApiError, "account-scoped"):
                await lease.async_get_connector(ref, scopes={ProviderScope.FILESYSTEM_READ}, roots={"world"})

        asyncio.run(run())

    def test_registry_rejects_unbounded_leases_connectors_and_snapshot_handles(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            registry = ProviderConnectorRegistry(
                backend_resolver=lambda ref: backend,
                grant_authorizer=grant,
                approval_authorizer=TokenAuthorizer(),
            )
            leases = [
                registry.register_consumer(
                    api_version=PROVIDER_CONNECTOR_API_VERSION,
                    consumer_domain="save_monitor",
                    consumer_entry_id=f"entry-{index}",
                )
                for index in range(MAX_CONSUMER_LEASES)
            ]
            with self.assertRaisesRegex(ProviderApiError, "lease limit"):
                registry.register_consumer(
                    api_version=PROVIDER_CONNECTOR_API_VERSION,
                    consumer_domain="save_monitor",
                    consumer_entry_id="one-too-many",
                )

            lease = leases[0]
            connectors = [
                await lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_SNAPSHOT},
                    roots={"world"},
                )
                for _ in range(MAX_CONNECTORS_PER_LEASE)
            ]
            with self.assertRaisesRegex(ProviderApiError, "companion"):
                await lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_SNAPSHOT},
                    roots={"world"},
                )

            handles = [
                await connectors[0].async_snapshot_tree(ProviderTreeRequest("world"))
                for _ in range(MAX_CONTENT_HANDLES_PER_CONNECTOR)
            ]
            with self.assertRaisesRegex(ProviderApiError, "handle limit"):
                await connectors[0].async_snapshot_tree(ProviderTreeRequest("world"))
            for snapshot in handles:
                await snapshot.content.async_close()

        asyncio.run(run())

    def test_bounded_typed_filesystem_read_snapshot_and_verify(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            _, _, connector = await connector_for(
                backend,
                {ProviderScope.FILESYSTEM_READ, ProviderScope.FILESYSTEM_SNAPSHOT},
            )
            content = await connector.async_read_file(ProviderFileReadRequest("world/Level.sav", max_bytes=100))
            self.assertEqual(content.content, b"level")
            snapshot = await connector.async_snapshot_tree(
                ProviderTreeRequest("world", max_files=10, max_bytes=1000, max_depth=4)
            )
            streamed = {}
            async for chunk in snapshot.content.async_chunks(chunk_bytes=3):
                streamed.setdefault(chunk.path, bytearray()).extend(chunk.content)
            self.assertEqual(
                {path: bytes(content) for path, content in streamed.items()},
                {
                    "Level.sav": b"level",
                    "Players/one.sav": b"player",
                },
            )
            verified = await connector.async_verify_tree(ProviderTreeVerifyRequest("world", snapshot.manifest))
            self.assertEqual(verified.manifest, snapshot.manifest)
            with self.assertRaises(ValueError):
                ProviderFileReadRequest("../secret")
            with self.assertRaisesRegex(ProviderAuthorizationError, "granted roots"):
                await connector.async_read_file(ProviderFileReadRequest("world2/Level.sav"))

        asyncio.run(run())

    def test_filesystem_scope_requires_exact_granted_roots(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))

            def rootless_grant(request: ProviderGrantRequest) -> ProviderGrant:
                return ProviderGrant(
                    "grant",
                    request.consumer_domain,
                    request.consumer_entry_id,
                    request.service_ref,
                    request.requested_scopes,
                )

            registry = ProviderConnectorRegistry(
                backend_resolver=lambda ref: backend,
                grant_authorizer=rootless_grant,
                approval_authorizer=TokenAuthorizer(),
            )
            lease = await async_register_provider_consumer(
                installed_hass(registry),
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry",
            )
            with self.assertRaisesRegex(ProviderAuthorizationError, "exact consumer request"):
                await lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_READ},
                    roots={"world"},
                )
            with self.assertRaisesRegex(ProviderAuthorizationError, "require exact roots"):
                await lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_READ},
                )
            with self.assertRaisesRegex(ProviderAuthorizationError, "cannot request roots"):
                await lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.NATIVE_BACKUP_READ},
                    roots={"world"},
                )

        asyncio.run(run())

    def test_provider_paths_reject_unicode_and_case_aliases(self) -> None:
        with self.assertRaisesRegex(ValueError, "NFC-normalized"):
            ProviderTreeFile.from_bytes("Cafe\u0301/Level.sav", b"x")
        upper = ProviderTreeFile.from_bytes("Level.sav", b"one")
        lower = ProviderTreeFile.from_bytes("level.sav", b"two")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ProviderTreeManifest.from_files((upper, lower))

    def test_tree_replace_is_two_phase_exact_and_one_shot(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            registry, _, connector = await connector_for(
                backend,
                {ProviderScope.FILESYSTEM_SNAPSHOT, ProviderScope.FILESYSTEM_REPLACE},
            )
            current = await connector.async_snapshot_tree(ProviderTreeRequest("world"))
            await current.content.async_close()
            request = ProviderTreeReplaceRequest(
                "world",
                (ProviderTreeFile.from_bytes("Level.sav", b"new"),),
                current.manifest,
            )
            plan = connector.plan_tree_replace(request)
            self.assertEqual(
                registry.get_pending_plan(plan.plan_id, plan.payload_digest),
                plan,
            )
            with self.assertRaises(ProviderPlanError):
                registry.get_pending_plan(plan.plan_id, "0" * 64)
            result = await connector.async_execute_tree_replace(plan, approval_for(plan))
            self.assertEqual(result.manifest, request.proposed)
            self.assertEqual(backend.replacements, [request])
            with self.assertRaisesRegex(ProviderPlanError, "already consumed"):
                await connector.async_execute_tree_replace(plan, approval_for(plan, "other"))
            with self.assertRaises(ProviderPlanError):
                registry.get_pending_plan(plan.plan_id, plan.payload_digest)

        asyncio.run(run())

    def test_core_review_broker_delivers_exact_approval_to_companion(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            registry, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            waiting = asyncio.create_task(connector.async_wait_for_approval(plan))
            await asyncio.sleep(0)
            self.assertEqual(registry.pending_plans(), (plan,))
            issued = approval_for(plan)
            registry.publish_approval(issued)
            self.assertEqual(await waiting, issued)
            result = await connector.async_execute_native_backup_restore(plan, issued)
            self.assertTrue(result.provider_accepted)

        asyncio.run(run())

    def test_core_review_rejection_refuses_plan_and_wakes_companion(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            registry, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            waiting = asyncio.create_task(connector.async_wait_for_approval(plan))
            await asyncio.sleep(0)
            rejected = registry.reject_pending_plan(plan.plan_id, plan.payload_digest)
            self.assertEqual(rejected, plan)
            with self.assertRaisesRegex(ProviderPlanError, "administrator rejected"):
                await waiting
            progress = connector.mutation_progress(plan)
            self.assertEqual(progress.state, ProviderMutationState.REFUSED)
            self.assertEqual(progress.error_code, "AdministratorRejected")
            self.assertEqual(backend.restores, [])

        asyncio.run(run())

    def test_disabled_mutation_scope_blocks_approval_but_allows_stale_plan_rejection(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            registry, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            registry._disabled_scopes = frozenset({ProviderScope.NATIVE_BACKUP_RESTORE})

            with self.assertRaisesRegex(ProviderAuthorizationError, "disabled in this release"):
                registry.get_pending_plan(plan.plan_id, plan.payload_digest)
            rejected = registry.reject_pending_plan(plan.plan_id, plan.payload_digest)

            self.assertEqual(rejected, plan)
            self.assertEqual(backend.restores, [])

        asyncio.run(run())

    def test_native_restore_requires_opaque_core_token_and_strong_preconditions(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            _, _, connector = await connector_for(
                backend,
                {ProviderScope.NATIVE_BACKUP_READ, ProviderScope.NATIVE_BACKUP_RESTORE},
            )
            inventory = await connector.async_list_native_backups(limit=5)
            self.assertEqual(inventory.backups[0].status, "ready")
            plan = connector.plan_native_backup_restore(restore_request())
            result = await connector.async_execute_native_backup_restore(plan, approval_for(plan))
            self.assertTrue(result.provider_accepted)
            self.assertEqual(backend.restores, [restore_request()])
            # Status values are provider facts, not a core-invented enum. The
            # restore service binds and rechecks the exact value while also
            # requiring Nitrado's restore_possible witness.
            transitional = NativeBackupRestoreRequest(
                NativeBackupIdentity("palworldxb", "35"),
                expected_size_bytes=99,
                expected_created_at="2026-08-16",
                expected_status="creating",
            )
            self.assertEqual(transitional.expected_status, "creating")

        asyncio.run(run())

    def test_invalid_token_consumes_exact_plan_without_mutation(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            _, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            with self.assertRaisesRegex(ProviderAuthorizationError, "Core did not authorize"):
                await connector.async_execute_native_backup_restore(plan, approval_for(plan, "companion-forged-token"))
            with self.assertRaises(ProviderPlanError):
                await connector.async_execute_native_backup_restore(plan, approval_for(plan))
            self.assertEqual(backend.restores, [])

        asyncio.run(run())

    def test_acquisition_revoked_while_authorizer_awaits_cannot_resurrect(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_grant(request):
                started.set()
                await release.wait()
                return grant(request)

            registry = ProviderConnectorRegistry(
                backend_resolver=lambda ref: backend,
                grant_authorizer=slow_grant,
                approval_authorizer=TokenAuthorizer(),
            )
            lease = await async_register_provider_consumer(
                installed_hass(registry),
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry",
            )
            acquiring = asyncio.create_task(
                lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_READ},
                    roots={"world"},
                )
            )
            await started.wait()
            await lease.async_close()
            release.set()
            with self.assertRaises(ProviderLeaseRevokedError):
                await acquiring

        asyncio.run(run())

    def test_acquisition_revoked_while_backend_resolver_awaits_cannot_resurrect(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_backend(ref):
                started.set()
                await release.wait()
                return backend

            registry = ProviderConnectorRegistry(
                backend_resolver=slow_backend,
                grant_authorizer=grant,
                approval_authorizer=TokenAuthorizer(),
            )
            lease = await async_register_provider_consumer(
                installed_hass(registry),
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry",
            )
            acquiring = asyncio.create_task(
                lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_READ},
                    roots={"world"},
                )
            )
            await started.wait()
            await lease.async_close()
            release.set()
            with self.assertRaises(ProviderLeaseRevokedError):
                await acquiring

        asyncio.run(run())

    def test_grant_revocation_during_capability_probe_cannot_commit_connector(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.scope_validation_started = asyncio.Event()
            backend.scope_validation_release = asyncio.Event()
            registry = ProviderConnectorRegistry(
                backend_resolver=lambda service_ref: backend,
                grant_authorizer=grant,
                approval_authorizer=TokenAuthorizer(),
            )
            lease = registry.register_consumer(
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry",
            )
            acquiring = asyncio.create_task(
                lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_READ},
                    roots={"world"},
                )
            )
            await backend.scope_validation_started.wait()
            await registry.async_revoke_grant("grant-1")
            backend.scope_validation_release.set()
            with self.assertRaisesRegex(ProviderLeaseRevokedError, "grants changed"):
                await acquiring

        asyncio.run(run())

    def test_service_invalidation_during_backend_resolution_cannot_resurrect(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_backend(ref):
                started.set()
                await release.wait()
                return backend

            registry = ProviderConnectorRegistry(
                backend_resolver=slow_backend,
                grant_authorizer=grant,
                approval_authorizer=TokenAuthorizer(),
            )
            lease = await async_register_provider_consumer(
                installed_hass(registry),
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry",
            )
            acquiring = asyncio.create_task(
                lease.async_get_connector(
                    backend.service_ref,
                    scopes={ProviderScope.FILESYSTEM_READ},
                    roots={"world"},
                )
            )
            await started.wait()
            await registry.async_invalidate_service(backend.service_ref)
            release.set()
            with self.assertRaisesRegex(ProviderLeaseRevokedError, "service changed"):
                await acquiring

        asyncio.run(run())

    def test_read_result_is_rejected_after_service_revocation_and_invalidation_drains(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.read_started = asyncio.Event()
            backend.read_release = asyncio.Event()
            registry, _, connector = await connector_for(backend, {ProviderScope.FILESYSTEM_READ})
            reading = asyncio.create_task(connector.async_read_file(ProviderFileReadRequest("world/Level.sav")))
            await backend.read_started.wait()
            invalidating = asyncio.create_task(registry.async_invalidate_service(backend.service_ref))
            await asyncio.sleep(0)
            self.assertFalse(invalidating.done())
            backend.read_release.set()
            with self.assertRaises(ProviderLeaseRevokedError):
                await reading
            await invalidating

        asyncio.run(run())

    def test_grant_revocation_revokes_live_connector_plans_and_drains_started_work(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.read_started = asyncio.Event()
            backend.read_release = asyncio.Event()
            registry, _, connector = await connector_for(
                backend,
                {ProviderScope.FILESYSTEM_READ, ProviderScope.FILESYSTEM_REPLACE},
            )
            current = await backend.async_snapshot_tree(ProviderTreeRequest("world"))
            try:
                plan = connector.plan_tree_replace(
                    ProviderTreeReplaceRequest(
                        root="world",
                        files=(ProviderTreeFile.from_bytes("Level.sav", b"new"),),
                        expected_current=current.manifest,
                    )
                )
            finally:
                await current.content.async_close()
            reading = asyncio.create_task(connector.async_read_file(ProviderFileReadRequest("world/Level.sav")))
            await backend.read_started.wait()

            revoking = asyncio.create_task(registry.async_revoke_grant("grant-1"))
            await asyncio.sleep(0)
            self.assertFalse(connector.active)
            self.assertFalse(revoking.done())
            with self.assertRaises(ProviderPlanError):
                registry.get_pending_plan(plan.plan_id, plan.payload_digest)

            backend.read_release.set()
            with self.assertRaises(ProviderLeaseRevokedError):
                await reading
            await revoking

        asyncio.run(run())

    def test_grant_revocation_drains_only_affected_connectors(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            started_a = asyncio.Event()
            started_b = asyncio.Event()
            release_a = asyncio.Event()
            release_b = asyncio.Event()

            async def split_read(request: ProviderFileReadRequest) -> ProviderFileContent:
                if request.path.endswith("Level.sav"):
                    started_a.set()
                    await release_a.wait()
                else:
                    started_b.set()
                    await release_b.wait()
                content = backend.files[request.path]
                return ProviderFileContent(
                    backend.service_ref,
                    request.path,
                    content,
                    ProviderTreeFile.from_bytes(request.path, content).sha256,
                )

            def separate_grants(request: ProviderGrantRequest) -> ProviderGrant:
                return ProviderGrant(
                    f"grant-{request.consumer_entry_id}",
                    request.consumer_domain,
                    request.consumer_entry_id,
                    request.service_ref,
                    request.requested_scopes,
                    permitted_roots=request.requested_roots,
                )

            backend.async_read_file = split_read  # type: ignore[method-assign]
            registry = ProviderConnectorRegistry(
                backend_resolver=lambda ref: backend,
                grant_authorizer=separate_grants,
                approval_authorizer=TokenAuthorizer(),
            )
            lease_a = registry.register_consumer(
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="save_monitor",
                consumer_entry_id="entry-a",
            )
            lease_b = registry.register_consumer(
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="backup_browser",
                consumer_entry_id="entry-b",
            )
            connector_a = await lease_a.async_get_connector(
                backend.service_ref,
                scopes={ProviderScope.FILESYSTEM_READ},
                roots={"world"},
            )
            connector_b = await lease_b.async_get_connector(
                backend.service_ref,
                scopes={ProviderScope.FILESYSTEM_READ},
                roots={"world"},
            )
            reading_a = asyncio.create_task(
                connector_a.async_read_file(ProviderFileReadRequest("world/Level.sav", max_bytes=16))
            )
            reading_b = asyncio.create_task(
                connector_b.async_read_file(ProviderFileReadRequest("world/Players/one.sav", max_bytes=16))
            )
            await asyncio.gather(started_a.wait(), started_b.wait())

            revoking = asyncio.create_task(registry.async_revoke_grant("grant-entry-a"))
            await asyncio.sleep(0)
            release_a.set()
            with self.assertRaises(ProviderLeaseRevokedError):
                await reading_a
            await asyncio.wait_for(asyncio.shield(revoking), timeout=0.5)
            self.assertFalse(reading_b.done())
            self.assertTrue(connector_b.active)

            release_b.set()
            self.assertEqual((await reading_b).content, b"player")

        asyncio.run(run())

    def test_grant_revocation_while_waiting_prevents_first_mutation(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.pre_mutation_started = asyncio.Event()
            backend.pre_mutation_release = asyncio.Event()
            registry, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            execution = asyncio.create_task(connector.async_execute_native_backup_restore(plan, approval_for(plan)))
            await backend.pre_mutation_started.wait()

            revoking = asyncio.create_task(registry.async_revoke_grant("grant-1"))
            await asyncio.sleep(0)
            self.assertFalse(revoking.done())
            backend.pre_mutation_release.set()

            with self.assertRaises(ProviderLeaseRevokedError):
                await execution
            await revoking
            self.assertEqual(backend.restores, [])

        asyncio.run(run())

    def test_started_mutation_drains_through_quiesce_and_caller_cancellation(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.mutation_started = asyncio.Event()
            backend.mutation_release = asyncio.Event()
            registry, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            execution = asyncio.create_task(connector.async_execute_native_backup_restore(plan, approval_for(plan)))
            await backend.mutation_started.wait()
            execution.cancel()
            quiescing = asyncio.create_task(registry.async_quiesce())
            await asyncio.sleep(0)
            self.assertFalse(execution.done())
            self.assertFalse(quiescing.done())
            backend.mutation_release.set()
            self.assertTrue((await execution).provider_accepted)
            await quiescing
            with self.assertRaises(ProviderLeaseRevokedError):
                registry.register_consumer(
                    api_version=PROVIDER_CONNECTOR_API_VERSION,
                    consumer_domain="x",
                    consumer_entry_id="y",
                )
            registry.async_resume()
            resumed = registry.register_consumer(
                api_version=PROVIDER_CONNECTOR_API_VERSION,
                consumer_domain="x",
                consumer_entry_id="y",
            )
            self.assertTrue(resumed.active)
            await registry.async_close()
            with self.assertRaises(ProviderLeaseRevokedError):
                registry.async_resume()

        asyncio.run(run())

    def test_long_mutation_exposes_bounded_progress_lifecycle(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.mutation_started = asyncio.Event()
            backend.mutation_release = asyncio.Event()
            _, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            self.assertEqual(connector.mutation_progress(plan).state, ProviderMutationState.PLANNED)

            execution = asyncio.create_task(connector.async_execute_native_backup_restore(plan, approval_for(plan)))
            await backend.mutation_started.wait()
            running = connector.mutation_progress(plan)
            self.assertEqual(running.state, ProviderMutationState.RUNNING)
            self.assertEqual(running.audit_id, plan.audit_id)
            self.assertIsNone(running.error_code)

            backend.mutation_release.set()
            await execution
            self.assertEqual(connector.mutation_progress(plan).state, ProviderMutationState.COMPLETED)

        asyncio.run(run())

    def test_lease_close_drains_started_mutation(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.mutation_started = asyncio.Event()
            backend.mutation_release = asyncio.Event()
            _, lease, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            execution = asyncio.create_task(connector.async_execute_native_backup_restore(plan, approval_for(plan)))
            await backend.mutation_started.wait()
            closing = asyncio.create_task(lease.async_close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            backend.mutation_release.set()
            await execution
            await closing
            self.assertFalse(lease.active)

        asyncio.run(run())

    def test_plan_expiring_during_core_approval_never_mutates(self) -> None:
        async def run() -> None:
            now = [100.0]
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            started = asyncio.Event()
            release = asyncio.Event()

            async def delayed_approval(plan, approval):
                started.set()
                await release.wait()
                return True

            _, _, connector = await connector_for(
                backend,
                {ProviderScope.NATIVE_BACKUP_RESTORE},
                authorizer=delayed_approval,
                clock=lambda: now[0],
            )
            plan = connector.plan_native_backup_restore(restore_request(), ttl_seconds=1)
            execution = asyncio.create_task(connector.async_execute_native_backup_restore(plan, approval_for(plan)))
            await started.wait()
            now[0] = 101.0
            release.set()
            with self.assertRaisesRegex(ProviderPlanError, "approval was pending"):
                await execution
            self.assertEqual(backend.restores, [])
            self.assertEqual(connector.mutation_progress(plan).state, ProviderMutationState.REFUSED)

        asyncio.run(run())

    def test_plan_caps_and_expired_plan_pruning(self) -> None:
        async def run() -> None:
            now = [100.0]
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            _, _, connector = await connector_for(
                backend,
                {ProviderScope.NATIVE_BACKUP_RESTORE},
                clock=lambda: now[0],
            )
            for _ in range(MAX_PLANS_PER_CONNECTOR):
                connector.plan_native_backup_restore(restore_request(), ttl_seconds=1)
            with self.assertRaisesRegex(ProviderPlanError, "limit"):
                connector.plan_native_backup_restore(restore_request())
            now[0] = 102.0
            connector.plan_native_backup_restore(restore_request())

        asyncio.run(run())

    def test_tree_plan_bytes_are_reserved_and_released(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            _, _, connector = await connector_for(backend, {ProviderScope.FILESYSTEM_REPLACE})
            current = ProviderTreeManifest.from_files((ProviderTreeFile.from_bytes("old.sav", b"old"),))
            request = ProviderTreeReplaceRequest(
                root="world",
                files=(ProviderTreeFile.from_bytes("Level.sav", b"four"),),
                expected_current=current,
            )
            with (
                patch.object(provider_api, "MAX_PENDING_PLAN_BYTES_PER_CONNECTOR", 6),
                patch.object(provider_api, "MAX_PENDING_PLAN_BYTES_TOTAL", 6),
            ):
                first = connector.plan_tree_replace(request)
                with self.assertRaisesRegex(ProviderPlanError, "byte limit"):
                    connector.plan_tree_replace(request)
                connector._registry.reject_pending_plan(first.plan_id, first.payload_digest)
                connector.plan_tree_replace(request)

        asyncio.run(run())

    def test_executing_tree_plan_keeps_its_byte_reservation(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            authorizing = asyncio.Event()
            release = asyncio.Event()

            async def delayed_authorizer(plan, approval):
                authorizing.set()
                await release.wait()
                return True

            _, _, connector = await connector_for(
                backend,
                {ProviderScope.FILESYSTEM_REPLACE},
                authorizer=delayed_authorizer,
            )
            current = ProviderTreeManifest.from_files((ProviderTreeFile.from_bytes("old.sav", b"old"),))
            request = ProviderTreeReplaceRequest(
                root="world",
                files=(ProviderTreeFile.from_bytes("Level.sav", b"four"),),
                expected_current=current,
            )
            with (
                patch.object(provider_api, "MAX_PENDING_PLAN_BYTES_PER_CONNECTOR", 6),
                patch.object(provider_api, "MAX_PENDING_PLAN_BYTES_TOTAL", 6),
            ):
                first = connector.plan_tree_replace(request)
                execution = asyncio.create_task(connector.async_execute_tree_replace(first, approval_for(first)))
                await authorizing.wait()
                with self.assertRaisesRegex(ProviderPlanError, "byte limit"):
                    connector.plan_tree_replace(request)
                release.set()
                await execution
                connector.plan_tree_replace(request)

        asyncio.run(run())

    def test_connector_drain_event_is_removed_after_lease_close(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            registry, lease, connector = await connector_for(backend, {ProviderScope.FILESYSTEM_READ})
            await connector.async_read_file(ProviderFileReadRequest("world/Level.sav", max_bytes=16))
            self.assertIn(connector.connector_id, registry._connector_drained_events)
            await lease.async_close()
            self.assertNotIn(connector.connector_id, registry._connector_drained_events)

        asyncio.run(run())

    def test_snapshot_reservation_precedes_backend_materialization(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.snapshot_started = asyncio.Event()
            backend.snapshot_release = asyncio.Event()
            _, _, connector = await connector_for(backend, {ProviderScope.FILESYSTEM_SNAPSHOT})
            with (
                patch.object(provider_api, "MAX_OPEN_SNAPSHOT_BYTES_TOTAL", 20),
                patch.object(provider_api, "MAX_OPEN_SNAPSHOT_BYTES_PER_CONNECTOR", 20),
            ):
                first = asyncio.create_task(connector.async_snapshot_tree(ProviderTreeRequest("world", max_bytes=16)))
                await backend.snapshot_started.wait()
                with self.assertRaisesRegex(ProviderApiError, "reservation limit"):
                    await connector.async_snapshot_tree(ProviderTreeRequest("world", max_bytes=16))
                backend.snapshot_release.set()
                snapshot = await first
                await snapshot.content.async_close()

        asyncio.run(run())

    def test_malformed_snapshot_result_releases_admission_reservation(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            _, _, connector = await connector_for(backend, {ProviderScope.FILESYSTEM_SNAPSHOT})
            valid_snapshot = backend.async_snapshot_tree

            async def malformed(request):
                return object()

            backend.async_snapshot_tree = malformed  # type: ignore[method-assign]
            with self.assertRaisesRegex(ProviderApiError, "different service"):
                await connector.async_snapshot_tree(ProviderTreeRequest("world", max_bytes=16))
            self.assertEqual(connector._registry._snapshot_reservations, 0)
            self.assertEqual(connector._registry._snapshot_reserved_bytes, 0)

            backend.async_snapshot_tree = valid_snapshot  # type: ignore[method-assign]
            snapshot = await connector.async_snapshot_tree(ProviderTreeRequest("world", max_bytes=16))
            await snapshot.content.async_close()

        asyncio.run(run())

    def test_file_read_bytes_are_reserved_before_backend_call(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))
            backend.read_started = asyncio.Event()
            backend.read_release = asyncio.Event()
            _, _, connector = await connector_for(backend, {ProviderScope.FILESYSTEM_READ})
            with (
                patch.object(provider_api, "MAX_INFLIGHT_FILE_READ_BYTES_TOTAL", 8),
                patch.object(provider_api, "MAX_INFLIGHT_FILE_READ_BYTES_PER_CONNECTOR", 8),
            ):
                first = asyncio.create_task(
                    connector.async_read_file(ProviderFileReadRequest("world/Level.sav", max_bytes=8))
                )
                await backend.read_started.wait()
                with self.assertRaisesRegex(ProviderApiError, "byte reservation limit"):
                    await connector.async_read_file(ProviderFileReadRequest("world/Level.sav", max_bytes=8))
                backend.read_release.set()
                await first

        asyncio.run(run())

    def test_backend_mutation_failure_is_unconfirmed_and_secret_safe(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))

            async def broken(request, *, pre_mutation_check, mutation_started):
                pre_mutation_check()
                mutation_started()
                raise RuntimeError("password=do-not-leak")

            backend.async_restore_native_backup = broken  # type: ignore[method-assign]
            _, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            with self.assertRaises(ProviderMutationExecutionError) as caught:
                await connector.async_execute_native_backup_restore(plan, approval_for(plan))
            self.assertEqual(caught.exception.audit_id, plan.audit_id)
            self.assertNotIn("do-not-leak", str(caught.exception))
            self.assertEqual(connector.mutation_progress(plan).state, ProviderMutationState.FAILED)

        asyncio.run(run())

    def test_malformed_result_after_started_mutation_is_outcome_unknown(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))

            async def malformed(request, *, pre_mutation_check, mutation_started):
                pre_mutation_check()
                mutation_started()
                return NativeBackupRestoreResult(
                    backend.service_ref.service_id,
                    NativeBackupIdentity("palworldxb", "999"),
                    provider_accepted=True,
                    provider_returned_data=False,
                )

            backend.async_restore_native_backup = malformed  # type: ignore[method-assign]
            _, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            with self.assertRaises(ProviderMutationExecutionError):
                await connector.async_execute_native_backup_restore(plan, approval_for(plan))
            self.assertEqual(connector.mutation_progress(plan).state, ProviderMutationState.FAILED)

        asyncio.run(run())

    def test_pre_mutation_refusal_is_not_reported_as_unknown(self) -> None:
        async def run() -> None:
            backend = FakeBackend(ProviderServiceRef("account", "900001"))

            async def refused(request, *, pre_mutation_check, mutation_started):
                pre_mutation_check()
                raise ProviderApiError("server started before mutation")

            backend.async_restore_native_backup = refused  # type: ignore[method-assign]
            _, _, connector = await connector_for(backend, {ProviderScope.NATIVE_BACKUP_RESTORE})
            plan = connector.plan_native_backup_restore(restore_request())
            with self.assertRaisesRegex(ProviderApiError, "started before mutation"):
                await connector.async_execute_native_backup_restore(plan, approval_for(plan))
            progress = connector.mutation_progress(plan)
            self.assertEqual(progress.state, ProviderMutationState.REFUSED)
            self.assertEqual(progress.error_code, "ProviderApiError")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
