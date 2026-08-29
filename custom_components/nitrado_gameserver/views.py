"""HTTP views for generic profile extension surfaces."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import fields, is_dataclass, replace
from enum import Enum
from typing import Any

try:
    from aiohttp import web
    from homeassistant.components.http import HomeAssistantView
except ModuleNotFoundError:  # pragma: no cover - local compile checks run without HA installed.
    web = None  # type: ignore[assignment]

    class HomeAssistantView:  # type: ignore[no-redef]
        """Fallback base for local import/compile checks."""

        requires_auth = True

        def json(self, result: Any, **kwargs: Any) -> Any:
            del kwargs
            return result


from .api.nitrado import NitradoApiError
from .cockpit import (
    CockpitLeaseError,
    cockpit_asset_by_identity,
    cockpit_asset_for_profile_id,
    cockpit_state,
    consume_cockpit_handoff,
    consume_editable_preview_grant,
    consume_save_bundle_preview_grant,
    consume_save_bundle_transfer_grant,
    issue_cockpit_handoff,
    issue_cockpit_lease,
    issue_editable_preview_grant,
    issue_save_bundle_preview_grant,
    issue_save_bundle_transfer_grant,
    require_cockpit_lease,
)
from .const import (
    CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    DOMAIN,
    EDITOR_MUTATIONS_ENABLED,
    FILESYSTEM_TREE_REPLACE_ENABLED,
    NATIVE_BACKUP_RESTORE_ENABLED,
)
from .editable_file_journal import EditableFileJournalError
from .extensions import ProfileExtensionError, resource_content_family
from .filesystem import tree_manifest_sha256
from .models import ServiceIdentity
from .naming import service_display_name
from .operation_journal import OperationIntent, OperationJournalError, OperationPhase
from .plugins.base import (
    COCKPIT_API_VERSION,
    PROFILE_HANDLER_TIMEOUT_SECONDS,
    ActionDeclaration,
    CapabilityVerdict,
    CockpitSnapshotContext,
    EditableFileDeclaration,
    EntityDeclaration,
    ExtensionAccess,
    LifecycleHookDeclaration,
    ProfileManifestError,
    ProfileOptionType,
    ResourceDeclaration,
    SaveBundleDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    async_invoke_profile,
    validate_profile_option_value,
)
from .profile_logging import log_profile_failure
from .profile_workers import async_run_profile_sync
from .provider_api import ProviderApiError, ProviderScope
from .provider_runtime import get_provider_runtime, new_provider_grant
from .repairs import async_update_repair_issues
from .runtime import runtime_profile_extension_manifest
from .save_bundle_jobs import (
    discard_save_bundle_job,
    require_save_bundle_job,
    start_save_bundle_job,
    take_save_bundle_job,
)
from .save_bundles import MAX_SAVE_BUNDLE_UPLOAD_BYTES, preview_payload
from .service_options import (
    cleared_profile_option_acknowledgement_options,
    cleared_profile_option_options,
    entry_option_update_lock,
    profile_option_acknowledged,
    profile_option_acknowledgements_map,
    profile_option_value,
    profile_options_map,
    service_id_set,
    updated_idle_toggle_options,
    updated_profile_option_acknowledgement_options,
    updated_profile_option_options,
)

_VIEW_REGISTRATION_KEY = f"{DOMAIN}_extension_views_registered"
_MAX_EXTENSION_PAYLOAD_BYTES = 65_536
_MAX_STREAM_RESOURCE_BYTES = 16_777_216
_LOGGER = logging.getLogger(__name__)


def async_register_extension_views(hass: Any) -> None:
    """Register generic HTTP views for profile-declared extensions."""

    registered = hass.data.get(_VIEW_REGISTRATION_KEY)
    if registered is True:
        return
    if not isinstance(registered, set):
        registered = set()
        hass.data[_VIEW_REGISTRATION_KEY] = registered
    views = (
        ProfileExtensionServicesView(),
        NitradoWebinterfaceLoginView(),
        CockpitAssetView(),
        CockpitBootstrapView(),
        CockpitCapabilityView(),
        CockpitSaveBundleDownloadView(),
        CockpitSaveBundleInspectView(),
        CockpitHandoffView(),
        ProfileExtensionsView(),
        ProfileActionView(),
        ProfileResourceView(),
        ProfileSurfaceResourcesView(),
        ProfileEditableFileReadView(),
        ProfileEditableFilePreviewView(),
        ProfileEditableFileApplyView(),
        ProfileEditableFileRollbackView(),
        ProfileOptionView(),
        FilesystemSettingsView(),
        ProviderGrantCollectionView(),
        ProviderGrantItemView(),
        ProviderMutationApprovalView(),
    )
    for view in views:
        if view.name in registered:
            continue
        hass.http.register_view(view)
        registered.add(view.name)


class ProfileExtensionServicesView(HomeAssistantView):
    """List managed services and extension manifests for the generic panel."""

    url = "/api/nitrado_gameserver/extensions"
    name = "api:nitrado_gameserver:extension_services"
    requires_auth = True

    async def get(self, request: Any) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        hass = request.app["hass"]
        services: list[dict[str, Any]] = []
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if not getattr(coordinator, "setup_committed", True) or not hasattr(coordinator, "services"):
                continue
            display_names = getattr(getattr(coordinator, "options", None), "service_display_names", {})
            account_entry_id = str(getattr(coordinator, "account_entry_id", ""))
            account_title = _account_title(hass, account_entry_id)
            for service_id, runtime in sorted(coordinator.services.items()):
                profile = runtime.profile
                manifest = _manifest_payload(runtime)
                surface_error: str | None = None
                try:
                    surfaces = () if manifest.get("invalid") else coordinator.profile_surface_descriptors(service_id)
                except ProfileExtensionError as err:
                    surfaces = ()
                    surface_error = "Profile surface metadata is unavailable; core server controls remain available."
                    log_profile_failure(_LOGGER, "surface_metadata", err, key=service_id)
                services.append(
                    {
                        "account_entry_id": account_entry_id,
                        "account_title": account_title,
                        "service_id": service_id,
                        "name": service_display_name(runtime, display_names),
                        "profile": {
                            "profile_id": getattr(profile, "profile_id", None),
                            "name": getattr(profile, "name", None),
                        },
                        "manifest": manifest,
                        "core_entities": _core_entity_ids_payload(
                            hass, service_id, str(getattr(coordinator, "account_entry_id", ""))
                        ),
                        **_public_profile_option_values(runtime),
                        "filesystem": _filesystem_status_payload(
                            coordinator,
                            service_id,
                            manifest,
                        ),
                        "surfaces": _surface_descriptors_payload(
                            hass,
                            service_id,
                            str(getattr(coordinator, "account_entry_id", "")),
                            profile,
                            surfaces,
                        ),
                        "surface_error": surface_error,
                    }
                )
        return self.json(_json_safe({"services": services}))


def _account_title(hass: Any, account_entry_id: str) -> str:
    """Return the user-facing config-entry title with a stable ID fallback."""

    config_entries = getattr(hass, "config_entries", None)
    get_entry = getattr(config_entries, "async_get_entry", None)
    entry = get_entry(account_entry_id) if callable(get_entry) and account_entry_id else None
    title = getattr(entry, "title", None)
    return str(title).strip() if isinstance(title, str) and title.strip() else account_entry_id


class NitradoWebinterfaceLoginView(HomeAssistantView):
    """Fail old JSON clients closed after the one-time handoff migration."""

    url = "/api/nitrado_gameserver/services/{service_id}/webinterface-login"
    name = "api:nitrado_gameserver:webinterface_login"
    requires_auth = True

    async def post(self, request: Any, service_id: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        del service_id
        raise _json_error(
            "Reload Nitrado Servers to use the protected one-time web-interface handoff.",
            code="reload_required",
            status=409,
        )


class CockpitAssetView(HomeAssistantView):
    """Serve one exact owner-bound immutable cockpit module."""

    url = "/api/nitrado_gameserver/cockpit-assets/{owner_domain}/{asset_key}/{digest}.js"
    name = "api:nitrado_gameserver:cockpit_asset"
    # Native ``import()`` cannot attach Home Assistant's bearer token. These
    # files are immutable, digest-addressed installed code and contain no
    # service data or secrets; bootstrap and every capability remain admin-only.
    requires_auth = False

    async def get(self, request: Any, owner_domain: str, asset_key: str, digest: str) -> Any:
        record = cockpit_asset_by_identity(request.app["hass"], owner_domain, asset_key, digest)
        if record is None:
            raise web.HTTPNotFound()
        return web.Response(
            body=record.content,
            content_type="text/javascript",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{record.digest}"',
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )


class CockpitBootstrapView(HomeAssistantView):
    """Bootstrap one exact account/service cockpit mount."""

    url = "/api/nitrado_gameserver/accounts/{account_entry_id}/services/{service_id}/cockpit/bootstrap"
    name = "api:nitrado_gameserver:cockpit_bootstrap"
    requires_auth = True

    async def get(self, request: Any, account_entry_id: str, service_id: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        hass = request.app["hass"]
        user = request.get("hass_user")
        coordinator = _find_exact_coordinator(hass, account_entry_id, service_id)
        runtime = coordinator.get_runtime(service_id)
        manifest = runtime_profile_extension_manifest(runtime)
        declaration, degraded_code = _cockpit_compatibility(manifest.cockpit)
        asset = None if declaration is None else cockpit_asset_for_profile_id(hass, str(runtime.profile.profile_id))
        if declaration is not None and (asset is None or declaration.frontend_revision != f"sha256:{asset.digest}"):
            asset = None
            degraded_code = "cockpit_asset_unavailable"
        mount_epoch = str(request.query.get("mount_epoch", ""))
        if not mount_epoch or len(mount_epoch) > 128:
            raise _bad_request("mount_epoch is required")
        lease = issue_cockpit_lease(
            hass,
            user_id=str(getattr(user, "id", "")),
            coordinator=coordinator,
            service_id=service_id,
            declaration=declaration if asset is not None else None,
            mount_epoch=mount_epoch,
        )
        snapshot = await _cockpit_snapshot(
            hass,
            coordinator,
            runtime,
            presenter_enabled=declaration is not None and asset is not None,
        )
        snapshot["operations"] = await _operation_facts_payload(coordinator, service_id)
        return self.json(
            _json_safe(
                {
                    "cockpit_api_version": COCKPIT_API_VERSION,
                    "registry_epoch": cockpit_state(hass).registry_epoch,
                    "account_epoch": (cockpit_state(hass).account_epochs or {}).get(str(account_entry_id), 0),
                    "lease": lease.token,
                    "lease_expires_in": 900,
                    "mount_epoch": mount_epoch,
                    "degraded_code": degraded_code,
                    "cockpit": (
                        None
                        if declaration is None or asset is None
                        else {
                            "key": declaration.key,
                            "name": declaration.name,
                            "api_version": declaration.cockpit_api_version,
                            "frontend_revision": declaration.frontend_revision,
                            "default_route": declaration.default_route,
                            "route_keys": declaration.route_keys,
                            "route_aliases": declaration.route_aliases,
                            "asset_url": asset.url,
                        }
                    ),
                    "snapshot": snapshot,
                }
            ),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class CockpitCapabilityView(HomeAssistantView):
    """Dispatch host capabilities through a captured service/profile lease."""

    url = "/api/nitrado_gameserver/accounts/{account_entry_id}/services/{service_id}/cockpit/capabilities/{capability}"
    name = "api:nitrado_gameserver:cockpit_capability"
    requires_auth = True

    async def post(self, request: Any, account_entry_id: str, service_id: str, capability: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        hass = request.app["hass"]
        user = request.get("hass_user")
        body = await _json_body(request)
        coordinator = _find_exact_coordinator(hass, account_entry_id, service_id)
        try:
            lease = require_cockpit_lease(
                hass,
                token=str(body.get("lease", "")),
                user_id=str(getattr(user, "id", "")),
                account_entry_id=account_entry_id,
                service_id=service_id,
                coordinator=coordinator,
            )
        except CockpitLeaseError as err:
            raise _json_error(str(err), code=err.code, status=409) from err

        if capability == "refresh":
            await coordinator.async_refresh_service(service_id)
            result: dict[str, Any] = {"code": "refresh_complete"}
        elif capability == "start":
            await coordinator.async_start_service(service_id)
            result = {"code": "start_accepted"}
        elif capability == "stop":
            await coordinator.async_stop_service(service_id)
            result = {"code": "stop_accepted"}
        elif capability == "open-nitrado":
            result = {"code": "handoff_ready", "path": issue_cockpit_handoff(hass, lease)}
        elif capability == "profile-option":
            option_key = _required_short_string(body, "option_key")
            result = await _async_update_profile_option(
                hass,
                account_entry_id,
                coordinator,
                service_id,
                option_key,
                body,
            )
        elif capability == "editable-read":
            file_key = _required_short_string(body, "file_key")
            try:
                snapshot = await coordinator.async_read_editable_file(
                    service_id,
                    file_key,
                    expected_profile=lease.profile_dispatch_token,
                )
            except ProfileExtensionError as err:
                raise _http_blocked(err) from err
            result = {
                "code": "editable_file_loaded",
                "file": _editable_snapshot_payload(snapshot, include_raw=snapshot.editor_model is None),
            }
        elif capability == "editable-preview":
            file_key = _required_short_string(body, "file_key")
            structured = "operations" in body
            value = _required_edit_operations(body) if structured else _required_edit_value(body, value_is_parsed=False)
            source_revision = _required_short_string(body, "source_revision") if structured else None
            try:
                preview = await coordinator.async_preview_editable_file(
                    service_id,
                    file_key,
                    value,
                    value_is_parsed=structured,
                    expected_source_revision=source_revision,
                    expected_profile=lease.profile_dispatch_token,
                )
            except ProfileExtensionError as err:
                raise _http_blocked(err) from err
            preview_token = None
            if preview.verdict.allowed and preview.changed:
                preview_token = issue_editable_preview_grant(
                    hass,
                    lease=lease,
                    file_key=file_key,
                    declared_path=preview.path,
                    source_revision=preview.source_revision,
                    proposed_revision=preview.proposed_revision,
                ).token
            result = {
                "code": "editable_preview_ready",
                "preview_token": preview_token,
                "preview_expires_in": 300 if preview_token is not None else 0,
                "preview": _editable_preview_payload(preview, include_raw=False),
            }
        elif capability == "editable-history":
            file_key = _required_short_string(body, "file_key")
            try:
                records = await coordinator.async_editable_file_recovery_records(service_id, file_key)
            except EditableFileJournalError as err:
                raise _json_error(str(err), code="editable_history_unavailable", status=409) from err
            history: list[dict[str, Any]] = []
            for record in records[:10]:
                verdict = await coordinator.async_probe_editable_file_backup(
                    service_id,
                    file_key,
                    record.backup_path,
                    expected_profile=lease.profile_dispatch_token,
                )
                history.append(
                    {
                        "recovery_id": record.recovery_id,
                        "backup_kind": record.backup_kind,
                        "created_at": record.created_at,
                        "operation_id": record.operation_id,
                        "available": verdict.allowed,
                        "reason": verdict.reason,
                    }
                )
            result = {"code": "editable_history_ready", "history": history}
        elif capability == "editable-apply":
            if not EDITOR_MUTATIONS_ENABLED:
                raise _bad_request("Editable-file writes are disabled pending controlled live acceptance")
            if body.get("confirm") is not True:
                raise _bad_request("Explicit confirmation is required for editable-file apply")
            file_key = _required_short_string(body, "file_key")
            structured = "operations" in body
            value = _required_edit_operations(body) if structured else _required_edit_value(body, value_is_parsed=False)
            preview_token = _required_short_string(body, "preview_token")
            try:
                source_revision = _required_short_string(body, "source_revision") if structured else None
                resolved = await coordinator.async_preview_editable_file(
                    service_id,
                    file_key,
                    value,
                    value_is_parsed=structured,
                    expected_source_revision=source_revision,
                    expected_profile=lease.profile_dispatch_token,
                )
                value = resolved.proposed_text
                grant = consume_editable_preview_grant(
                    hass,
                    token=preview_token,
                    lease=lease,
                    file_key=file_key,
                    declared_path=resolved.path,
                    proposed_revision=resolved.proposed_revision,
                )
                applied = await coordinator.async_apply_editable_file(
                    service_id,
                    file_key,
                    value,
                    value_is_parsed=False,
                    expected_profile=lease.profile_dispatch_token,
                    expected_source_revision=grant.source_revision,
                    expected_proposed_revision=grant.proposed_revision,
                    expected_path=grant.declared_path,
                )
            except CockpitLeaseError as err:
                raise _json_error(str(err), code=err.code, status=409) from err
            except (ProfileExtensionError, EditableFileJournalError) as err:
                if isinstance(err, ProfileExtensionError):
                    raise _http_blocked(err) from err
                raise _json_error(str(err), code="editable_history_unavailable", status=409) from err
            result = {
                "code": "editable_file_applied",
                "wrote": applied.wrote,
                "recovery_id": applied.recovery_id,
                "operation_id": applied.operation_id,
                "requires_restart": applied.preview.requires_restart and applied.wrote,
            }
        elif capability == "editable-rollback":
            if not EDITOR_MUTATIONS_ENABLED:
                raise _bad_request("Editable-file rollback is disabled pending controlled live acceptance")
            if body.get("confirm") is not True:
                raise _bad_request("Explicit confirmation is required for editable-file rollback")
            file_key = _required_short_string(body, "file_key")
            recovery_id = _required_short_string(body, "recovery_id")
            try:
                recovery = await coordinator.async_require_editable_file_recovery(
                    service_id,
                    file_key,
                    recovery_id,
                )
                verdict = await coordinator.async_probe_editable_file_backup(
                    service_id,
                    file_key,
                    recovery.backup_path,
                    expected_profile=lease.profile_dispatch_token,
                )
                if not verdict.allowed:
                    raise ProfileExtensionError(verdict)
                rolled_back = await coordinator.async_rollback_editable_file(
                    service_id,
                    file_key,
                    recovery.backup_path,
                    expected_profile=lease.profile_dispatch_token,
                )
            except EditableFileJournalError as err:
                raise _json_error(str(err), code="editable_history_unavailable", status=409) from err
            except ProfileExtensionError as err:
                raise _http_blocked(err) from err
            result = {
                "code": "editable_file_rolled_back",
                "wrote": rolled_back.wrote,
                "recovery_id": rolled_back.recovery_id,
                "operation_id": rolled_back.operation_id,
                "requires_restart": rolled_back.wrote,
            }
        elif capability == "save-bundle-transfer":
            bundle_key = _required_short_string(body, "bundle_key")
            action = _required_short_string(body, "action")
            try:
                transfer = issue_save_bundle_transfer_grant(
                    hass,
                    lease=lease,
                    bundle_key=bundle_key,
                    action=action,
                )
            except CockpitLeaseError as err:
                raise _json_error(str(err), code=err.code, status=409) from err
            result = {
                "code": "save_bundle_transfer_ready",
                "transfer_token": transfer.token,
                "expires_in": 60,
            }
        elif capability == "save-bundle-job-status":
            bundle_key = _required_short_string(body, "bundle_key")
            action = _required_short_string(body, "action")
            job_id = _required_short_string(body, "job_id")
            try:
                job = require_save_bundle_job(
                    hass,
                    job_id=job_id,
                    user_id=lease.user_id,
                    account_entry_id=account_entry_id,
                    service_id=service_id,
                    bundle_key=bundle_key,
                    action=action,
                )
            except ValueError as err:
                raise _json_error(str(err), code="save_bundle_job_invalid", status=409) from err
            if job.status == "running":
                result = {
                    "code": "save_bundle_job_running",
                    "status": "running",
                    "progress": job.progress_payload(),
                }
            elif job.status == "error":
                result = {
                    "code": "save_bundle_job_failed",
                    "status": "error",
                    "message": job.error or "Save-bundle preparation failed safely.",
                }
                discard_save_bundle_job(
                    hass,
                    job_id=job_id,
                    user_id=lease.user_id,
                    account_entry_id=account_entry_id,
                    service_id=service_id,
                    bundle_key=bundle_key,
                    action=action,
                )
            elif action == "download":
                transfer = issue_save_bundle_transfer_grant(
                    hass,
                    lease=lease,
                    bundle_key=bundle_key,
                    action="download_ready",
                )
                result = {
                    "code": "save_bundle_job_ready",
                    "status": "ready",
                    "progress": job.progress_payload(),
                    "transfer_token": transfer.token,
                    "expires_in": 60,
                }
            elif action == "inspect":
                preview = take_save_bundle_job(
                    hass,
                    job_id=job_id,
                    user_id=lease.user_id,
                    account_entry_id=account_entry_id,
                    service_id=service_id,
                    bundle_key=bundle_key,
                    action=action,
                )
                preview_token = None
                try:
                    if preview.changed:
                        grant = issue_save_bundle_preview_grant(
                            hass,
                            lease=lease,
                            preview=preview,
                            expected_current_digest=tree_manifest_sha256(preview.current),
                            proposed_digest=tree_manifest_sha256(preview.proposed),
                        )
                        preview_token = grant.token
                    result = {
                        "code": "save_bundle_review_ready",
                        "status": "ready",
                        "progress": job.progress_payload(),
                        "preview_token": preview_token,
                        "preview_expires_in": 600 if preview_token is not None else 0,
                        "restore_enabled": FILESYSTEM_TREE_REPLACE_ENABLED,
                        "preview": preview_payload(preview),
                    }
                    if preview_token is None:
                        preview.close()
                except BaseException:
                    if preview_token is None:
                        preview.close()
                    raise
            else:
                raise _bad_request("Unknown save-bundle job action")
        elif capability == "save-bundle-apply":
            if not FILESYSTEM_TREE_REPLACE_ENABLED:
                raise _bad_request("Save-game restore is disabled pending controlled live acceptance")
            if body.get("confirm") is not True:
                raise _bad_request("Explicit confirmation is required for save-game restore")
            bundle_key = _required_short_string(body, "bundle_key")
            preview_token = _required_short_string(body, "preview_token")
            try:
                grant = consume_save_bundle_preview_grant(
                    hass,
                    token=preview_token,
                    lease=lease,
                    bundle_key=bundle_key,
                )
                try:
                    applied = await coordinator.async_apply_save_bundle(
                        service_id,
                        grant.preview,
                        expected_profile=lease.profile_dispatch_token,
                    )
                finally:
                    grant.preview.close()
            except CockpitLeaseError as err:
                raise _json_error(str(err), code=err.code, status=409) from err
            except ProfileExtensionError as err:
                raise _http_blocked(err) from err
            result = {
                "code": "save_bundle_restored",
                "operation_id": applied.operation_id,
                "transaction_id": applied.transaction_id,
                "manifest_sha256": tree_manifest_sha256(applied.manifest),
                "files": len(applied.manifest.entries),
                "bytes": applied.manifest.total_bytes,
            }
        elif capability == "entity-action":
            result = await _async_cockpit_entity_action(
                hass,
                account_entry_id,
                service_id,
                body,
            )
        elif capability == "acknowledge-operation":
            if body.get("confirm") is not True:
                raise _bad_request("Explicit confirmation is required to acknowledge an unknown outcome")
            operation_id = _required_short_string(body, "operation_id")
            try:
                await coordinator.async_acknowledge_unknown_operation(service_id, operation_id)
            except OperationJournalError as err:
                raise _bad_request(str(err)) from err
            result = {"code": "unknown_outcome_acknowledged"}
        else:
            raise _bad_request("Unknown cockpit capability")
        return self.json(
            _json_safe(result),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class CockpitSaveBundleDownloadView(HomeAssistantView):
    """Stream one stopped-server save bundle without exposing auth to profile code."""

    url = "/api/nitrado_gameserver/accounts/{account_entry_id}/services/{service_id}/cockpit/save-bundles/{bundle_key}/download"
    name = "api:nitrado_gameserver:cockpit_save_bundle_download"
    requires_auth = False

    async def post(self, request: Any, account_entry_id: str, service_id: str, bundle_key: str) -> Any:
        hass = request.app["hass"]
        coordinator = _find_exact_coordinator(hass, account_entry_id, service_id)
        job_id = str(request.headers.get("X-Nitrado-Save-Job", ""))
        transfer_action = "download_ready" if job_id else "download"
        try:
            transfer = consume_save_bundle_transfer_grant(
                hass,
                token=str(request.headers.get("X-Nitrado-Save-Transfer", "")),
                account_entry_id=account_entry_id,
                service_id=service_id,
                bundle_key=bundle_key,
                action=transfer_action,
            )
            lease = require_cockpit_lease(
                hass,
                token=transfer.lease_token,
                user_id=transfer.user_id,
                account_entry_id=account_entry_id,
                service_id=service_id,
                coordinator=coordinator,
            )
        except CockpitLeaseError as err:
            raise _json_error(str(err), code=err.code, status=409) from err
        if not job_id:
            try:
                job = start_save_bundle_job(
                    hass,
                    user_id=lease.user_id,
                    account_entry_id=account_entry_id,
                    service_id=service_id,
                    bundle_key=bundle_key,
                    action="download",
                    operation=lambda progress: coordinator.async_export_save_bundle(
                        service_id,
                        bundle_key,
                        expected_profile=lease.profile_dispatch_token,
                        progress=progress,
                    ),
                )
            except ValueError as err:
                raise _json_error(str(err), code="save_bundle_job_busy", status=409) from err
            return self.json(
                {"code": "save_bundle_job_started", "status": "running", "job_id": job.job_id},
                status_code=202,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        try:
            exported = take_save_bundle_job(
                hass,
                job_id=job_id,
                user_id=lease.user_id,
                account_entry_id=account_entry_id,
                service_id=service_id,
                bundle_key=bundle_key,
                action="download",
            )
        except ValueError as err:
            raise _json_error(str(err), code="save_bundle_job_invalid", status=409) from err
        try:
            exported.stream.seek(0, 2)
            length = exported.stream.tell()
            exported.stream.seek(0)
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(length),
                    "Content-Disposition": f'attachment; filename="{exported.filename}"',
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
            await response.prepare(request)
            while chunk := exported.stream.read(1024 * 1024):
                await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            exported.close()


class CockpitSaveBundleInspectView(HomeAssistantView):
    """Stream, validate, and compare one uploaded save ZIP."""

    url = "/api/nitrado_gameserver/accounts/{account_entry_id}/services/{service_id}/cockpit/save-bundles/{bundle_key}/inspect"
    name = "api:nitrado_gameserver:cockpit_save_bundle_inspect"
    requires_auth = False

    async def post(self, request: Any, account_entry_id: str, service_id: str, bundle_key: str) -> Any:
        hass = request.app["hass"]
        coordinator = _find_exact_coordinator(hass, account_entry_id, service_id)
        try:
            transfer = consume_save_bundle_transfer_grant(
                hass,
                token=str(request.headers.get("X-Nitrado-Save-Transfer", "")),
                account_entry_id=account_entry_id,
                service_id=service_id,
                bundle_key=bundle_key,
                action="inspect",
            )
            lease = require_cockpit_lease(
                hass,
                token=transfer.lease_token,
                user_id=transfer.user_id,
                account_entry_id=account_entry_id,
                service_id=service_id,
                coordinator=coordinator,
            )
        except CockpitLeaseError as err:
            raise _json_error(str(err), code=err.code, status=409) from err
        if request.content_length is not None and request.content_length > MAX_SAVE_BUNDLE_UPLOAD_BYTES:
            raise _json_error("The ZIP exceeds the 256 MiB upload limit.", code="upload_too_large", status=413)
        content_type = str(request.content_type or "").casefold()
        if content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}:
            raise _bad_request("Upload must be a ZIP file")
        stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")  # noqa: SIM115
        total = 0
        try:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                total += len(chunk)
                if total > MAX_SAVE_BUNDLE_UPLOAD_BYTES:
                    raise _json_error("The ZIP exceeds the 256 MiB upload limit.", code="upload_too_large", status=413)
                stream.write(chunk)
            if total == 0:
                raise _bad_request("The uploaded ZIP is empty")
            stream.seek(0)
            # Uploading may take long enough for a profile/service epoch to
            # change. Revalidate the exact lease after the last byte arrives.
            lease = require_cockpit_lease(
                hass,
                token=transfer.lease_token,
                user_id=transfer.user_id,
                account_entry_id=account_entry_id,
                service_id=service_id,
                coordinator=coordinator,
            )

            async def prepare_preview(progress: Any) -> Any:
                try:
                    return await coordinator.async_inspect_save_bundle(
                        service_id,
                        bundle_key,
                        stream,
                        expected_profile=lease.profile_dispatch_token,
                        progress=progress,
                    )
                finally:
                    stream.close()

            job = start_save_bundle_job(
                hass,
                user_id=lease.user_id,
                account_entry_id=account_entry_id,
                service_id=service_id,
                bundle_key=bundle_key,
                action="inspect",
                operation=prepare_preview,
            )
        except CockpitLeaseError as err:
            stream.close()
            raise _json_error(str(err), code=err.code, status=409) from err
        except ValueError as err:
            stream.close()
            raise _json_error(str(err), code="save_bundle_job_busy", status=409) from err
        except BaseException:
            stream.close()
            raise
        return self.json(
            {"code": "save_bundle_job_started", "status": "running", "job_id": job.job_id},
            status_code=202,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )


class CockpitHandoffView(HomeAssistantView):
    """Consume a single-use grant and redirect without exposing the destination to JS."""

    url = "/api/nitrado_gameserver/cockpit-handoff/{nonce}"
    name = "api:nitrado_gameserver:cockpit_handoff"
    requires_auth = True

    async def get(self, request: Any, nonce: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        hass = request.app["hass"]
        user = request.get("hass_user")
        try:
            grant = consume_cockpit_handoff(
                hass,
                nonce=nonce,
                user_id=str(getattr(user, "id", "")),
            )
        except CockpitLeaseError as err:
            raise _json_error(str(err), code=err.code, status=410) from err
        coordinator = _find_exact_coordinator(hass, grant.account_entry_id, grant.service_id)
        try:
            login = await coordinator.client.webinterface_login(grant.service_id)
        except NitradoApiError as err:
            raise _http_api_error(err) from err
        raise web.HTTPFound(
            location=login.url,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )


class ProfileExtensionsView(HomeAssistantView):
    """Expose profile extension descriptors for one service."""

    url = "/api/nitrado_gameserver/services/{service_id}/extensions"
    name = "api:nitrado_gameserver:extensions"
    requires_auth = True

    async def get(self, request: Any, service_id: str) -> Any:
        coordinator = _find_coordinator(request.app["hass"], service_id)
        runtime = coordinator.get_runtime(service_id)
        profile = runtime.profile
        manifest = _manifest_payload(runtime)
        if manifest.get("invalid"):
            resources = ()
            surfaces = ()
        else:
            try:
                resources = coordinator.profile_resource_descriptors(service_id)
                surfaces = coordinator.profile_surface_descriptors(service_id)
            except ProfileExtensionError as err:
                raise _http_blocked(err) from err
        return self.json(
            _json_safe(
                {
                    "service_id": service_id,
                    "profile": {
                        "profile_id": getattr(profile, "profile_id", None),
                        "name": getattr(profile, "name", None),
                    },
                    "resources": resources,
                    "surfaces": _surface_descriptors_payload(
                        request.app["hass"],
                        service_id,
                        str(getattr(coordinator, "account_entry_id", "")),
                        profile,
                        surfaces,
                    ),
                    "manifest": manifest,
                }
            )
        )


class ProfileActionView(HomeAssistantView):
    """Run a profile-declared action."""

    url = "/api/nitrado_gameserver/services/{service_id}/actions/{action_key}"
    name = "api:nitrado_gameserver:action"
    requires_auth = True

    async def post(self, request: Any, service_id: str, action_key: str) -> Any:
        coordinator = _find_coordinator(request.app["hass"], service_id)
        access, expected_profile = _declaration_authorization(
            coordinator, service_id, "actions", action_key, ExtensionAccess.ADMIN
        )
        _require_extension_access(request, access)
        body = await _json_body(request)
        try:
            result = await coordinator.async_run_profile_action(
                service_id,
                action_key,
                payload=_optional_extension_payload(body),
                confirmed=_optional_bool(body, "confirm", default=False),
                expected_profile=expected_profile,
            )
        except ProfileExtensionError as err:
            raise _http_blocked(err) from err
        except NitradoApiError as err:
            raise _http_api_error(err) from err
        except Exception as err:
            raise _http_extension_error(err) from err
        return self.json(_json_safe({"result": result}))


class ProfileResourceView(HomeAssistantView):
    """Fetch one profile-declared resource."""

    url = "/api/nitrado_gameserver/services/{service_id}/resources/{resource_key}"
    name = "api:nitrado_gameserver:resource"
    requires_auth = True

    async def post(self, request: Any, service_id: str, resource_key: str) -> Any:
        coordinator = _find_coordinator(request.app["hass"], service_id)
        access, expected_profile = _declaration_authorization(
            coordinator,
            service_id,
            "resources",
            resource_key,
            ExtensionAccess.AUTHENTICATED,
        )
        _require_extension_access(request, access)
        body = await _json_body(request)
        try:
            result = await coordinator.async_fetch_profile_resource(
                service_id,
                resource_key,
                payload=_optional_extension_payload(body),
                use_cache=_optional_bool(body, "use_cache", default=True),
                expected_profile=expected_profile,
            )
        except ProfileExtensionError as err:
            raise _http_blocked(err) from err
        except NitradoApiError as err:
            raise _http_api_error(err) from err
        except Exception as err:
            raise _http_extension_error(err) from err
        if isinstance(result.data, (bytes, bytearray, memoryview)) and web is not None:
            return web.Response(
                body=bytes(result.data),
                content_type=result.content_type,
                headers={
                    "X-Nitrado-Resource-Key": result.key,
                    "X-Nitrado-Resource-Cached": "1" if result.cached else "0",
                },
            )
        if hasattr(result.data, "__aiter__") and web is not None:
            stream = result.data
            response = web.StreamResponse(
                headers={
                    "Content-Type": result.content_type,
                    "X-Nitrado-Resource-Key": result.key,
                    "X-Nitrado-Resource-Cached": "0",
                }
            )
            try:
                await response.prepare(request)
                written = 0
                async with asyncio.timeout(PROFILE_HANDLER_TIMEOUT_SECONDS):
                    async for chunk in stream:
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        elif isinstance(chunk, (bytearray, memoryview)):
                            chunk = bytes(chunk)
                        if not isinstance(chunk, bytes):
                            raise web.HTTPInternalServerError(reason="Profile stream emitted a non-bytes chunk")
                        written += len(chunk)
                        if written > _MAX_STREAM_RESOURCE_BYTES:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=_MAX_STREAM_RESOURCE_BYTES,
                                actual_size=written,
                            )
                        await response.write(chunk)
                await response.write_eof()
                return response
            finally:
                closer = getattr(stream, "aclose", None)
                if callable(closer):
                    with suppress(Exception):
                        async with asyncio.timeout(5.0):
                            await closer()
        return self.json(_json_safe({"resource": result}))


class ProfileSurfaceResourcesView(HomeAssistantView):
    """Fetch resources composed by one profile-declared surface."""

    url = "/api/nitrado_gameserver/services/{service_id}/surfaces/{surface_key}/resources"
    name = "api:nitrado_gameserver:surface_resources"
    requires_auth = True

    async def post(self, request: Any, service_id: str, surface_key: str) -> Any:
        coordinator = _find_coordinator(request.app["hass"], service_id)
        access, expected_profile = _surface_resource_authorization(coordinator, service_id, surface_key)
        _require_extension_access(request, access)
        body = await _json_body(request)
        try:
            bundle = await coordinator.async_fetch_profile_surface_resources(
                service_id,
                surface_key,
                payload=_optional_extension_payload(body),
                use_cache=_optional_bool(body, "use_cache", default=True),
                expected_profile=expected_profile,
            )
        except ProfileExtensionError as err:
            raise _http_blocked(err) from err
        except NitradoApiError as err:
            raise _http_api_error(err) from err
        except Exception as err:
            raise _http_extension_error(err) from err
        return self.json(_json_safe({"surface": bundle.surface, "resources": bundle.resources}))


class ProfileEditableFileReadView(HomeAssistantView):
    """Read one profile-declared editable file."""

    url = "/api/nitrado_gameserver/services/{service_id}/editable-files/{file_key}/read"
    name = "api:nitrado_gameserver:editable_file_read"
    requires_auth = True

    async def post(self, request: Any, service_id: str, file_key: str) -> Any:
        coordinator = _find_coordinator(request.app["hass"], service_id)
        access, expected_profile = _declaration_authorization(
            coordinator, service_id, "editable_files", file_key, ExtensionAccess.ADMIN
        )
        _require_extension_access(request, access)
        body = await _json_body(request)
        include_raw = _optional_bool(body, "include_raw", default=False)
        if include_raw:
            _require_extension_access(request, ExtensionAccess.ADMIN)
        try:
            snapshot = await coordinator.async_read_editable_file(
                service_id, file_key, expected_profile=expected_profile
            )
        except ProfileExtensionError as err:
            raise _http_blocked(err) from err
        except NitradoApiError as err:
            raise _http_api_error(err) from err
        except Exception as err:
            raise _http_extension_error(err) from err
        return self.json(_editable_snapshot_payload(snapshot, include_raw=include_raw))


class ProfileEditableFilePreviewView(HomeAssistantView):
    """Preview one profile-declared editable-file write."""

    url = "/api/nitrado_gameserver/services/{service_id}/editable-files/{file_key}/preview"
    name = "api:nitrado_gameserver:editable_file_preview"
    requires_auth = True

    async def post(self, request: Any, service_id: str, file_key: str) -> Any:
        coordinator = _find_coordinator(request.app["hass"], service_id)
        access, expected_profile = _declaration_authorization(
            coordinator, service_id, "editable_files", file_key, ExtensionAccess.ADMIN
        )
        _require_extension_access(request, access)
        body = await _json_body(request)
        include_raw = _optional_bool(body, "include_raw", default=False)
        if include_raw:
            _require_extension_access(request, ExtensionAccess.ADMIN)
        value_is_parsed = _optional_bool(body, "value_is_parsed", default=False)
        value = _required_edit_value(body, value_is_parsed=value_is_parsed)
        try:
            preview = await coordinator.async_preview_editable_file(
                service_id,
                file_key,
                value,
                value_is_parsed=value_is_parsed,
                expected_profile=expected_profile,
            )
        except ProfileExtensionError as err:
            raise _http_blocked(err) from err
        except NitradoApiError as err:
            raise _http_api_error(err) from err
        except Exception as err:
            raise _http_extension_error(err) from err
        return self.json(_editable_preview_payload(preview, include_raw=include_raw))


class ProfileEditableFileApplyView(HomeAssistantView):
    """Reject legacy direct writes outside the bound cockpit workflow."""

    url = "/api/nitrado_gameserver/services/{service_id}/editable-files/{file_key}/apply"
    name = "api:nitrado_gameserver:editable_file_apply"
    requires_auth = True

    async def post(self, request: Any, service_id: str, file_key: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        if not EDITOR_MUTATIONS_ENABLED:
            raise _bad_request("Editable-file writes are disabled in this release")
        raise _json_error(
            "Editable-file writes require the cockpit's session-bound preview workflow.",
            code="cockpit_preview_required",
            status=409,
        )


class ProfileEditableFileRollbackView(HomeAssistantView):
    """Reject legacy path-based rollback outside the bound cockpit workflow."""

    url = "/api/nitrado_gameserver/services/{service_id}/editable-files/{file_key}/rollback"
    name = "api:nitrado_gameserver:editable_file_rollback"
    requires_auth = True

    async def post(self, request: Any, service_id: str, file_key: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        if not EDITOR_MUTATIONS_ENABLED:
            raise _bad_request("Editable-file rollback is disabled in this release")
        raise _json_error(
            "Editable-file rollback requires a cockpit recovery ID.",
            code="cockpit_recovery_required",
            status=409,
        )


class ProfileOptionView(HomeAssistantView):
    """Persist one administrator-only profile option."""

    url = "/api/nitrado_gameserver/services/{service_id}/profile-options/{option_key}"
    name = "api:nitrado_gameserver:profile_option"
    requires_auth = True

    async def post(self, request: Any, service_id: str, option_key: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        hass = request.app["hass"]
        entry_id, coordinator = _find_entry_coordinator(hass, service_id)
        body = await _json_body(request)
        response = await _async_update_profile_option(
            hass,
            entry_id,
            coordinator,
            service_id,
            option_key,
            body,
        )
        return self.json(response)


async def _async_update_profile_option(
    hass: Any,
    entry_id: str,
    coordinator: Any,
    service_id: str,
    option_key: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Persist one exact profile option for legacy and cockpit callers."""

    runtime = coordinator.get_runtime(service_id)
    expected_profile = coordinator.profile_dispatch_token(service_id)
    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        raise _bad_request("The selected profile manifest is invalid; details were logged.") from err
    declaration = next((item for item in manifest.profile_options if item.key == option_key), None)
    if declaration is None:
        raise _bad_request(f"Profile option not found: {option_key}")
    clear_value = body.get("clear") is True
    value = body.get("value")
    if not clear_value:
        try:
            value = validate_profile_option_value(declaration, value)
        except ValueError as err:
            raise _bad_request(str(err)) from err
    try:
        runtime = coordinator.require_profile_dispatch_token(service_id, expected_profile)
    except ProfileExtensionError as err:
        raise _http_blocked(err) from err
    intent = OperationIntent(
        "profile-option",
        str(option_key),
        generation=(
            f"{runtime.profile.profile_id}-{runtime.profile_generation}-{runtime.profile_registry_generation_seen}"
        ),
        expected_evidence="Home Assistant config-entry options contain the selected profile value",
    )
    async with (
        entry_option_update_lock(hass, entry_id),
        coordinator.async_operation(service_id, intent) as reservation,
    ):
        try:
            runtime = coordinator.require_profile_dispatch_token(service_id, expected_profile)
        except ProfileExtensionError as err:
            raise _http_blocked(err) from err
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise _bad_request("The Nitrado config entry is no longer available")
        profile_id = str(runtime.profile.profile_id)
        current = profile_option_value(
            dict(entry.options),
            profile_id,
            declaration.key,
            str(service_id),
            declaration.default,
        )
        revision = declaration.acknowledgement_revision
        acknowledged = revision is None or profile_option_acknowledged(
            dict(entry.options),
            profile_id,
            declaration.key,
            str(service_id),
            revision,
        )
        changing = clear_value or value != current or not acknowledged
        if declaration.confirmation_required and changing and body.get("confirm") is not True:
            raise _bad_request("Explicit confirmation is required for this profile setting")
        option_args = (
            dict(entry.options),
            profile_id,
            declaration.key,
            str(service_id),
        )
        options = (
            cleared_profile_option_options(*option_args)
            if clear_value
            else updated_profile_option_options(*option_args, value)
        )
        acknowledgement_args = (
            options,
            str(runtime.profile.profile_id),
            declaration.key,
            str(service_id),
        )
        if clear_value:
            options = cleared_profile_option_acknowledgement_options(*acknowledgement_args)
        elif declaration.acknowledgement_revision is not None:
            options = updated_profile_option_acknowledgement_options(
                *acknowledgement_args,
                declaration.acknowledgement_revision,
            )
        if declaration.idle_shutdown_required and (clear_value or value is not True):
            options = updated_idle_toggle_options(
                options,
                str(service_id),
                "idle_shutdown_service_ids",
                False,
            )
        coordinator.options = replace(
            coordinator.options,
            profile_options=profile_options_map(options),
            profile_option_acknowledgements=profile_option_acknowledgements_map(options),
            idle_shutdown_service_ids=frozenset(service_id_set(options, "idle_shutdown_service_ids")),
        )
        await reservation.async_mark_dispatched()
        hass.config_entries.async_update_entry(entry, options=options)
        await coordinator.async_profile_options_changed(
            runtime,
            "Profile option changed; idle timer reset",
        )
        await async_update_repair_issues(hass, entry, coordinator)
        await reservation.async_mark_verifying()
    response = {"key": declaration.key, "configured": not clear_value}
    if not clear_value and declaration.option_type != ProfileOptionType.SECRET:
        response["value"] = value
    return response


