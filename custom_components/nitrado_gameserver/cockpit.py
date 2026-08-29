"""Profile-owned cockpit assets, leases, and registry epochs."""

from __future__ import annotations

import hashlib
import inspect
import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .const import DOMAIN
from .plugins.base import (
    SHA256_PATTERN,
    CockpitDeclaration,
    LocalCockpitAsset,
    profile_extension_manifest,
)
from .profile_logging import log_profile_failure

_LOGGER = logging.getLogger(__name__)

MAX_COCKPIT_ASSET_BYTES = 2 * 1024 * 1024
COCKPIT_ASSET_PREFIX = f"/api/{DOMAIN}/cockpit-assets"
COCKPIT_EVENT = f"{DOMAIN}_cockpit_epoch"
COCKPIT_LEASE_SECONDS = 45 * 60
EDITABLE_PREVIEW_SECONDS = 5 * 60
SAVE_BUNDLE_PREVIEW_SECONDS = 10 * 60
MAX_SAVE_BUNDLE_PREVIEWS = 8
SAVE_BUNDLE_TRANSFER_SECONDS = 60

_DATA_KEY = f"{DOMAIN}_cockpit_state"


def _cockpit_asset_issue_id(profile_id: str) -> str:
    return f"cockpit_asset_{hashlib.sha256(str(profile_id).encode()).hexdigest()[:16]}"


def _update_cockpit_asset_issue(hass: Any, profile_id: str, error_type: str | None) -> None:
    """Expose bundled cockpit degradation through Home Assistant Repairs."""

    try:
        from homeassistant.helpers import issue_registry as ir
    except ModuleNotFoundError:  # pragma: no cover - pure unit environment.
        return
    issue_id = _cockpit_asset_issue_id(profile_id)
    if error_type is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="cockpit_asset_unavailable",
        translation_placeholders={"profile_id": str(profile_id), "error_type": str(error_type)},
    )


class CockpitAssetError(ValueError):
    """Raised when a cockpit asset cannot be owner-bound safely."""


