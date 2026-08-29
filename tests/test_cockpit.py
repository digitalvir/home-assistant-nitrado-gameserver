"""Tests for owner-bound cockpit assets and server-authoritative leases."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.nitrado_gameserver.cockpit import (
    CockpitAssetError,
    CockpitAssetRecord,
    CockpitLeaseError,
    async_prepare_cockpit_assets,
    async_register_builtin_cockpit_assets,
    cockpit_asset_by_identity,
    cockpit_state,
    consume_cockpit_handoff,
    consume_editable_preview_grant,
    consume_save_bundle_preview_grant,
    consume_save_bundle_transfer_grant,
    ensure_cockpit_assets_registerable,
    issue_cockpit_handoff,
    issue_cockpit_lease,
    issue_editable_preview_grant,
    issue_save_bundle_preview_grant,
    issue_save_bundle_transfer_grant,
    publish_cockpit_account_epoch,
    publish_cockpit_epoch,
    register_cockpit_assets,
    require_cockpit_lease,
    unregister_cockpit_assets,
)
from custom_components.nitrado_gameserver.plugins.base import (
    COCKPIT_API_VERSION,
    CockpitDeclaration,
    LocalCockpitAsset,
)
from custom_components.nitrado_gameserver.plugins.generic import GenericProfile


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def async_fire(self, event: str, data: dict[str, object]) -> None:
        self.events.append((event, data))


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.bus = FakeBus()

    async def async_add_executor_job(self, callback, *args):
        return callback(*args)


class FakeCoordinator:
    def __init__(self, declaration: CockpitDeclaration, account_entry_id: str = "entry-a") -> None:
        self.account_entry_id = account_entry_id
        profile = SimpleNamespace(profile_id="palworld")
        manifest = SimpleNamespace(cockpit=declaration)
        self.runtime = SimpleNamespace(profile=profile, profile_manifest=manifest)
        self.token = (3, "palworld", 4, 5, 6)

    def get_runtime(self, service_id: str):
        if service_id != "123":
            raise KeyError(service_id)
        return self.runtime

    def profile_dispatch_token(self, service_id: str):
        self.get_runtime(service_id)
        return self.token

    def require_profile_dispatch_token(self, service_id: str, token):
        self.get_runtime(service_id)
        if token != self.token:
            raise RuntimeError("stale")
        return self.runtime


def declaration(digest: str) -> CockpitDeclaration:
    return CockpitDeclaration(
        key="palworld",
        name="Palworld",
        cockpit_api_version=COCKPIT_API_VERSION,
        asset_key="cockpit",
        frontend_revision=f"sha256:{digest}",
        default_route="overview",
        route_keys=("overview",),
    )


class CockpitTests(unittest.TestCase):
    def test_save_bundle_transfer_is_exact_and_one_shot(self) -> None:
        hass = FakeHass()
        coordinator = FakeCoordinator(declaration("a" * 64))
        lease = issue_cockpit_lease(
            hass,
            user_id="admin",
            coordinator=coordinator,
            service_id="123",
            declaration=declaration("a" * 64),
            mount_epoch="mount-1",
        )
        grant = issue_save_bundle_transfer_grant(
            hass,
            lease=lease,
            bundle_key="world",
            action="download",
        )
        self.assertIs(
            consume_save_bundle_transfer_grant(
                hass,
                token=grant.token,
                account_entry_id=lease.account_entry_id,
                service_id=lease.service_id,
                bundle_key="world",
                action="download",
            ),
            grant,
        )
        with self.assertRaises(CockpitLeaseError):
            consume_save_bundle_transfer_grant(
                hass,
                token=grant.token,
                account_entry_id=lease.account_entry_id,
                service_id=lease.service_id,
                bundle_key="world",
                action="download",
            )

    def test_save_bundle_preview_is_exact_one_shot_and_closes_private_content(self) -> None:
        hass = FakeHass()
        coordinator = FakeCoordinator(declaration("a" * 64))
        lease = issue_cockpit_lease(
            hass,
            user_id="admin",
            coordinator=coordinator,
            service_id="123",
            declaration=declaration("a" * 64),
            mount_epoch="mount-1",
        )
        preview = SimpleNamespace(key="world", target_root="game/save", closed=False)
        preview.close = lambda: setattr(preview, "closed", True)
        grant = issue_save_bundle_preview_grant(
            hass,
            lease=lease,
            preview=preview,
            expected_current_digest="1" * 64,
            proposed_digest="2" * 64,
        )
        consumed = consume_save_bundle_preview_grant(
            hass,
            token=grant.token,
            lease=lease,
            bundle_key="world",
        )
        self.assertIs(consumed, grant)
        self.assertFalse(preview.closed)
        consumed.preview.close()
        self.assertTrue(preview.closed)
        with self.assertRaises(CockpitLeaseError):
            consume_save_bundle_preview_grant(
                hass,
                token=grant.token,
                lease=lease,
                bundle_key="world",
            )

        expiring = SimpleNamespace(key="world", target_root="game/save", closed=False)
        expiring.close = lambda: setattr(expiring, "closed", True)
        issue_save_bundle_preview_grant(
            hass,
            lease=lease,
            preview=expiring,
            expected_current_digest="1" * 64,
            proposed_digest="2" * 64,
        )
        publish_cockpit_epoch(hass, "profile_replaced")
        self.assertTrue(expiring.closed)

    def test_new_save_bundle_review_replaces_private_content_for_same_lease(self) -> None:
        hass = FakeHass()
        coordinator = FakeCoordinator(declaration("a" * 64))
        lease = issue_cockpit_lease(
            hass,
            user_id="admin",
            coordinator=coordinator,
            service_id="123",
            declaration=declaration("a" * 64),
            mount_epoch="mount-1",
        )
        first = SimpleNamespace(key="world", target_root="game/save", closed=False)
        first.close = lambda: setattr(first, "closed", True)
        first_grant = issue_save_bundle_preview_grant(
            hass,
            lease=lease,
            preview=first,
            expected_current_digest="1" * 64,
            proposed_digest="2" * 64,
        )
        second = SimpleNamespace(key="world", target_root="game/save", closed=False)
        second.close = lambda: setattr(second, "closed", True)
        second_grant = issue_save_bundle_preview_grant(
            hass,
            lease=lease,
            preview=second,
            expected_current_digest="3" * 64,
            proposed_digest="4" * 64,
        )

        self.assertTrue(first.closed)
        with self.assertRaises(CockpitLeaseError):
            consume_save_bundle_preview_grant(
                hass,
                token=first_grant.token,
                lease=lease,
                bundle_key="world",
            )
        self.assertIs(
            consume_save_bundle_preview_grant(
                hass,
                token=second_grant.token,
                lease=lease,
                bundle_key="world",
            ).preview,
            second,
        )
        second.close()

    def test_editable_preview_grant_is_exact_and_one_shot(self) -> None:
        hass = FakeHass()
        coordinator = FakeCoordinator(declaration("a" * 64))
        lease = issue_cockpit_lease(
            hass,
            user_id="admin",
            coordinator=coordinator,
            service_id="123",
            declaration=declaration("a" * 64),
            mount_epoch="mount-1",
        )
        grant = issue_editable_preview_grant(
            hass,
            lease=lease,
            file_key="settings",
            declared_path="/game/PalWorldSettings.ini",
            source_revision="1" * 64,
            proposed_revision="2" * 64,
        )

        with self.assertRaises(CockpitLeaseError):
            consume_editable_preview_grant(
                hass,
                token=grant.token,
                lease=lease,
                file_key="settings",
                declared_path="/game/PalWorldSettings.ini",
                proposed_revision="3" * 64,
            )
        with self.assertRaises(CockpitLeaseError):
            consume_editable_preview_grant(
                hass,
                token=grant.token,
                lease=lease,
                file_key="settings",
                declared_path="/game/PalWorldSettings.ini",
                proposed_revision="2" * 64,
            )
        path_bound = issue_editable_preview_grant(
            hass,
            lease=lease,
            file_key="settings",
            declared_path="/game/PalWorldSettings.ini",
            source_revision="1" * 64,
            proposed_revision="2" * 64,
        )
        with self.assertRaises(CockpitLeaseError):
            consume_editable_preview_grant(
                hass,
                token=path_bound.token,
                lease=lease,
                file_key="settings",
                declared_path="/different/PalWorldSettings.ini",
                proposed_revision="2" * 64,
            )
        second = issue_editable_preview_grant(
            hass,
            lease=lease,
            file_key="settings",
            declared_path="/game/PalWorldSettings.ini",
            source_revision="1" * 64,
            proposed_revision="2" * 64,
        )
        self.assertEqual(
            consume_editable_preview_grant(
                hass,
                token=second.token,
                lease=lease,
                file_key="settings",
                declared_path="/game/PalWorldSettings.ini",
                proposed_revision="2" * 64,
            ),
            second,
        )

    def test_asset_collision_preflight_is_nonmutating(self) -> None:
        hass = FakeHass()
        original = CockpitAssetRecord(
            owner_domain="owner",
            profile_id="profile",
            asset_key="cockpit",
            digest="a" * 64,
            content=b"original",
        )
        collision = CockpitAssetRecord(
            owner_domain="owner",
            profile_id="profile",
            asset_key="cockpit",
            digest="a" * 64,
            content=b"different",
        )
        register_cockpit_assets(hass, (original,))

        with self.assertRaisesRegex(CockpitAssetError, "collision"):
            ensure_cockpit_assets_registerable(hass, (collision,))

        state = cockpit_state(hass)
        self.assertEqual(state.assets, {original.identity: original})
        self.assertEqual(state.profile_assets, {"profile": original.identity})

    def test_owner_bound_asset_is_exact_immutable_bytes(self) -> None:
        async def run() -> None:
            hass = FakeHass()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                content = b"export const cockpitMetadata = {};\n"
                (root / "cockpit.js").write_bytes(content)
                digest = hashlib.sha256(content).hexdigest()
                records = await async_prepare_cockpit_assets(
                    hass,
                    owner_domain="nitrado_gameserver",
                    profile_id="palworld",
                    declaration=declaration(digest),
                    assets={"cockpit": LocalCockpitAsset("cockpit.js", digest)},
                    owner_root=root,
                )
                register_cockpit_assets(hass, records)
                record = cockpit_asset_by_identity(hass, "nitrado_gameserver", "cockpit", digest)
                self.assertIsNotNone(record)
                self.assertEqual(record.content, content)
                self.assertTrue(record.url.endswith(f"/{digest}.js"))
                unregister_cockpit_assets(hass, records)
                self.assertIsNone(cockpit_asset_by_identity(hass, "nitrado_gameserver", "cockpit", digest))

        asyncio.run(run())

    def test_asset_rejects_digest_mismatch_and_escape(self) -> None:
        async def run() -> None:
            hass = FakeHass()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "cockpit.js").write_text("export {};", encoding="utf-8")
                fake_digest = "0" * 64
                with self.assertRaises(CockpitAssetError):
                    await async_prepare_cockpit_assets(
                        hass,
                        owner_domain="owner",
                        profile_id="profile",
                        declaration=declaration(fake_digest),
                        assets={"cockpit": LocalCockpitAsset("cockpit.js", fake_digest)},
                        owner_root=root,
                    )
                with self.assertRaises(CockpitAssetError):
                    await async_prepare_cockpit_assets(
                        hass,
                        owner_domain="owner",
                        profile_id="profile",
                        declaration=declaration(fake_digest),
                        assets={"cockpit": LocalCockpitAsset("../cockpit.js", fake_digest)},
                        owner_root=root,
                    )

        asyncio.run(run())

    def test_broken_builtin_asset_degrades_without_aborting_setup(self) -> None:
        class BrokenAssetProfile(GenericProfile):
            profile_id = "broken_asset"

            def cockpit(self):
                return CockpitDeclaration(
                    key="broken",
                    name="Broken",
                    cockpit_api_version=COCKPIT_API_VERSION,
                    asset_key="cockpit",
                    frontend_revision=f"sha256:{'a' * 64}",
                    default_route="overview",
                    route_keys=("overview",),
                )

            def cockpit_assets(self):
                return {"cockpit": LocalCockpitAsset("missing.js", "a" * 64)}

        async def run() -> None:
            hass = FakeHass()
            with tempfile.TemporaryDirectory() as directory:
                await async_register_builtin_cockpit_assets(
                    hass,
                    profiles=(BrokenAssetProfile(),),
                    owner_root=Path(directory),
                )
            state = cockpit_state(hass)
            self.assertEqual(state.profile_assets, {})
            self.assertEqual(state.asset_failures, {"broken_asset": "FileNotFoundError"})

        asyncio.run(run())

    def test_builtin_asset_reconciliation_clears_stale_no_cockpit_failure(self) -> None:
        async def run() -> None:
            hass = FakeHass()
            state = cockpit_state(hass)
            state.asset_failures = {"generic": "OldAssetError"}
            with (
                tempfile.TemporaryDirectory() as directory,
                patch("custom_components.nitrado_gameserver.cockpit._update_cockpit_asset_issue") as update,
            ):
                await async_register_builtin_cockpit_assets(
                    hass,
                    profiles=(GenericProfile(),),
                    owner_root=Path(directory),
                )
            self.assertEqual(state.asset_failures, {})
            update.assert_called_once_with(hass, "generic", None)

        asyncio.run(run())

    def test_lease_binds_user_account_service_profile_and_epoch(self) -> None:
        hass = FakeHass()
        digest = "a" * 64
        coordinator = FakeCoordinator(declaration(digest))
        lease = issue_cockpit_lease(
            hass,
            user_id="admin-a",
            coordinator=coordinator,
            service_id="123",
            declaration=declaration(digest),
            mount_epoch="mount-a",
        )
        self.assertIs(
            require_cockpit_lease(
                hass,
                token=lease.token,
                user_id="admin-a",
                account_entry_id="entry-a",
                service_id="123",
                coordinator=coordinator,
            ),
            lease,
        )
        with self.assertRaises(CockpitLeaseError):
            require_cockpit_lease(
                hass,
                token=lease.token,
                user_id="admin-b",
                account_entry_id="entry-a",
                service_id="123",
                coordinator=coordinator,
            )
        publish_cockpit_epoch(hass, "profile_replaced")
        with self.assertRaises(CockpitLeaseError):
            require_cockpit_lease(
                hass,
                token=lease.token,
                user_id="admin-a",
                account_entry_id="entry-a",
                service_id="123",
                coordinator=coordinator,
            )
        self.assertEqual(cockpit_state(hass).registry_epoch, 1)
        self.assertEqual(len(hass.bus.events), 1)

    def test_account_epoch_revokes_only_the_selected_account(self) -> None:
        hass = FakeHass()
        digest = "a" * 64
        coordinator_a = FakeCoordinator(declaration(digest), "entry-a")
        coordinator_b = FakeCoordinator(declaration(digest), "entry-b")
        lease_a = issue_cockpit_lease(
            hass,
            user_id="admin",
            coordinator=coordinator_a,
            service_id="123",
            declaration=declaration(digest),
            mount_epoch="mount-a",
        )
        lease_b = issue_cockpit_lease(
            hass,
            user_id="admin",
            coordinator=coordinator_b,
            service_id="123",
            declaration=declaration(digest),
            mount_epoch="mount-b",
        )

        publish_cockpit_account_epoch(hass, "entry-b", "account_reloaded")

        self.assertIs(
            require_cockpit_lease(
                hass,
                token=lease_a.token,
                user_id="admin",
                account_entry_id="entry-a",
                service_id="123",
                coordinator=coordinator_a,
            ),
            lease_a,
        )
        with self.assertRaises(CockpitLeaseError):
            require_cockpit_lease(
                hass,
                token=lease_b.token,
                user_id="admin",
                account_entry_id="entry-b",
                service_id="123",
                coordinator=coordinator_b,
            )
        self.assertEqual(cockpit_state(hass).registry_epoch, 0)

    def test_handoff_is_user_bound_single_use_without_wrong_user_dos(self) -> None:
        hass = FakeHass()
        digest = "b" * 64
        coordinator = FakeCoordinator(declaration(digest))
        lease = issue_cockpit_lease(
            hass,
            user_id="admin-a",
            coordinator=coordinator,
            service_id="123",
            declaration=declaration(digest),
            mount_epoch="mount-a",
        )
        path = issue_cockpit_handoff(hass, lease)
        nonce = path.rsplit("/", 1)[-1]
        with self.assertRaises(CockpitLeaseError):
            consume_cockpit_handoff(hass, nonce=nonce, user_id="admin-b")
        grant = consume_cockpit_handoff(hass, nonce=nonce, user_id="admin-a")
        self.assertEqual((grant.account_entry_id, grant.service_id), ("entry-a", "123"))
        with self.assertRaises(CockpitLeaseError):
            consume_cockpit_handoff(hass, nonce=nonce, user_id="admin-a")


if __name__ == "__main__":
    unittest.main()