class FilesystemSettingsView(HomeAssistantView):
    """Persist advanced administrator-only provider filesystem consent."""

    url = "/api/nitrado_gameserver/services/{service_id}/filesystem-settings"
    name = "api:nitrado_gameserver:filesystem_settings"
    requires_auth = True

    async def post(self, request: Any, service_id: str) -> Any:
        """Update per-service plaintext FTP consent without provider I/O."""

        _require_extension_access(request, ExtensionAccess.ADMIN)
        body = await _json_body(request)
        if "allow_plaintext_ftp" not in body:
            raise _bad_request("allow_plaintext_ftp is required")
        enabled = _optional_bool(body, "allow_plaintext_ftp", default=False)
        hass = request.app["hass"]
        entry_id, coordinator = _find_entry_coordinator(hass, service_id)
        intent = OperationIntent(
            "filesystem-consent",
            "allow-plaintext-ftp",
            expected_evidence="Home Assistant config-entry options contain the selected consent value",
        )
        async with (
            entry_option_update_lock(hass, entry_id),
            coordinator.async_operation(service_id, intent) as reservation,
        ):
            # Re-resolve inside the entry-wide lock so service removal cannot
            # turn this read/modify/write transaction into a stale update.
            locked_entry_id, coordinator = _find_entry_coordinator(hass, service_id)
            if locked_entry_id != entry_id:
                raise _bad_request("The Nitrado service changed config entries; reload the panel and try again")
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                raise _bad_request("The Nitrado config entry is no longer available")
            options = updated_idle_toggle_options(
                dict(entry.options),
                str(service_id),
                CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
                enabled,
            )
            await reservation.async_mark_dispatched()
            hass.config_entries.async_update_entry(entry, options=options)
            allowed = frozenset(service_id_set(options, CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS))
            coordinator.options = replace(
                coordinator.options,
                allow_plaintext_ftp_service_ids=allowed,
            )
            filesystem = getattr(coordinator, "filesystem", None)
            update_consent = getattr(filesystem, "update_plaintext_ftp_consent", None)
            if callable(update_consent):
                update_consent(set(allowed))
            await reservation.async_mark_verifying()
        return self.json(
            {
                "allow_plaintext_ftp": str(service_id) in allowed,
            }
        )