class CockpitLeaseError(PermissionError):
    """Raised when a cockpit capability lease is absent, stale, or expired."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True, frozen=True)
class CockpitAssetRecord:
    """One immutable, digest-addressed JavaScript module."""

    owner_domain: str
    profile_id: str
    asset_key: str
    digest: str
    content: bytes

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.owner_domain, self.asset_key, self.digest)

    @property
    def url(self) -> str:
        return f"{COCKPIT_ASSET_PREFIX}/{self.owner_domain}/{self.asset_key}/{self.digest}.js"


@dataclass(slots=True, frozen=True)
class CockpitLease:
    """Server-authoritative authorization for one mounted cockpit context."""

    token: str
    user_id: str
    account_entry_id: str
    service_id: str
    profile_id: str
    profile_dispatch_token: tuple[int, str, int, int, int]
    cockpit_key: str
    frontend_revision: str
    mount_epoch: str
    registry_epoch: int
    account_epoch: int
    expires_at: float


@dataclass(slots=True, frozen=True)
class CockpitHandoff:
    nonce: str
    user_id: str
    account_entry_id: str
    service_id: str
    expires_at: float


@dataclass(slots=True, frozen=True)
class EditablePreviewGrant:
    """One-shot authorization bound to an exact administrator preview."""

    token: str
    lease_token: str
    user_id: str
    account_entry_id: str
    service_id: str
    file_key: str
    declared_path: str
    source_revision: str
    proposed_revision: str
    expires_at: float


@dataclass(slots=True, frozen=True)
class SaveBundlePreviewGrant:
    """One-shot lease-bound save-tree review with private spooled content."""

    token: str
    lease_token: str
    user_id: str
    account_entry_id: str
    service_id: str
    bundle_key: str
    target_root: str
    expected_current_digest: str
    proposed_digest: str
    preview: Any
    expires_at: float


@dataclass(slots=True, frozen=True)
class SaveBundleTransferGrant:
    """One-shot authorization for one binary save-bundle HTTP transfer."""

    token: str
    lease_token: str
    user_id: str
    account_entry_id: str
    service_id: str
    bundle_key: str
    action: str
    expires_at: float


@dataclass(slots=True)
class _CockpitState:
    assets: dict[tuple[str, str, str], CockpitAssetRecord]
    profile_assets: dict[str, tuple[str, str, str]]
    leases: dict[str, CockpitLease]
    handoffs: dict[str, CockpitHandoff]
    editable_previews: dict[str, EditablePreviewGrant]
    save_bundle_previews: dict[str, SaveBundlePreviewGrant]
    save_bundle_transfers: dict[str, SaveBundleTransferGrant]
    registry_epoch: int = 0
    account_epochs: dict[str, int] | None = None
    asset_failures: dict[str, str] | None = None


def cockpit_state(hass: Any) -> _CockpitState:
    """Return the per-Home-Assistant cockpit registry."""

    state = hass.data.get(_DATA_KEY)
    if isinstance(state, _CockpitState):
        return state
    state = _CockpitState(
        assets={},
        profile_assets={},
        leases={},
        handoffs={},
        editable_previews={},
        save_bundle_previews={},
        save_bundle_transfers={},
        account_epochs={},
        asset_failures={},
    )
    hass.data[_DATA_KEY] = state
    return state


def _asset_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_resource_path(owner_root: Path, resource: str) -> Path:
    if not isinstance(resource, str) or not resource or "\\" in resource:
        raise CockpitAssetError("cockpit package resource is invalid")
    relative = PurePosixPath(resource)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise CockpitAssetError("cockpit package resource must be a normalized relative path")
    root = owner_root.resolve(strict=True)
    candidate = owner_root.joinpath(*relative.parts)
    current = owner_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CockpitAssetError("cockpit package resource cannot traverse a symlink")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as err:
        raise CockpitAssetError("cockpit package resource escaped its owner package") from err
    if not resolved.is_file() or resolved.suffix != ".js":
        raise CockpitAssetError("cockpit package resource must be a regular JavaScript file")
    return resolved


def _export_module_path(export: Any) -> Path | None:
    value = export if inspect.isclass(export) or inspect.isfunction(export) else export.__class__
    module = inspect.getmodule(value)
    filename = getattr(module, "__file__", None)
    return None if not isinstance(filename, str) else Path(filename).resolve(strict=True)


async def async_owner_package_root(hass: Any, owner_domain: str, export: Any) -> Path:
    """Resolve and prove the HA integration package that owns an external profile."""

    if not isinstance(owner_domain, str) or not owner_domain:
        raise CockpitAssetError("cockpit owner domain is required")
    from homeassistant import loader

    integration = await loader.async_get_integration(hass, owner_domain)
    root_value = getattr(integration, "file_path", None)
    if root_value is None:
        raise CockpitAssetError("cockpit owner integration has no package path")
    root = Path(root_value).resolve(strict=True)
    module_path = _export_module_path(export)
    if module_path is None:
        raise CockpitAssetError("cockpit profile export has no importable owner module")
    try:
        module_path.relative_to(root)
    except ValueError as err:
        raise CockpitAssetError("cockpit profile export does not belong to owner domain") from err
    return root


async def async_prepare_cockpit_assets(
    hass: Any,
    *,
    owner_domain: str,
    profile_id: str,
    declaration: CockpitDeclaration | None,
    assets: Mapping[str, LocalCockpitAsset] | None,
    owner_root: Path | None = None,
    export: Any | None = None,
) -> tuple[CockpitAssetRecord, ...]:
    """Validate owner-bound immutable bytes without mutating the registry."""

    if declaration is None:
        if assets:
            raise CockpitAssetError("cockpit assets were supplied without a cockpit declaration")
        return ()
    if not isinstance(assets, Mapping) or set(assets) != {declaration.asset_key}:
        raise CockpitAssetError("cockpit declaration must have exactly one matching asset")
    if owner_root is None:
        if export is None:
            raise CockpitAssetError("external cockpit validation requires its profile export")
        owner_root = await async_owner_package_root(hass, owner_domain, export)
    records: list[CockpitAssetRecord] = []
    for asset_key, asset in assets.items():
        if not isinstance(asset, LocalCockpitAsset):
            raise CockpitAssetError("cockpit asset declaration is invalid")
        expected = asset.sha256.removeprefix("sha256:")
        if SHA256_PATTERN.fullmatch(expected) is None:
            raise CockpitAssetError("cockpit asset digest is invalid")
        path = _safe_resource_path(owner_root, asset.package_resource)
        content = await hass.async_add_executor_job(path.read_bytes)
        if not content or len(content) > MAX_COCKPIT_ASSET_BYTES:
            raise CockpitAssetError("cockpit asset size is outside the allowed range")
        actual = _asset_digest(content)
        if not secrets.compare_digest(actual, expected):
            raise CockpitAssetError("cockpit asset digest does not match its package resource")
        if declaration.frontend_revision != f"sha256:{actual}":
            raise CockpitAssetError("cockpit frontend revision does not match its immutable asset")
        records.append(
            CockpitAssetRecord(
                owner_domain=owner_domain,
                profile_id=profile_id,
                asset_key=asset_key,
                digest=actual,
                content=bytes(content),
            )
        )
    return tuple(records)


def ensure_cockpit_assets_registerable(hass: Any, records: tuple[CockpitAssetRecord, ...]) -> None:
    """Prove an asset commit cannot overwrite another owner binding."""

    state = cockpit_state(hass)
    for record in records:
        existing = state.assets.get(record.identity)
        if existing is not None and existing != record:
            raise CockpitAssetError("cockpit asset identity collision")
        bound = state.profile_assets.get(record.profile_id)
        if bound is not None and bound != record.identity:
            raise CockpitAssetError("profile already owns another cockpit asset revision")


def register_cockpit_assets(hass: Any, records: tuple[CockpitAssetRecord, ...]) -> None:
    """Commit assets only after the same-turn collision preflight succeeds."""

    ensure_cockpit_assets_registerable(hass, records)
    state = cockpit_state(hass)
    for record in records:
        state.assets[record.identity] = record
        state.profile_assets[record.profile_id] = record.identity


def unregister_cockpit_assets(hass: Any, records: tuple[CockpitAssetRecord, ...]) -> None:
    """Remove only the exact asset records owned by one registration handle."""

    state = cockpit_state(hass)
    for record in records:
        if state.assets.get(record.identity) == record:
            state.assets.pop(record.identity, None)
        if state.profile_assets.get(record.profile_id) == record.identity:
            state.profile_assets.pop(record.profile_id, None)


def cockpit_asset_for_profile(hass: Any, owner_domain: str, profile_id: str) -> CockpitAssetRecord | None:
    state = cockpit_state(hass)
    identity = state.profile_assets.get(profile_id)
    record = None if identity is None else state.assets.get(identity)
    return record if record is not None and record.owner_domain == owner_domain else None


def cockpit_asset_for_profile_id(hass: Any, profile_id: str) -> CockpitAssetRecord | None:
    """Return the one globally selected profile's owner-bound asset."""

    state = cockpit_state(hass)
    identity = state.profile_assets.get(str(profile_id))
    return None if identity is None else state.assets.get(identity)


async def async_register_builtin_cockpit_assets(
    hass: Any,
    *,
    profiles: tuple[Any, ...],
    owner_root: Path,
) -> None:
    """Validate and bind bundled cockpit assets once per HA instance."""

    state = cockpit_state(hass)
    for profile in profiles:
        profile_id = str(getattr(profile, "profile_id", "unknown"))
        try:
            manifest = profile_extension_manifest(profile)
            declaration = manifest.cockpit
            if declaration is None or manifest.profile_id in state.profile_assets:
                if state.asset_failures is not None:
                    state.asset_failures.pop(profile_id, None)
                _update_cockpit_asset_issue(hass, profile_id, None)
                continue
            assets = getattr(profile, "cockpit_assets", lambda: {})()
            records = await async_prepare_cockpit_assets(
                hass,
                owner_domain=DOMAIN,
                profile_id=manifest.profile_id,
                declaration=declaration,
                assets=assets,
                owner_root=owner_root,
            )
            register_cockpit_assets(hass, records)
        except Exception as err:  # noqa: BLE001 - degrade arbitrary bundled profile failures.
            failures = state.asset_failures if state.asset_failures is not None else {}
            state.asset_failures = failures
            failures[profile_id] = err.__class__.__name__
            log_profile_failure(_LOGGER, "cockpit_asset", err, profile_id=profile_id)
            _update_cockpit_asset_issue(hass, profile_id, err.__class__.__name__)
            continue
        if state.asset_failures is not None:
            state.asset_failures.pop(profile_id, None)
        _update_cockpit_asset_issue(hass, profile_id, None)