class ProviderGrantCollectionView(HomeAssistantView):
    """Manage exact persistent companion-integration provider grants."""

    url = "/api/nitrado_gameserver/provider-grants"
    name = "api:nitrado_gameserver:provider_grants"
    requires_auth = True

    async def get(self, request: Any) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        runtime = get_provider_runtime(request.app["hass"])
        if runtime is None:
            raise _bad_request("The Nitrado provider connector is unavailable")
        return self.json({"grants": [item.as_storage() for item in runtime.authority.grants()]})

    async def post(self, request: Any) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        body = await _json_body(request)
        _require_confirmation(body, "Creating provider access")
        hass = request.app["hass"]
        runtime = get_provider_runtime(hass)
        if runtime is None:
            raise _bad_request("The Nitrado provider connector is unavailable")
        consumer_domain = _required_short_string(body, "consumer_domain")
        consumer_entry_id = _required_short_string(body, "consumer_entry_id")
        account_entry_id = _required_short_string(body, "account_entry_id")
        service_id = _required_numeric_string(body, "service_id")
        consumer_entry = hass.config_entries.async_get_entry(consumer_entry_id)
        if consumer_entry is None or consumer_entry.domain != consumer_domain:
            raise _bad_request("The companion config entry does not match the requested domain")
        coordinator = hass.data.get(DOMAIN, {}).get(account_entry_id)
        if coordinator is None or service_id not in getattr(coordinator, "services", {}):
            raise _bad_request("The exact Nitrado account entry does not manage this service")
        try:
            raw_scopes = body.get("scopes")
            raw_roots = body.get("permitted_roots", [])
            if not isinstance(raw_scopes, list) or not isinstance(raw_roots, list):
                raise ValueError("scopes and permitted_roots must be arrays")
            scopes = frozenset(ProviderScope(str(item)) for item in raw_scopes)
            disabled_scopes = {
                scope
                for scope, enabled in (
                    (ProviderScope.FILESYSTEM_REPLACE, FILESYSTEM_TREE_REPLACE_ENABLED),
                    (ProviderScope.NATIVE_BACKUP_RESTORE, NATIVE_BACKUP_RESTORE_ENABLED),
                )
                if not enabled
            }
            requested_disabled = scopes & disabled_scopes
            if requested_disabled:
                names = ", ".join(sorted(scope.value for scope in requested_disabled))
                raise ValueError(f"Provider capability is disabled in this release: {names}")
            roots = frozenset(str(item) for item in raw_roots)
            grant = new_provider_grant(
                consumer_domain=consumer_domain,
                consumer_entry_id=consumer_entry_id,
                account_entry_id=account_entry_id,
                service_id=service_id,
                scopes=scopes,
                permitted_roots=roots,
            )
            await runtime.authority.async_put_grant(grant)
        except (ProviderApiError, TypeError, ValueError) as err:
            raise _bad_request(str(err)) from err
        return self.json(
            {"grant": next(item.as_storage() for item in runtime.authority.grants() if item.grant_id == grant.grant_id)}
        )