def cockpit_asset_by_identity(
    hass: Any,
    owner_domain: str,
    asset_key: str,
    digest: str,
) -> CockpitAssetRecord | None:
    return cockpit_state(hass).assets.get((owner_domain, asset_key, digest))


def issue_cockpit_lease(
    hass: Any,
    *,
    user_id: str,
    coordinator: Any,
    service_id: str,
    declaration: CockpitDeclaration | None,
    mount_epoch: str,
) -> CockpitLease:
    """Mint one short-lived lease after exact composite service lookup."""

    state = cockpit_state(hass)
    runtime = coordinator.get_runtime(service_id)
    token = secrets.token_urlsafe(32)
    lease = CockpitLease(
        token=token,
        user_id=user_id,
        account_entry_id=str(coordinator.account_entry_id),
        service_id=str(service_id),
        profile_id=str(runtime.profile.profile_id),
        profile_dispatch_token=coordinator.profile_dispatch_token(service_id),
        cockpit_key=declaration.key if declaration is not None else "generic",
        frontend_revision=declaration.frontend_revision if declaration is not None else "core",
        mount_epoch=str(mount_epoch),
        registry_epoch=state.registry_epoch,
        account_epoch=(state.account_epochs or {}).get(str(coordinator.account_entry_id), 0),
        expires_at=time.monotonic() + COCKPIT_LEASE_SECONDS,
    )
    state.leases[token] = lease
    _prune_leases(state)
    return lease


def require_cockpit_lease(
    hass: Any,
    *,
    token: str,
    user_id: str,
    account_entry_id: str,
    service_id: str,
    coordinator: Any,
) -> CockpitLease:
    """Validate identity, expiry, epoch, and live profile generation."""

    state = cockpit_state(hass)
    lease = state.leases.get(str(token))
    if lease is None:
        raise CockpitLeaseError("stale_context", "The cockpit context is no longer active.")
    if lease.expires_at <= time.monotonic():
        state.leases.pop(lease.token, None)
        raise CockpitLeaseError("stale_context", "The cockpit context expired; reload it.")
    expected = (
        str(user_id),
        str(account_entry_id),
        str(service_id),
        state.registry_epoch,
        (state.account_epochs or {}).get(str(account_entry_id), 0),
    )
    actual = (
        lease.user_id,
        lease.account_entry_id,
        lease.service_id,
        lease.registry_epoch,
        lease.account_epoch,
    )
    if actual != expected:
        raise CockpitLeaseError("stale_context", "The cockpit context belongs to another service or user.")
    try:
        runtime = coordinator.require_profile_dispatch_token(service_id, lease.profile_dispatch_token)
    except Exception as err:
        raise CockpitLeaseError("stale_context", "The selected game profile changed; reload the cockpit.") from err
    manifest = getattr(runtime, "profile_manifest", None)
    declaration = getattr(manifest, "cockpit", None)
    revision_matches = lease.cockpit_key == "generic" or (
        declaration is not None
        and declaration.key == lease.cockpit_key
        and declaration.frontend_revision == lease.frontend_revision
    )
    if not revision_matches:
        raise CockpitLeaseError("stale_context", "The cockpit revision changed; reload it.")
    return lease


def revoke_cockpit_leases(
    hass: Any,
    *,
    account_entry_id: str | None = None,
    service_id: str | None = None,
) -> None:
    state = cockpit_state(hass)

    def matches(value: Any) -> bool:
        return (account_entry_id is None or value.account_entry_id == str(account_entry_id)) and (
            service_id is None or value.service_id == str(service_id)
        )

    revoked = {token for token, lease in state.leases.items() if matches(lease)}
    state.leases = {token: lease for token, lease in state.leases.items() if token not in revoked}
    state.handoffs = {token: grant for token, grant in state.handoffs.items() if not matches(grant)}
    state.editable_previews = {token: grant for token, grant in state.editable_previews.items() if not matches(grant)}
    for token, grant in tuple(state.save_bundle_previews.items()):
        if matches(grant):
            state.save_bundle_previews.pop(token, None)
            _close_preview(grant)
    state.save_bundle_transfers = {
        token: grant for token, grant in state.save_bundle_transfers.items() if not matches(grant)
    }
    from .save_bundle_jobs import cancel_save_bundle_jobs

    cancel_save_bundle_jobs(hass, account_entry_id=account_entry_id, service_id=service_id)


def issue_cockpit_handoff(hass: Any, lease: CockpitLease) -> str:
    """Mint a single-use browser redirect without exposing its destination."""

    state = cockpit_state(hass)
    nonce = secrets.token_urlsafe(32)
    state.handoffs[nonce] = CockpitHandoff(
        nonce=nonce,
        user_id=lease.user_id,
        account_entry_id=lease.account_entry_id,
        service_id=lease.service_id,
        expires_at=time.monotonic() + 60,
    )
    if len(state.handoffs) > 128:
        for key, _grant in sorted(state.handoffs.items(), key=lambda item: item[1].expires_at)[:-128]:
            state.handoffs.pop(key, None)
    return f"/api/{DOMAIN}/cockpit-handoff/{nonce}"


def consume_cockpit_handoff(hass: Any, *, nonce: str, user_id: str) -> CockpitHandoff:
    """Consume one exact user-bound redirect grant."""

    state = cockpit_state(hass)
    token = str(nonce)
    grant = state.handoffs.get(token)
    if grant is None or grant.expires_at <= time.monotonic() or grant.user_id != str(user_id):
        raise CockpitLeaseError("handoff_invalid", "This Nitrado handoff is invalid or expired.")
    state.handoffs.pop(token, None)
    return grant


def issue_editable_preview_grant(
    hass: Any,
    *,
    lease: CockpitLease,
    file_key: str,
    declared_path: str,
    source_revision: str,
    proposed_revision: str,
) -> EditablePreviewGrant:
    """Mint a secret-free one-shot grant for one exact valid preview."""

    state = cockpit_state(hass)
    token = secrets.token_urlsafe(32)
    grant = EditablePreviewGrant(
        token=token,
        lease_token=lease.token,
        user_id=lease.user_id,
        account_entry_id=lease.account_entry_id,
        service_id=lease.service_id,
        file_key=str(file_key),
        declared_path=str(declared_path),
        source_revision=str(source_revision),
        proposed_revision=str(proposed_revision),
        expires_at=time.monotonic() + EDITABLE_PREVIEW_SECONDS,
    )
    state.editable_previews[token] = grant
    _prune_editable_previews(state)
    return grant


def consume_editable_preview_grant(
    hass: Any,
    *,
    token: str,
    lease: CockpitLease,
    file_key: str,
    declared_path: str,
    proposed_revision: str,
) -> EditablePreviewGrant:
    """Consume and validate an exact preview grant before any file mutation."""

    state = cockpit_state(hass)
    grant = state.editable_previews.pop(str(token), None)
    expected = (
        lease.token,
        lease.user_id,
        lease.account_entry_id,
        lease.service_id,
        str(file_key),
        str(declared_path),
        str(proposed_revision),
    )
    actual = (
        None
        if grant is None
        else (
            grant.lease_token,
            grant.user_id,
            grant.account_entry_id,
            grant.service_id,
            grant.file_key,
            grant.declared_path,
            grant.proposed_revision,
        )
    )
    if grant is None or grant.expires_at <= time.monotonic() or actual != expected:
        raise CockpitLeaseError("preview_invalid", "The settings preview is stale or does not match this edit.")
    return grant


def issue_save_bundle_preview_grant(
    hass: Any,
    *,
    lease: CockpitLease,
    preview: Any,
    expected_current_digest: str,
    proposed_digest: str,
) -> SaveBundlePreviewGrant:
    """Hold private uploaded bytes behind one exact stopped-server review."""

    state = cockpit_state(hass)
    token = secrets.token_urlsafe(32)
    grant = SaveBundlePreviewGrant(
        token=token,
        lease_token=lease.token,
        user_id=lease.user_id,
        account_entry_id=lease.account_entry_id,
        service_id=lease.service_id,
        bundle_key=str(preview.key),
        target_root=str(preview.target_root),
        expected_current_digest=str(expected_current_digest),
        proposed_digest=str(proposed_digest),
        preview=preview,
        expires_at=time.monotonic() + SAVE_BUNDLE_PREVIEW_SECONDS,
    )
    identity = (lease.token, lease.user_id, lease.account_entry_id, lease.service_id, str(preview.key))
    for previous_token, previous in tuple(state.save_bundle_previews.items()):
        previous_identity = (
            previous.lease_token,
            previous.user_id,
            previous.account_entry_id,
            previous.service_id,
            previous.bundle_key,
        )
        if previous_identity == identity:
            state.save_bundle_previews.pop(previous_token, None)
            _close_preview(previous)
    state.save_bundle_previews[token] = grant
    _prune_save_bundle_previews(state)
    return grant