class ProviderGrantItemView(HomeAssistantView):
    """Revoke one persistent companion-integration provider grant."""

    url = "/api/nitrado_gameserver/provider-grants/{grant_id}"
    name = "api:nitrado_gameserver:provider_grant"
    requires_auth = True

    async def delete(self, request: Any, grant_id: str) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        runtime = get_provider_runtime(request.app["hass"])
        if runtime is None:
            raise _bad_request("The Nitrado provider connector is unavailable")
        removed = await runtime.async_delete_grant(grant_id)
        return self.json({"removed": removed})


class ProviderMutationApprovalView(HomeAssistantView):
    """Issue an opaque one-shot token for one registry-owned mutation plan."""

    url = "/api/nitrado_gameserver/provider-mutation-approvals"
    name = "api:nitrado_gameserver:provider_mutation_approvals"
    requires_auth = True

    async def get(self, request: Any) -> Any:
        """List only secret-free active plans for administrator review."""

        _require_extension_access(request, ExtensionAccess.ADMIN)
        runtime = get_provider_runtime(request.app["hass"])
        if runtime is None:
            raise _bad_request("The Nitrado provider connector is unavailable")
        return self.json(
            {
                "plans": [_provider_plan_payload(plan) for plan in runtime.registry.pending_plans()],
                "progress": [
                    {
                        "plan_id": item.plan_id,
                        "audit_id": item.audit_id,
                        "action": item.action,
                        "state": item.state.value,
                        "updated_at": item.updated_at,
                        "error_code": item.error_code,
                    }
                    for item in runtime.registry.recent_progress()
                ],
            }
        )

    async def post(self, request: Any) -> Any:
        _require_extension_access(request, ExtensionAccess.ADMIN)
        body = await _json_body(request)
        decision = str(body.get("decision") or "approve").strip().lower()
        if decision not in {"approve", "reject"}:
            raise _bad_request("Provider mutation decision must be approve or reject")
        _require_confirmation(
            body,
            "Approving a provider mutation" if decision == "approve" else "Rejecting a provider mutation",
        )
        runtime = get_provider_runtime(request.app["hass"])
        if runtime is None:
            raise _bad_request("The Nitrado provider connector is unavailable")
        plan_id = _required_short_string(body, "plan_id")
        payload_digest = _required_short_string(body, "payload_digest")
        try:
            if decision == "reject":
                rejected = runtime.registry.reject_pending_plan(plan_id, payload_digest)
                return self.json({"decision": "rejected", "plan": _provider_plan_payload(rejected)})
            plan = runtime.registry.get_pending_plan(plan_id, payload_digest)
            approval = await runtime.authority.async_issue_approval(
                runtime.registry,
                plan_id,
                payload_digest,
            )
            runtime.registry.publish_approval(approval)
        except (ProviderApiError, TypeError, ValueError) as err:
            raise _bad_request(str(err)) from err
        return self.json(
            {
                "decision": "approved",
                "approval": {
                    "plan_id": approval.plan_id,
                    "payload_digest": approval.payload_digest,
                    "approval_token": approval.approval_token,
                },
                "plan": _provider_plan_payload(plan),
            }
        )