def issue_save_bundle_transfer_grant(
    hass: Any,
    *,
    lease: CockpitLease,
    bundle_key: str,
    action: str,
) -> SaveBundleTransferGrant:
    """Mint one short-lived, single-use binary-transfer capability."""

    if action not in {"download", "download_ready", "inspect"}:
        raise CockpitLeaseError("transfer_invalid", "The save-bundle transfer action is invalid.")
    state = cockpit_state(hass)
    _prune_save_bundle_transfers(state)
    token = secrets.token_urlsafe(32)
    grant = SaveBundleTransferGrant(
        token=token,
        lease_token=lease.token,
        user_id=lease.user_id,
        account_entry_id=lease.account_entry_id,
        service_id=lease.service_id,
        bundle_key=str(bundle_key),
        action=action,
        expires_at=time.monotonic() + SAVE_BUNDLE_TRANSFER_SECONDS,
    )
    state.save_bundle_transfers[token] = grant
    _prune_save_bundle_transfers(state)
    return grant


def consume_save_bundle_transfer_grant(
    hass: Any,
    *,
    token: str,
    account_entry_id: str,
    service_id: str,
    bundle_key: str,
    action: str,
) -> SaveBundleTransferGrant:
    """Consume one exact opaque binary-transfer capability."""

    state = cockpit_state(hass)
    grant = state.save_bundle_transfers.pop(str(token), None)
    expected = (str(account_entry_id), str(service_id), str(bundle_key), str(action))
    actual = None if grant is None else (grant.account_entry_id, grant.service_id, grant.bundle_key, grant.action)
    if grant is None or grant.expires_at <= time.monotonic() or actual != expected:
        raise CockpitLeaseError(
            "transfer_invalid",
            "The save-bundle transfer authorization is missing, expired, or already used.",
        )
    return grant


def consume_save_bundle_preview_grant(
    hass: Any,
    *,
    token: str,
    lease: CockpitLease,
    bundle_key: str,
) -> SaveBundlePreviewGrant:
    """Consume one exact upload review before entering the mutation path."""

    state = cockpit_state(hass)
    grant = state.save_bundle_previews.pop(str(token), None)
    expected = (lease.token, lease.user_id, lease.account_entry_id, lease.service_id, str(bundle_key))
    actual = (
        None
        if grant is None
        else (
            grant.lease_token,
            grant.user_id,
            grant.account_entry_id,
            grant.service_id,
            grant.bundle_key,
        )
    )
    if grant is None or grant.expires_at <= time.monotonic() or actual != expected:
        if grant is not None:
            _close_preview(grant)
        raise CockpitLeaseError("preview_invalid", "The save-bundle review is stale or belongs to another server.")
    return grant


def publish_cockpit_epoch(hass: Any, reason: str) -> int:
    """Revoke old contexts and announce one committed registry/service change."""

    state = cockpit_state(hass)
    state.registry_epoch += 1
    state.leases.clear()
    state.handoffs.clear()
    state.editable_previews.clear()
    for grant in state.save_bundle_previews.values():
        _close_preview(grant)
    state.save_bundle_previews.clear()
    state.save_bundle_transfers.clear()
    from .save_bundle_jobs import cancel_save_bundle_jobs

    cancel_save_bundle_jobs(hass)
    hass.bus.async_fire(
        COCKPIT_EVENT,
        {"scope": "registry", "epoch": state.registry_epoch, "reason": str(reason)[:64]},
    )
    return state.registry_epoch


def publish_cockpit_account_epoch(hass: Any, account_entry_id: str, reason: str) -> int:
    """Revoke only one account's contexts without changing profile identity."""

    state = cockpit_state(hass)
    account_id = str(account_entry_id)
    epochs = state.account_epochs if state.account_epochs is not None else {}
    state.account_epochs = epochs
    epochs[account_id] = epochs.get(account_id, 0) + 1
    revoke_cockpit_leases(hass, account_entry_id=account_id)
    hass.bus.async_fire(
        COCKPIT_EVENT,
        {
            "scope": "account",
            "account_entry_id": account_id,
            "account_epoch": epochs[account_id],
            "reason": str(reason)[:64],
        },
    )
    return epochs[account_id]


def purge_cockpit_account_state(hass: Any, account_entry_id: str) -> None:
    """Revoke and forget only one removed account's transient cockpit state."""

    state = hass.data.get(_DATA_KEY)
    if not isinstance(state, _CockpitState):
        return
    account_id = str(account_entry_id)
    revoke_cockpit_leases(hass, account_entry_id=account_id)
    if state.account_epochs is not None:
        state.account_epochs.pop(account_id, None)


def _prune_leases(state: _CockpitState) -> None:
    now = time.monotonic()
    state.leases = {token: lease for token, lease in state.leases.items() if lease.expires_at > now}
    if len(state.leases) > 256:
        oldest = sorted(state.leases.values(), key=lambda lease: lease.expires_at)[: len(state.leases) - 256]
        for lease in oldest:
            state.leases.pop(lease.token, None)


def _prune_editable_previews(state: _CockpitState) -> None:
    now = time.monotonic()
    state.editable_previews = {
        token: grant for token, grant in state.editable_previews.items() if grant.expires_at > now
    }
    if len(state.editable_previews) > 256:
        oldest = sorted(state.editable_previews.values(), key=lambda grant: grant.expires_at)[
            : len(state.editable_previews) - 256
        ]
        for grant in oldest:
            state.editable_previews.pop(grant.token, None)


def _prune_save_bundle_previews(state: _CockpitState) -> None:
    now = time.monotonic()
    for token, grant in tuple(state.save_bundle_previews.items()):
        if grant.expires_at <= now:
            state.save_bundle_previews.pop(token, None)
            _close_preview(grant)
    if len(state.save_bundle_previews) > MAX_SAVE_BUNDLE_PREVIEWS:
        oldest = sorted(state.save_bundle_previews.values(), key=lambda grant: grant.expires_at)[
            : len(state.save_bundle_previews) - MAX_SAVE_BUNDLE_PREVIEWS
        ]
        for grant in oldest:
            state.save_bundle_previews.pop(grant.token, None)
            _close_preview(grant)


def _prune_save_bundle_transfers(state: _CockpitState) -> None:
    now = time.monotonic()
    state.save_bundle_transfers = {
        token: grant for token, grant in state.save_bundle_transfers.items() if grant.expires_at > now
    }
    if len(state.save_bundle_transfers) > 128:
        oldest = sorted(state.save_bundle_transfers.values(), key=lambda grant: grant.expires_at)[
            : len(state.save_bundle_transfers) - 128
        ]
        for grant in oldest:
            state.save_bundle_transfers.pop(grant.token, None)


def _close_preview(grant: SaveBundlePreviewGrant) -> None:
    close = getattr(grant.preview, "close", None)
    if callable(close):
        close()


__all__ = (
    "COCKPIT_ASSET_PREFIX",
    "COCKPIT_EVENT",
    "CockpitAssetError",
    "CockpitAssetRecord",
    "CockpitLeaseError",
    "async_prepare_cockpit_assets",
    "async_register_builtin_cockpit_assets",
    "cockpit_asset_by_identity",
    "cockpit_asset_for_profile",
    "cockpit_asset_for_profile_id",
    "cockpit_state",
    "consume_cockpit_handoff",
    "consume_editable_preview_grant",
    "consume_save_bundle_preview_grant",
    "consume_save_bundle_transfer_grant",
    "ensure_cockpit_assets_registerable",
    "issue_cockpit_handoff",
    "issue_cockpit_lease",
    "issue_editable_preview_grant",
    "issue_save_bundle_preview_grant",
    "issue_save_bundle_transfer_grant",
    "publish_cockpit_account_epoch",
    "publish_cockpit_epoch",
    "purge_cockpit_account_state",
    "register_cockpit_assets",
    "require_cockpit_lease",
    "revoke_cockpit_leases",
    "unregister_cockpit_assets",
)