def _provider_plan_payload(plan: Any) -> dict[str, Any]:
    """Serialize the immutable, secret-free plan facts reviewed by an administrator."""

    return {
        "plan_id": plan.plan_id,
        "audit_id": plan.audit_id,
        "payload_digest": plan.payload_digest,
        "action": plan.action,
        "summary": plan.summary,
        "consumer_domain": plan.consumer_domain,
        "consumer_entry_id": plan.consumer_entry_id,
        "grant_id": plan.grant_id,
        "account_entry_id": plan.service_ref.account_entry_id,
        "service_id": plan.service_ref.service_id,
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
    }


def _find_coordinator(hass: Any, service_id: str) -> Any:
    matches = [
        coordinator
        for coordinator in hass.data.get(DOMAIN, {}).values()
        if getattr(coordinator, "setup_committed", True)
        and hasattr(coordinator, "services")
        and str(service_id) in coordinator.services
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _bad_request(
            f"Nitrado service {service_id} exists in more than one account; use a composite account/service route."
        )
    raise _bad_request(f"Nitrado service {service_id} is not managed by this Home Assistant instance.")


def _find_exact_coordinator(hass: Any, account_entry_id: str, service_id: str) -> Any:
    """Resolve one composite account/service identity without first-match fallback."""

    coordinator = hass.data.get(DOMAIN, {}).get(str(account_entry_id))
    if (
        coordinator is None
        or not getattr(coordinator, "setup_committed", True)
        or not hasattr(coordinator, "services")
        or str(service_id) not in coordinator.services
    ):
        raise _bad_request("The selected Nitrado account/service is no longer managed by this instance.")
    return coordinator


def _cockpit_compatibility(declaration: Any) -> tuple[Any | None, str | None]:
    """Return a host-compatible declaration without disabling its backend profile."""

    if declaration is None:
        return None, None
    if getattr(declaration, "cockpit_api_version", None) != COCKPIT_API_VERSION:
        return None, "cockpit_api_unsupported"
    return declaration, None


def _normalize_cockpit_profile_state(candidate: Any) -> Mapping[str, Any]:
    """Detach one bounded presenter result outside Home Assistant's loop."""

    encoded = json.dumps(candidate, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_EXTENSION_PAYLOAD_BYTES:
        raise ValueError("cockpit presenter result exceeds the supported size")
    detached = json.loads(encoded)
    if not isinstance(detached, dict):
        raise TypeError("cockpit presenter result must be an object")
    return detached


async def _cockpit_snapshot(
    hass: Any,
    coordinator: Any,
    runtime: Any,
    *,
    presenter_enabled: bool,
) -> dict[str, Any]:
    """Build a secret-free normalized host snapshot and opaque profile state."""

    service_id = str(runtime.state.identity.service_id)
    profile_id = str(getattr(runtime.profile, "profile_id", "generic"))
    entity_ids = _core_entity_ids_payload(hass, service_id, str(coordinator.account_entry_id))
    entities: dict[str, Any] = {}
    allowed_attributes = {"device_class", "unit_of_measurement", "friendly_name"}
    for key, entity_id in entity_ids.items():
        current = hass.states.get(entity_id)
        entities[key] = {
            "entity_id": entity_id,
            "state": None if current is None else current.state,
            "attributes": (
                {}
                if current is None
                else {name: current.attributes[name] for name in allowed_attributes if name in current.attributes}
            ),
        }

    public_option_payload = _public_profile_option_values(runtime)
    public_options = dict(public_option_payload["profile_option_values"])
    profile_values = getattr(coordinator.options, "profile_options", {}).get(profile_id, {})
    configured = frozenset(key for key, service_values in profile_values.items() if service_id in service_values)
    acknowledgement_values = getattr(coordinator.options, "profile_option_acknowledgements", {}).get(profile_id, {})
    acknowledgements = {
        key: values.get(service_id, 0) for key, values in acknowledgement_values.items() if isinstance(values, Mapping)
    }
    profile_state: Mapping[str, Any] = {}
    presenter = getattr(runtime.profile, "cockpit_snapshot", None)
    if presenter_enabled and callable(presenter):
        try:
            candidate = await async_invoke_profile(
                presenter,
                CockpitSnapshotContext(
                    service_id=service_id,
                    server=runtime.server,
                    status_fresh=bool(runtime.status_fresh),
                    using_cached_data=bool(runtime.using_cached_data),
                    public_options=public_options,
                    configured_options=configured,
                    option_acknowledgements=acknowledgements,
                ),
                worker_identity=(
                    "cockpit_presenter",
                    profile_id,
                    runtime.profile_generation,
                    runtime.profile_registration_generation,
                    runtime.profile_registry_generation_seen,
                ),
            )
            profile_state = await async_run_profile_sync(
                (
                    "cockpit_presenter_normalize",
                    profile_id,
                    runtime.profile_generation,
                    runtime.profile_registration_generation,
                    runtime.profile_registry_generation_seen,
                ),
                _normalize_cockpit_profile_state,
                candidate,
                timeout=15.0,
            )
        except Exception as err:
            log_profile_failure(_LOGGER, "cockpit_presenter", err, profile_id=profile_id, key=service_id)

    server = runtime.server
    service = runtime.service
    display_names = getattr(coordinator.options, "service_display_names", {})
    return {
        "identity": {
            "account_entry_id": str(coordinator.account_entry_id),
            "service_id": service_id,
            "service_name": service_display_name(runtime, display_names),
            "game_name": (
                getattr(server, "game_human", None)
                or getattr(service, "game_human", None)
                or getattr(service, "game", None)
                or "Unknown game"
            ),
            "profile_id": profile_id,
            "profile_name": str(getattr(runtime.profile, "name", "Generic Nitrado")),
        },
        "provider": {
            "status": getattr(server, "raw_status", None),
            "address": getattr(server, "address", None),
            "slots": getattr(server, "player_max", None),
            "status_fresh": bool(runtime.status_fresh),
            "using_cached_data": bool(runtime.using_cached_data),
            "refreshed_at": runtime.refreshed_at,
        },
        "entities": entities,
        "profile": {
            "manifest": _manifest_payload(runtime),
            "public_options": public_options,
            "configured_options": sorted(configured),
            "public_state": profile_state,
            "editor_mutations_enabled": EDITOR_MUTATIONS_ENABLED,
            "save_bundle_mutations_enabled": FILESYSTEM_TREE_REPLACE_ENABLED,
        },
        "tools": {
            "filesystem": _filesystem_status_payload(coordinator, service_id, _manifest_payload(runtime)),
        },
    }


async def _operation_facts_payload(coordinator: Any, service_id: str) -> dict[str, Any]:
    """Return bounded, sanitized operation truth for controls and recovery UI."""

    try:
        records = await coordinator.async_operation_records(service_id)
    except OperationJournalError:
        return {
            "available": False,
            "blocked": True,
            "code": "operation_journal_unavailable",
            "active": [],
            "unknown": [],
            "history": [],
        }
    active: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    for record in records[-32:]:
        item = {
            "operation_id": record.operation_id,
            "kind": record.intent.kind,
            "target_key": record.intent.target_key,
            "phase": record.phase.value,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "result_code": record.result_code,
            "expected_evidence": record.intent.expected_evidence,
        }
        if record.phase is OperationPhase.UNKNOWN:
            unknown.append(item)
        elif record.phase is OperationPhase.TERMINAL:
            history.append(item)
        else:
            active.append(item)
    return {
        "available": True,
        "blocked": bool(active or unknown),
        "code": "operation_blocked" if active or unknown else "operation_ready",
        "active": active,
        "unknown": unknown,
        "history": history[-10:],
    }


async def _async_cockpit_entity_action(
    hass: Any,
    account_entry_id: str,
    service_id: str,
    body: dict[str, Any],
) -> dict[str, str]:
    """Dispatch one allowlisted core entity action after ownership validation."""

    key = _required_short_string(body, "entity_key")
    action = _required_short_string(body, "action")
    allowed = {
        ("auto_shutdown", "turn_on"): ("switch", "turn_on"),
        ("auto_shutdown", "turn_off"): ("switch", "turn_off"),
        ("cancel_pending_shutdown", "press"): ("button", "press"),
        ("idle_shutdown_minutes", "set_value"): ("number", "set_value"),
        ("startup_cooldown_minutes", "set_value"): ("number", "set_value"),
    }
    service_call = allowed.get((key, action))
    if service_call is None:
        raise _bad_request("Unsupported cockpit entity action")
    entity_id = _core_entity_ids_payload(hass, service_id, account_entry_id).get(key)
    if not entity_id:
        raise _bad_request("The requested Home Assistant control is unavailable")
    try:
        from homeassistant.helpers import entity_registry as er

        registry_entry = er.async_get(hass).async_get(entity_id)
    except (ImportError, AttributeError):
        registry_entry = None
    if registry_entry is None or str(getattr(registry_entry, "config_entry_id", "")) != str(account_entry_id):
        raise _bad_request("The requested control does not belong to the selected Nitrado account")
    domain, service = service_call
    data: dict[str, Any] = {}
    if service == "set_value":
        value = body.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _bad_request("value must be numeric")
        data["value"] = float(value)
    await hass.services.async_call(
        domain,
        service,
        data,
        target={"entity_id": entity_id},
        blocking=True,
    )
    return {"code": "entity_action_accepted"}


def _find_entry_coordinator(hass: Any, service_id: str) -> tuple[str, Any]:
    """Return config-entry ID and coordinator for one managed service."""

    matches = [
        (str(entry_id), coordinator)
        for entry_id, coordinator in hass.data.get(DOMAIN, {}).items()
        if getattr(coordinator, "setup_committed", True)
        and hasattr(coordinator, "services")
        and str(service_id) in coordinator.services
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _bad_request(
            f"Nitrado service {service_id} exists in more than one account; use a composite account/service route."
        )
    raise _bad_request(f"Nitrado service {service_id} is not managed by this Home Assistant instance.")


def _declaration_authorization(
    coordinator: Any,
    service_id: str,
    family: str,
    key: str,
    default: ExtensionAccess,
) -> tuple[ExtensionAccess, Any]:
    """Return access plus the exact profile generation that declared it."""

    runtime = coordinator.get_runtime(service_id)
    profile = runtime.profile
    token = coordinator.profile_dispatch_token(service_id)
    if profile is None:
        return default, token
    try:
        declarations = getattr(runtime_profile_extension_manifest(runtime), family)
    except ProfileManifestError as err:
        log_profile_failure(_LOGGER, "declaration_access", err, profile_id=getattr(profile, "profile_id", "unknown"))
        raise _bad_request("The selected profile manifest is invalid; details were logged.") from None
    declaration = next((item for item in declarations if item.key == key), None)
    return getattr(declaration, "access", default), token


def _surface_resource_authorization(coordinator: Any, service_id: str, surface_key: str) -> tuple[ExtensionAccess, Any]:
    """Return strict surface access plus its exact profile generation."""

    runtime = coordinator.get_runtime(service_id)
    profile = runtime.profile
    token = coordinator.profile_dispatch_token(service_id)
    if profile is None:
        return ExtensionAccess.AUTHENTICATED, token
    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        log_profile_failure(_LOGGER, "surface_access", err, profile_id=getattr(profile, "profile_id", "unknown"))
        raise _bad_request("The selected profile manifest is invalid; details were logged.") from None
    surface = next((item for item in manifest.surfaces if item.key == surface_key), None)
    if surface is None:
        return ExtensionAccess.AUTHENTICATED, token
    resource_access = {item.key: item.access for item in manifest.resources}
    if surface.access == ExtensionAccess.ADMIN or any(
        resource_access.get(key) == ExtensionAccess.ADMIN for key in surface.resources
    ):
        return ExtensionAccess.ADMIN, token
    return ExtensionAccess.AUTHENTICATED, token


def _require_extension_access(request: Any, access: ExtensionAccess) -> None:
    """Require a Home Assistant administrator for admin-only extensions."""

    if access != ExtensionAccess.ADMIN:
        return
    user = request.get("hass_user") if hasattr(request, "get") else None
    if user is not None and bool(getattr(user, "is_admin", False)):
        return
    raise _json_error(
        "Administrator access is required for this extension operation.",
        code="forbidden",
        status=403,
    )


async def _json_body(request: Any) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as err:
        raise _bad_request("Request body must be a valid JSON object.") from err
    if not isinstance(body, dict):
        raise _bad_request("Request body must be a JSON object.")
    return body


def _required_short_string(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise _bad_request(f"{key} must be a non-empty string of at most 128 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _bad_request(f"{key} contains invalid characters")
    return value.strip()


def _required_numeric_string(body: dict[str, Any], key: str) -> str:
    value = _required_short_string(body, key)
    if not value.isdigit():
        raise _bad_request(f"{key} must contain only digits")
    return value


def _optional_bool(body: dict[str, Any], key: str, *, default: bool) -> bool:
    """Read an optional boolean request field without string truthiness traps."""

    if key not in body:
        return default
    value = body[key]
    if not isinstance(value, bool):
        raise _bad_request(f"{key} must be a boolean")
    return value


def _optional_extension_payload(body: dict[str, Any]) -> Any:
    """Return a bounded finite-JSON extension payload."""

    value = body.get("payload")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, OverflowError) as err:
        raise _bad_request("payload must contain finite JSON data") from err
    if len(encoded) > _MAX_EXTENSION_PAYLOAD_BYTES:
        raise _bad_request(f"payload exceeds {_MAX_EXTENSION_PAYLOAD_BYTES} bytes")
    return value


def _require_confirmation(body: dict[str, Any], operation: str) -> None:
    """Require deliberate confirmation for mutating HTTP operations."""

    if _optional_bool(body, "confirm", default=False):
        return
    raise _bad_request(f"{operation} requires confirm=true")


def _required_edit_value(body: dict[str, Any], *, value_is_parsed: bool) -> Any:
    """Return the proposed editable-file value, requiring callers to be explicit."""

    if "value" not in body:
        raise _bad_request("value is required")
    value = body["value"]
    if not value_is_parsed and not isinstance(value, str):
        raise _bad_request("value must be a string when value_is_parsed is false")
    return value


def _required_edit_operations(body: dict[str, Any]) -> list[dict[str, str]]:
    """Return a bounded structured edit without logging credential values."""

    operations = body.get("operations")
    if not isinstance(operations, list) or len(operations) > 256:
        raise _bad_request("operations must be a bounded list")
    result: list[dict[str, str]] = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") != "set":
            raise _bad_request("every operation must be an explicit set")
        key = operation.get("key")
        raw_value = operation.get("raw_value")
        if not isinstance(key, str) or not key or len(key) > 128 or not isinstance(raw_value, str):
            raise _bad_request("every operation requires a bounded key and string value")
        result.append({"op": "set", "key": key, "raw_value": raw_value})
    return result


def _editable_snapshot_payload(snapshot: Any, *, include_raw: bool) -> dict[str, Any]:
    payload = {
        "key": snapshot.key,
        "name": snapshot.name,
        "path": snapshot.path,
        "text": snapshot.text if include_raw else snapshot.redacted_text,
        "redacted_text": snapshot.redacted_text,
        "requires_restart": snapshot.requires_restart,
        "revision": getattr(snapshot, "revision", None),
    }
    if include_raw:
        payload["parsed"] = snapshot.parsed
    editor_model = getattr(snapshot, "editor_model", None)
    if editor_model is not None:
        payload["model"] = editor_model
    return _json_safe(payload)


def _editable_preview_payload(preview: Any, *, include_raw: bool) -> dict[str, Any]:
    payload = {
        "key": preview.key,
        "name": preview.name,
        "path": preview.path,
        "current_text": preview.current_text if include_raw else preview.redacted_current_text,
        "proposed_text": preview.proposed_text if include_raw else preview.redacted_proposed_text,
        "redacted_current_text": preview.redacted_current_text,
        "redacted_proposed_text": preview.redacted_proposed_text,
        "diff": preview.diff if include_raw else preview.redacted_diff,
        "redacted_diff": preview.redacted_diff,
        "verdict": preview.verdict,
        "requires_restart": preview.requires_restart,
        "changed": preview.changed,
        "source_revision": getattr(preview, "source_revision", None),
        "proposed_revision": getattr(preview, "proposed_revision", None),
        "redacted_changes_hidden": bool(getattr(preview, "redacted_changes_hidden", False)),
    }
    if include_raw:
        payload["parsed"] = preview.parsed
    return _json_safe(payload)


def _manifest_payload(runtime: Any) -> dict[str, Any]:
    """Return full extension metadata for HTTP descriptor consumers."""

    if runtime is None or runtime.profile is None:
        return _empty_manifest_payload()
    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        log_profile_failure(
            _LOGGER,
            "manifest_payload",
            err,
            profile_id=getattr(runtime.profile, "profile_id", "unknown"),
        )
        payload = _empty_manifest_payload()
        payload["invalid"] = True
        payload["error"] = "The selected profile manifest is invalid; details were logged."
        return payload
    return {
        "invalid": False,
        "entities": [_entity_payload(entity) for entity in manifest.entities],
        "profile_options": [_profile_option_payload(option) for option in manifest.profile_options],
        "editable_files": [_editable_file_payload(file) for file in manifest.editable_files],
        "save_bundles": [_save_bundle_payload(bundle) for bundle in manifest.save_bundles],
        "resources": [_resource_payload(resource) for resource in manifest.resources],
        "actions": [_action_payload(action) for action in manifest.actions],
        "surfaces": [_surface_payload(surface) for surface in manifest.surfaces],
        "lifecycle_hooks": [_lifecycle_hook_payload(hook) for hook in manifest.lifecycle_hooks],
        "validators": [_validator_payload(validator) for validator in manifest.validators],
        "cockpit": (
            None
            if manifest.cockpit is None
            else {
                "key": manifest.cockpit.key,
                "name": manifest.cockpit.name,
                "api_version": manifest.cockpit.cockpit_api_version,
                "frontend_revision": manifest.cockpit.frontend_revision,
                "default_route": manifest.cockpit.default_route,
                "route_keys": manifest.cockpit.route_keys,
                "route_aliases": manifest.cockpit.route_aliases,
            }
        ),
    }


def _public_profile_option_values(runtime: Any) -> dict[str, Any]:
    """Expose option state without returning persisted secret values."""

    values = dict(runtime.extra.get("_persisted_profile_options", {}))
    configured: dict[str, bool] = {}
    try:
        declarations = (
            runtime_profile_extension_manifest(runtime).profile_options if runtime.profile is not None else ()
        )
    except ProfileManifestError:
        declarations = ()
    for declaration in declarations:
        if declaration.option_type != ProfileOptionType.SECRET:
            continue
        configured[declaration.key] = bool(values.get(declaration.key))
        values.pop(declaration.key, None)
    return {
        "profile_option_values": values,
        "profile_option_configured": configured,
    }


def _filesystem_status_payload(
    coordinator: Any,
    service_id: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an explicitly whitelisted, no-network filesystem status.

    The extension index is an administrator surface, but it still must not
    expose provider endpoints, credentials, remote paths, hashes, or file
    content.  Only cached facts from ``observed_status`` are accepted here.
    """

    allowed_ids = frozenset(
        str(value)
        for value in getattr(
            getattr(coordinator, "options", None),
            "allow_plaintext_ftp_service_ids",
            (),
        )
    )
    consent = str(service_id) in allowed_ids
    filesystem = getattr(coordinator, "filesystem", None)
    observed_status = getattr(filesystem, "observed_status", None)
    observed: Mapping[str, Any] = {}
    if callable(observed_status):
        try:
            candidate = observed_status(str(service_id))
            if isinstance(candidate, Mapping):
                observed = candidate
        except Exception:
            # Status rendering must never perform provider recovery or break
            # the extension panel.  The implementation logs the programming
            # error while returning a conservative empty snapshot.
            _LOGGER.exception("Could not read cached filesystem status for Nitrado service %s", service_id)
    http = observed.get("http")
    ftp = observed.get("ftp")
    http_status = http if isinstance(http, Mapping) else {}
    ftp_status = ftp if isinstance(ftp, Mapping) else {}
    transport = ftp_status.get("observed_transport")
    if transport not in {"ftp", "ftps"}:
        transport = None
    root_mapping_proven = http_status.get("root_mapping_proven") is True
    plaintext_required = ftp_status.get("plaintext_required") is True
    active_operations = ftp_status.get("active_operations")
    if not isinstance(active_operations, int) or isinstance(active_operations, bool) or active_operations < 0:
        active_operations = 0
    editable_files = manifest.get("editable_files")
    save_bundles = manifest.get("save_bundles")
    relevant = bool(
        (isinstance(editable_files, list) and editable_files)
        or (isinstance(save_bundles, list) and save_bundles)
        or consent
        or transport is not None
        or root_mapping_proven
    )
    return {
        "relevant": relevant,
        "allow_plaintext_ftp": consent,
        "http_root_mapping_proven": root_mapping_proven,
        "observed_transport": transport,
        "secure_transport_observed": transport == "ftps",
        "plaintext_consent_visible": consent or transport == "ftp" or plaintext_required,
        "plaintext_required": plaintext_required,
        "active_operations": active_operations,
        "closed": ftp_status.get("closed") is True,
    }


def _surface_descriptors_payload(
    hass: Any,
    service_id: str,
    account_entry_id: str | None,
    profile: Any,
    surfaces: Any,
) -> list[dict[str, Any]]:
    """Serialize surface descriptors and resolve declared HA control IDs."""

    profile_id = str(getattr(profile, "profile_id", "") or "")
    payloads = _json_safe(tuple(surfaces))
    if not isinstance(payloads, list):
        return []
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
    except (ImportError, AttributeError):
        registry = None
    for surface in payloads:
        if not isinstance(surface, dict):
            continue
        controls = surface.get("control_entities")
        if not isinstance(controls, list):
            continue
        for control in controls:
            if not isinstance(control, dict) or registry is None:
                continue
            entity_key = str(control.get("entity_key") or "")
            platform = str(control.get("platform") or "")
            if not entity_key or not platform or not profile_id:
                continue
            unique_id = ServiceIdentity(service_id, account_entry_id).entity_unique_id(
                entity_key,
                profile_id=profile_id,
            )
            control["entity_id"] = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    return payloads


def _core_entity_ids_payload(
    hass: Any,
    service_id: str,
    account_entry_id: str | None = None,
) -> dict[str, str]:
    """Resolve the normal HA entities used by the built-in server cockpit."""

    entity_platforms = {
        "status": "sensor",
        "address": "sensor",
        "player_count": "sensor",
        "player_max": "sensor",
        "online_players": "sensor",
        "auto_shutdown_status": "sensor",
        "idle_time_remaining": "sensor",
        "last_refresh_time": "sensor",
        "last_shutdown_reason": "sensor",
        "last_reset_reason": "sensor",
        "start_block_reason": "sensor",
        "stop_block_reason": "sensor",
        "running": "binary_sensor",
        "player_data_valid": "binary_sensor",
        "status_fresh": "binary_sensor",
        "using_cached_data": "binary_sensor",
        "can_start": "binary_sensor",
        "can_stop": "binary_sensor",
        "shutdown_pending": "binary_sensor",
        "refresh": "button",
        "start": "button",
        "stop": "button",
        "cancel_pending_shutdown": "button",
        "auto_shutdown": "switch",
        "maintenance_mode": "switch",
        "shutdown_dry_run": "switch",
        "idle_shutdown_minutes": "number",
        "startup_cooldown_minutes": "number",
    }
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
    except (ImportError, AttributeError):
        return {}
    identity = ServiceIdentity(service_id, account_entry_id)
    resolved: dict[str, str] = {}
    for key, platform in entity_platforms.items():
        entity_id = registry.async_get_entity_id(platform, DOMAIN, identity.entity_unique_id(key))
        registry_entry = registry.async_get(entity_id) if entity_id else None
        if entity_id and (
            account_entry_id is None
            or (
                registry_entry is not None
                and str(getattr(registry_entry, "config_entry_id", "")) == str(account_entry_id)
            )
        ):
            resolved[key] = entity_id
    return resolved


def _empty_manifest_payload() -> dict[str, Any]:
    """Return an empty manifest for managed services without a selected profile."""

    return {
        "invalid": False,
        "entities": [],
        "profile_options": [],
        "editable_files": [],
        "save_bundles": [],
        "resources": [],
        "actions": [],
        "surfaces": [],
        "lifecycle_hooks": [],
        "validators": [],
        "cockpit": None,
    }


def _profile_option_payload(option: Any) -> dict[str, Any]:
    """Serialize one administrator-managed profile option declaration."""

    return {
        "key": option.key,
        "name": option.name,
        "option_type": option.option_type,
        "description": option.description,
        "default": "" if option.option_type == ProfileOptionType.SECRET else option.default,
        "attributes": dict(option.attributes),
        "standard_options": option.standard_options,
        "onboarding": option.onboarding,
        "confirmation_required": option.confirmation_required,
        "acknowledgement_revision": option.acknowledgement_revision,
        "idle_shutdown_required": option.idle_shutdown_required,
    }


def _entity_payload(entity: EntityDeclaration) -> dict[str, Any]:
    return {
        "platform": entity.platform,
        "key": entity.key,
        "name": entity.name,
        "kind": entity.kind,
        "attributes": dict(entity.attributes),
    }


def _editable_file_payload(file: EditableFileDeclaration) -> dict[str, Any]:
    return {
        "key": file.key,
        "name": file.name,
        "description": file.description,
        "requires_restart": file.requires_restart,
        "requires_stopped": file.requires_stopped,
        "requires_running": file.requires_running,
        "create_backup": file.create_backup,
        "access": file.access,
        "validators": tuple(file.validators),
        "attributes": dict(file.attributes),
    }


def _save_bundle_payload(bundle: SaveBundleDeclaration) -> dict[str, Any]:
    return {
        "key": bundle.key,
        "name": bundle.name,
        "description": bundle.description,
        "required_files": tuple(bundle.required_files),
        "allowed_suffixes": tuple(bundle.allowed_suffixes),
        "excluded_paths": tuple(bundle.excluded_paths),
        "requires_stopped": bundle.requires_stopped,
        "access": bundle.access,
        "attributes": dict(bundle.attributes),
    }


def _resource_payload(resource: ResourceDeclaration) -> dict[str, Any]:
    return {
        "key": resource.key,
        "name": resource.name,
        "description": resource.description,
        "content_type": resource.content_type,
        "content_family": resource_content_family(resource),
        "cache_seconds": resource.cache_seconds,
        "validators": tuple(resource.validators),
        "access": resource.access,
        "attributes": dict(resource.attributes),
    }


def _action_payload(action: ActionDeclaration) -> dict[str, Any]:
    return {
        "key": action.key,
        "name": action.name,
        "description": action.description,
        "validators": tuple(action.validators),
        "requires_confirmation": action.requires_confirmation,
        "access": action.access,
        "inputs": [
            {
                "key": item.key,
                "name": item.name,
                "input_type": item.input_type,
                "description": item.description,
                "required": item.required,
                "default": item.default,
                "attributes": dict(item.attributes),
            }
            for item in action.inputs
        ],
        "attributes": dict(action.attributes),
    }


def _surface_payload(surface: SurfaceDeclaration) -> dict[str, Any]:
    return {
        "key": surface.key,
        "name": surface.name,
        "description": surface.description,
        "resources": tuple(surface.resources),
        "actions": tuple(surface.actions),
        "controls": tuple(surface.controls),
        "editable_files": tuple(surface.editable_files),
        "renderer_hint": surface.renderer_hint,
        "validators": tuple(surface.validators),
        "access": surface.access,
        "attributes": dict(surface.attributes),
    }


def _lifecycle_hook_payload(hook: LifecycleHookDeclaration) -> dict[str, Any]:
    return {
        "key": hook.key,
        "event": hook.event,
        "description": hook.description,
        "validators": tuple(hook.validators),
        "order": hook.order,
        "blocking": hook.blocking,
        "attributes": dict(hook.attributes),
    }


def _validator_payload(validator: ValidatorDeclaration) -> dict[str, Any]:
    return {
        "key": validator.key,
        "name": validator.name,
        "target": validator.target,
        "applies_to": tuple(validator.applies_to),
        "domains": tuple(validator.domains),
        "description": validator.description,
        "attributes": dict(validator.attributes),
    }


def _json_safe(value: Any) -> Any:
    """Return a JSON-safe value for authenticated extension HTTP responses."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value)
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
            "size": len(value),
        }
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, Mapping):
        return {_json_key_safe(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    return value


def _json_key_safe(value: Any) -> str:
    """Return a JSON object key string for arbitrary extension payload keys."""

    if isinstance(value, str):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _http_blocked(err: ProfileExtensionError) -> Exception:
    return _bad_request(err.verdict.reason or err.verdict.state.value, verdict=err.verdict)


def _http_api_error(err: NitradoApiError) -> Exception:
    _LOGGER.warning("Nitrado extension request failed error_type=%s", err.__class__.__name__)
    return _json_error(
        "The Nitrado API request failed; details were written to the Home Assistant log.",
        code="nitrado_api_error",
        status=502,
    )


def _http_extension_error(err: Exception) -> Exception:
    log_profile_failure(_LOGGER, "http_handler", err)
    return _json_error(
        "The profile extension failed safely; details were written to the Home Assistant log.",
        code="extension_handler_error",
        status=500,
    )


def _bad_request(message: str, *, verdict: CapabilityVerdict | None = None) -> Exception:
    return _json_error(message, code="bad_request", status=400, verdict=verdict)


def _json_error(
    message: str,
    *,
    code: str,
    status: int,
    verdict: CapabilityVerdict | None = None,
) -> Exception:
    payload = {
        "error": {
            "code": code,
            "message": message,
        }
    }
    if verdict is not None:
        payload["error"]["verdict"] = _json_safe(verdict)
    text = json.dumps(_json_safe(payload), separators=(",", ":"))
    if web is not None:
        if status == 409:
            return web.HTTPConflict(text=text, content_type="application/json")
        if status == 410:
            return web.HTTPGone(text=text, content_type="application/json")
        if status == 403:
            return web.HTTPForbidden(text=text, content_type="application/json")
        if status == 502:
            return web.HTTPBadGateway(text=text, content_type="application/json")
        if status >= 500:
            return web.HTTPInternalServerError(text=text, content_type="application/json")
        return web.HTTPBadRequest(text=text, content_type="application/json")
    return ValueError(text)
