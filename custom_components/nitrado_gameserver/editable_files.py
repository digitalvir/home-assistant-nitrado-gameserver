"""Safe editable-file plumbing for profile-declared game settings."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .api.nitrado import NitradoClient
from .const import RUNNING_STATUS, STOPPED_STATUS
from .extensions import (
    ProfileExtensionError,
    async_run_lifecycle_hooks,
    async_validate_profile,
    profile_action_context,
)
from .plugins.base import (
    SUPPORTED,
    CapabilityVerdict,
    EditableFileDeclaration,
    GameProfile,
    LifecycleEvent,
    ProfileManifestError,
    ValidatorTarget,
    async_invoke_profile,
    blocked,
    valid_capability_verdict,
)
from .plugins.registry import profile_registry_generation
from .profile_logging import log_profile_failure
from .profile_workers import async_run_profile_sync
from .runtime import ServiceRuntime, runtime_profile_extension_manifest

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class EditableFileSnapshot:
    """Current editable-file content read through profile plumbing."""

    key: str
    name: str
    path: str
    text: str
    redacted_text: str
    parsed: Any
    editor_model: Any | None
    requires_restart: bool
    revision: str


@dataclass(slots=True, frozen=True)
class EditableFilePreview:
    """Validated preview for an editable-file change."""

    key: str
    name: str
    path: str
    current_text: str
    proposed_text: str
    redacted_current_text: str
    redacted_proposed_text: str
    diff: str
    redacted_diff: str
    parsed: Any
    verdict: CapabilityVerdict
    requires_restart: bool
    changed: bool
    source_revision: str
    proposed_revision: str
    redacted_changes_hidden: bool


@dataclass(slots=True, frozen=True)
class EditableFileApplyResult:
    """Result of applying an editable-file change."""

    preview: EditableFilePreview
    wrote: bool
    backup_path: str | None = None
    operation_id: str | None = None
    recovery_id: str | None = None


@dataclass(slots=True, frozen=True)
class EditableFileRollbackResult:
    """Result of rolling an editable file back from a backup."""

    key: str
    name: str
    path: str
    backup_path: str
    pre_rollback_backup_path: str | None
    wrote: bool
    source_revision: str
    resulting_revision: str
    operation_id: str | None = None
    recovery_id: str | None = None


async def async_read_editable_file(
    runtime: ServiceRuntime,
    client: NitradoClient,
    file_key: str,
    *,
    now: int | None = None,
) -> EditableFileSnapshot:
    """Read and parse a profile-declared editable file."""

    profile, generation = _profile_snapshot(runtime)
    declaration = await _editable_file_declaration(runtime, file_key)
    path = await _resolve_path(runtime, client, declaration, now=now)
    _require_profile_snapshot(runtime, profile, generation)
    text = await _download_authoritative(client, runtime.state.identity.service_id, path)
    _require_profile_snapshot(runtime, profile, generation)
    parsed = await _parse(runtime, declaration, text)
    redacted_text = await _redact(declaration, text)
    editor_model = await _editor_model(runtime, declaration, text)
    public_parsed = await _public_editable_value(runtime, declaration, parsed)
    _require_profile_snapshot(runtime, profile, generation)
    return EditableFileSnapshot(
        key=declaration.key,
        name=declaration.name,
        path=path,
        text=text,
        redacted_text=redacted_text,
        parsed=public_parsed,
        editor_model=editor_model,
        requires_restart=declaration.requires_restart,
        revision=_text_revision(text),
    )


async def async_preview_editable_file(
    runtime: ServiceRuntime,
    client: NitradoClient,
    file_key: str,
    proposed_value: Any,
    *,
    value_is_parsed: bool = False,
    expected_source_revision: str | None = None,
    now: int | None = None,
) -> EditableFilePreview:
    """Build a validated diff preview for an editable-file change."""

    profile, generation = _profile_snapshot(runtime)
    declaration = await _editable_file_declaration(runtime, file_key)
    state_verdict = _state_prerequisite(runtime, declaration)
    snapshot = await async_read_editable_file(runtime, client, file_key, now=now)
    _require_profile_snapshot(runtime, profile, generation)
    if expected_source_revision is not None and snapshot.revision != expected_source_revision:
        raise ProfileExtensionError(
            blocked(f"{declaration.name} changed after this editor was opened; reload before previewing.")
        )
    parsed, proposed_text = await _proposed_text(
        runtime,
        declaration,
        proposed_value,
        source_text=snapshot.text,
        value_is_parsed=value_is_parsed,
    )
    validation = state_verdict
    if validation.allowed:
        validation = await _validate_editable_file(runtime, client, declaration, parsed, proposed_text, now=now)
    _require_profile_snapshot(runtime, profile, generation)
    diff = _unified_diff(
        snapshot.text, proposed_text, fromfile=f"{snapshot.path} (current)", tofile=f"{snapshot.path} (proposed)"
    )
    redacted_proposed = await _redact(declaration, proposed_text)
    redacted_diff = _unified_diff(
        snapshot.redacted_text,
        redacted_proposed,
        fromfile=f"{snapshot.path} (current, redacted)",
        tofile=f"{snapshot.path} (proposed, redacted)",
    )
    _require_profile_snapshot(runtime, profile, generation)
    return EditableFilePreview(
        key=declaration.key,
        name=declaration.name,
        path=snapshot.path,
        current_text=snapshot.text,
        proposed_text=proposed_text,
        redacted_current_text=snapshot.redacted_text,
        redacted_proposed_text=redacted_proposed,
        diff=diff,
        redacted_diff=redacted_diff,
        parsed=await _public_editable_value(runtime, declaration, parsed),
        verdict=validation,
        requires_restart=declaration.requires_restart,
        changed=snapshot.text != proposed_text,
        source_revision=snapshot.revision,
        proposed_revision=_text_revision(proposed_text),
        redacted_changes_hidden=snapshot.text != proposed_text and snapshot.redacted_text == redacted_proposed,
    )


async def async_probe_editable_file_backup(
    runtime: ServiceRuntime,
    client: NitradoClient,
    file_key: str,
    backup_path: str,
    *,
    now: int | None = None,
) -> CapabilityVerdict:
    """Verify that one generated backup still exists and parses safely."""

    profile, generation = _profile_snapshot(runtime)
    declaration = await _editable_file_declaration(runtime, file_key)
    target_path = await _resolve_path(runtime, client, declaration, now=now)
    normalized = _validated_backup_path(declaration, target_path, backup_path)
    _require_profile_snapshot(runtime, profile, generation)
    try:
        text = await _download_authoritative(client, runtime.state.identity.service_id, normalized)
        parsed = await _parse(runtime, declaration, text)
        verdict = await _validate_editable_file(runtime, client, declaration, parsed, text, now=now)
        _require_profile_snapshot(runtime, profile, generation)
        return verdict
    except ProfileExtensionError as err:
        return err.verdict
    except Exception:
        _LOGGER.warning("Editable-file recovery backup is unavailable for %s", declaration.key, exc_info=True)
        return blocked("The verified recovery backup is missing or unavailable.")


async def async_apply_editable_file(
    runtime: ServiceRuntime,
    client: NitradoClient,
    file_key: str,
    proposed_value: Any,
    *,
    value_is_parsed: bool = False,
    now: int | None = None,
    refresh_status: Callable[[], Awaitable[Any]] | None = None,
    expected_source_revision: str | None = None,
    expected_proposed_revision: str | None = None,
    expected_path: str | None = None,
) -> EditableFileApplyResult:
    """Validate and write a profile-declared editable file with backup first."""

    profile, generation = _profile_snapshot(runtime)
    declaration = await _editable_file_declaration(runtime, file_key)
    await _refresh_and_require_state(runtime, declaration, refresh_status)
    _require_profile_snapshot(runtime, profile, generation)
    preview = await async_preview_editable_file(
        runtime,
        client,
        file_key,
        proposed_value,
        value_is_parsed=value_is_parsed,
        now=now,
    )
    if not preview.verdict.allowed:
        raise ProfileExtensionError(preview.verdict)
    if expected_source_revision is not None and preview.source_revision != expected_source_revision:
        raise ProfileExtensionError(blocked(f"{declaration.name} changed after preview; apply was aborted."))
    if expected_proposed_revision is not None and preview.proposed_revision != expected_proposed_revision:
        raise ProfileExtensionError(blocked(f"{declaration.name} proposed content no longer matches its preview."))
    if expected_path is not None and preview.path != expected_path:
        raise ProfileExtensionError(blocked(f"{declaration.name} path changed after preview; apply was aborted."))
    if not preview.changed:
        return EditableFileApplyResult(preview=preview, wrote=False)

    backup_path = _backup_path(preview.path, now=now, suffix="backup") if declaration.create_backup else None
    payload = _payload(preview, backup_path=backup_path)
    await async_run_lifecycle_hooks(runtime, client, LifecycleEvent.BEFORE_FILE_WRITE, payload=payload, now=now)
    _require_profile_snapshot(runtime, profile, generation)

    # Hooks may perform network work or wait for another system. Recheck the
    # live server immediately before the first mutation instead of trusting the
    # snapshot used to build the preview.
    await _refresh_and_require_state(runtime, declaration, refresh_status)
    _require_profile_snapshot(runtime, profile, generation)

    compare_and_swap = getattr(client, "write_text_compare_and_swap", None)
    if not bool(getattr(client, "supports_authoritative_file_transactions", True)):
        compare_and_swap = None
    if callable(compare_and_swap):
        try:
            await compare_and_swap(
                runtime.state.identity.service_id,
                preview.path,
                preview.proposed_text,
                expected_content=preview.current_text,
                backup_path=backup_path,
            )
            _require_profile_snapshot(runtime, profile, generation)
        except asyncio.CancelledError:
            raise
        except ProfileExtensionError:
            raise
        except Exception:
            _LOGGER.exception("Editable-file compare-and-swap failed for %s", declaration.key)
            raise ProfileExtensionError(
                blocked(f"{declaration.name} changed or could not be written safely; details were logged.")
            ) from None
        try:
            await async_run_lifecycle_hooks(
                runtime,
                client,
                LifecycleEvent.AFTER_FILE_WRITE,
                payload=payload,
                raise_blocking=False,
                now=now,
            )
            _require_profile_snapshot(runtime, profile, generation)
        except (asyncio.CancelledError, ProfileExtensionError) as err:
            restore_error = await _await_cleanup_despite_repeated_cancellation(
                _compare_and_swap_restore(
                    client,
                    runtime.state.identity.service_id,
                    preview.path,
                    preview.current_text,
                    expected_content=preview.proposed_text,
                )
            )
            if restore_error is not None:
                raise ProfileExtensionError(
                    blocked(f"{declaration.name} mutation was cancelled and automatic rollback failed.")
                ) from None
            if isinstance(err, ProfileExtensionError):
                raise ProfileExtensionError(blocked(f"{err.verdict.reason}; automatic rollback verified.")) from None
            raise
        return EditableFileApplyResult(preview=preview, wrote=True, backup_path=backup_path)

    current_text = await _download_authoritative(client, runtime.state.identity.service_id, preview.path)
    if current_text != preview.current_text:
        raise ProfileExtensionError(
            blocked(f"{declaration.name} changed after preview; apply was aborted before writing.")
        )

    _require_profile_snapshot(runtime, profile, generation)
    if declaration.create_backup:
        try:
            await _upload_verified(
                client,
                runtime.state.identity.service_id,
                backup_path,
                preview.current_text,
                label="backup",
            )
            _require_profile_snapshot(runtime, profile, generation)
        except ProfileExtensionError:
            raise
        except Exception:
            _LOGGER.exception("Editable-file backup verification failed for %s", declaration.key)
            raise ProfileExtensionError(
                blocked(f"{declaration.name} backup verification failed safely; details were logged.")
            ) from None

        # Backup creation is itself remote I/O. Refuse to overwrite a target
        # changed by Nitrado or another administrator while that backup was
        # being uploaded and verified.
        latest_text = await _download_authoritative(client, runtime.state.identity.service_id, preview.path)
        _require_profile_snapshot(runtime, profile, generation)
        if latest_text != current_text:
            raise ProfileExtensionError(
                blocked(f"{declaration.name} changed while its backup was created; apply was aborted.")
            )

    try:
        await _upload_verified(
            client,
            runtime.state.identity.service_id,
            preview.path,
            preview.proposed_text,
            label="target",
        )
        _require_profile_snapshot(runtime, profile, generation)
    except BaseException as err:
        restore = _restore_after_failed_write(
            client,
            runtime.state.identity.service_id,
            preview.path,
            preview.current_text,
        )
        rollback_error = (
            await _await_cleanup_despite_repeated_cancellation(restore)
            if isinstance(err, asyncio.CancelledError)
            else await restore
        )
        if rollback_error is None:
            detail = "automatic rollback verified"
        else:
            _LOGGER.error(
                "Editable-file automatic rollback failed for %s",
                declaration.key,
                exc_info=rollback_error,
            )
            detail = "automatic rollback also failed; details were logged"
        _LOGGER.exception("Editable-file target verification failed for %s", declaration.key)
        if isinstance(err, asyncio.CancelledError):
            if rollback_error is not None:
                raise ProfileExtensionError(
                    blocked(f"{declaration.name} mutation was cancelled and automatic rollback failed.")
                ) from None
            raise
        if isinstance(err, ProfileExtensionError):
            raise ProfileExtensionError(blocked(f"{err.verdict.reason}; {detail}.")) from None
        raise ProfileExtensionError(blocked(f"{declaration.name} write verification failed; {detail}.")) from None
    try:
        await async_run_lifecycle_hooks(
            runtime,
            client,
            LifecycleEvent.AFTER_FILE_WRITE,
            payload=payload,
            raise_blocking=False,
            now=now,
        )
        _require_profile_snapshot(runtime, profile, generation)
    except (asyncio.CancelledError, ProfileExtensionError) as err:
        rollback_error = await _await_cleanup_despite_repeated_cancellation(
            _restore_after_failed_write(
                client,
                runtime.state.identity.service_id,
                preview.path,
                preview.current_text,
            )
        )
        if rollback_error is not None:
            raise ProfileExtensionError(
                blocked(f"{declaration.name} mutation was cancelled and automatic rollback failed.")
            ) from None
        if isinstance(err, ProfileExtensionError):
            raise ProfileExtensionError(blocked(f"{err.verdict.reason}; automatic rollback verified.")) from None
        raise
    return EditableFileApplyResult(preview=preview, wrote=True, backup_path=backup_path)


async def async_rollback_editable_file(
    runtime: ServiceRuntime,
    client: NitradoClient,
    file_key: str,
    backup_path: str,
    *,
    now: int | None = None,
    refresh_status: Callable[[], Awaitable[Any]] | None = None,
) -> EditableFileRollbackResult:
    """Restore an editable file from a previous backup path."""

    profile, generation = _profile_snapshot(runtime)
    declaration = await _editable_file_declaration(runtime, file_key)
    await _refresh_and_require_state(runtime, declaration, refresh_status)
    _require_profile_snapshot(runtime, profile, generation)
    snapshot = await async_read_editable_file(runtime, client, file_key, now=now)
    backup_path = _validated_backup_path(declaration, snapshot.path, backup_path)
    backup_text = await _download_authoritative(client, runtime.state.identity.service_id, backup_path)
    parsed = await _parse(runtime, declaration, backup_text)
    validation = await _validate_editable_file(runtime, client, declaration, parsed, backup_text, now=now)
    _require_profile_snapshot(runtime, profile, generation)
    if not validation.allowed:
        raise ProfileExtensionError(validation)

    pre_rollback_backup_path = (
        _backup_path(snapshot.path, now=now, suffix="pre-rollback") if declaration.create_backup else None
    )
    payload = _rollback_payload(snapshot, backup_path=backup_path, pre_rollback_backup_path=pre_rollback_backup_path)
    await async_run_lifecycle_hooks(runtime, client, LifecycleEvent.BEFORE_RESTORE, payload=payload, now=now)
    _require_profile_snapshot(runtime, profile, generation)

    await _refresh_and_require_state(runtime, declaration, refresh_status)
    _require_profile_snapshot(runtime, profile, generation)

    # Preserve the content that exists immediately before rollback. Hooks may
    # wait or perform network work, so the earlier snapshot is not safe to use
    # as the recovery copy.
    live_target_text = await _download_authoritative(client, runtime.state.identity.service_id, snapshot.path)
    _require_profile_snapshot(runtime, profile, generation)

    compare_and_swap = getattr(client, "write_text_compare_and_swap", None)
    if not bool(getattr(client, "supports_authoritative_file_transactions", True)):
        compare_and_swap = None
    if callable(compare_and_swap):
        try:
            await compare_and_swap(
                runtime.state.identity.service_id,
                snapshot.path,
                backup_text,
                expected_content=live_target_text,
                backup_path=pre_rollback_backup_path,
            )
            _require_profile_snapshot(runtime, profile, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Editable-file rollback compare-and-swap failed for %s", declaration.key)
            raise ProfileExtensionError(
                blocked(f"{declaration.name} changed or could not be rolled back safely; details were logged.")
            ) from None
        try:
            await async_run_lifecycle_hooks(
                runtime,
                client,
                LifecycleEvent.AFTER_RESTORE,
                payload=payload,
                raise_blocking=False,
                now=now,
            )
            _require_profile_snapshot(runtime, profile, generation)
        except (asyncio.CancelledError, ProfileExtensionError) as err:
            restore_error = await _await_cleanup_despite_repeated_cancellation(
                _compare_and_swap_restore(
                    client,
                    runtime.state.identity.service_id,
                    snapshot.path,
                    live_target_text,
                    expected_content=backup_text,
                )
            )
            if restore_error is not None:
                raise ProfileExtensionError(
                    blocked(f"{declaration.name} rollback was cancelled and original-content restore failed.")
                ) from None
            if isinstance(err, ProfileExtensionError):
                raise ProfileExtensionError(
                    blocked(f"{err.verdict.reason}; original content restored and verified.")
                ) from None
            raise
        return EditableFileRollbackResult(
            key=declaration.key,
            name=declaration.name,
            path=snapshot.path,
            backup_path=backup_path,
            pre_rollback_backup_path=pre_rollback_backup_path,
            wrote=True,
            source_revision=_text_revision(live_target_text),
            resulting_revision=_text_revision(backup_text),
        )

    if declaration.create_backup:
        try:
            await _upload_verified(
                client,
                runtime.state.identity.service_id,
                pre_rollback_backup_path,
                live_target_text,
                label="pre-rollback backup",
            )
            _require_profile_snapshot(runtime, profile, generation)
        except ProfileExtensionError:
            raise
        except Exception:
            _LOGGER.exception("Editable-file pre-rollback backup verification failed for %s", declaration.key)
            raise ProfileExtensionError(
                blocked(f"{declaration.name} pre-rollback backup verification failed safely; details were logged.")
            ) from None

        latest_target_text = await _download_authoritative(client, runtime.state.identity.service_id, snapshot.path)
        _require_profile_snapshot(runtime, profile, generation)
        if latest_target_text != live_target_text:
            raise ProfileExtensionError(
                blocked(f"{declaration.name} changed while its pre-rollback backup was created; rollback was aborted.")
            )

    try:
        await _upload_verified(
            client,
            runtime.state.identity.service_id,
            snapshot.path,
            backup_text,
            label="rollback target",
        )
        _require_profile_snapshot(runtime, profile, generation)
    except BaseException as err:
        restore = _restore_after_failed_write(
            client,
            runtime.state.identity.service_id,
            snapshot.path,
            live_target_text,
        )
        restore_error = (
            await _await_cleanup_despite_repeated_cancellation(restore)
            if isinstance(err, asyncio.CancelledError)
            else await restore
        )
        if restore_error is None:
            detail = "original content restored and verified"
        else:
            _LOGGER.error(
                "Editable-file original-content restore failed for %s",
                declaration.key,
                exc_info=restore_error,
            )
            detail = "original-content restore also failed; details were logged"
        if isinstance(err, asyncio.CancelledError):
            if restore_error is not None:
                raise ProfileExtensionError(
                    blocked(f"{declaration.name} rollback was cancelled and original-content restore failed.")
                ) from None
            raise
        _LOGGER.exception("Editable-file rollback target verification failed for %s", declaration.key)
        raise ProfileExtensionError(blocked(f"{declaration.name} rollback verification failed; {detail}.")) from None
    try:
        await async_run_lifecycle_hooks(
            runtime,
            client,
            LifecycleEvent.AFTER_RESTORE,
            payload=payload,
            raise_blocking=False,
            now=now,
        )
        _require_profile_snapshot(runtime, profile, generation)
    except (asyncio.CancelledError, ProfileExtensionError) as err:
        restore_error = await _await_cleanup_despite_repeated_cancellation(
            _restore_after_failed_write(
                client,
                runtime.state.identity.service_id,
                snapshot.path,
                live_target_text,
            )
        )
        if restore_error is not None:
            raise ProfileExtensionError(
                blocked(f"{declaration.name} rollback was cancelled and original-content restore failed.")
            ) from None
        if isinstance(err, ProfileExtensionError):
            raise ProfileExtensionError(
                blocked(f"{err.verdict.reason}; original content restored and verified.")
            ) from None
        raise
    return EditableFileRollbackResult(
        key=declaration.key,
        name=declaration.name,
        path=snapshot.path,
        backup_path=backup_path,
        pre_rollback_backup_path=pre_rollback_backup_path,
        wrote=True,
        source_revision=_text_revision(live_target_text),
        resulting_revision=_text_revision(backup_text),
    )


async def _editable_file_declaration(runtime: ServiceRuntime, file_key: str) -> EditableFileDeclaration:
    profile = runtime.profile
    if profile is None:
        raise ProfileExtensionError(blocked("No game profile is selected for this service."))
    try:
        manifest = runtime_profile_extension_manifest(runtime)
    except ProfileManifestError as err:
        log_profile_failure(
            _LOGGER,
            "editable_manifest",
            err,
            profile_id=getattr(profile, "profile_id", "unknown"),
            key=file_key,
        )
        raise ProfileExtensionError(
            blocked("The selected game profile manifest is invalid; details were logged.")
        ) from None
    declaration = next((file for file in manifest.editable_files if file.key == file_key), None)
    if declaration is None:
        raise ProfileExtensionError(blocked(f"Profile editable file not found: {file_key}"))
    return declaration


def _profile_snapshot(runtime: ServiceRuntime) -> tuple[GameProfile, tuple[int, int]]:
    """Capture profile identity plus runtime and whole-registry generations."""

    profile = runtime.profile
    if profile is None:
        raise ProfileExtensionError(blocked("No game profile is selected for this service."))
    return profile, (runtime.profile_generation, runtime.profile_registry_generation_seen)


def _require_profile_snapshot(
    runtime: ServiceRuntime,
    profile: GameProfile,
    generation: tuple[int, int],
) -> None:
    """Fail closed if profile selection changes before a file mutation."""

    profile_generation, registry_generation = generation
    if (
        runtime.profile is not profile
        or runtime.profile_generation != profile_generation
        or runtime.profile_registry_generation_seen != registry_generation
        or registry_generation != profile_registry_generation()
    ):
        raise ProfileExtensionError(
            blocked("The selected game profile changed during the file operation; retry against the current profile.")
        )


async def _resolve_path(
    runtime: ServiceRuntime,
    client: NitradoClient,
    declaration: EditableFileDeclaration,
    *,
    now: int | None,
) -> str:
    context = profile_action_context(client, runtime, now=now)
    try:
        path = await async_invoke_profile(declaration.path_fn, context)
    except ProfileExtensionError:
        raise
    except Exception as err:
        log_profile_failure(
            _LOGGER,
            "editable_path",
            err,
            profile_id=getattr(runtime.profile, "profile_id", "unknown"),
            key=declaration.key,
        )
        raise ProfileExtensionError(
            blocked(f"{declaration.name} path resolver failed safely; details were logged.")
        ) from None
    if type(path) is not str or not path.strip():
        raise ProfileExtensionError(blocked(f"Profile editable file path unavailable: {declaration.key}"))
    return path.strip()


async def _validate_editable_file(
    runtime: ServiceRuntime,
    client: NitradoClient,
    declaration: EditableFileDeclaration,
    parsed: Any,
    proposed_text: str,
    *,
    now: int | None,
) -> CapabilityVerdict:
    if declaration.validator is not None:
        try:
            verdict = await async_invoke_profile(declaration.validator, parsed)
        except Exception as err:
            log_profile_failure(
                _LOGGER,
                "editable_validator",
                err,
                profile_id=getattr(runtime.profile, "profile_id", "unknown"),
                key=declaration.key,
            )
            return blocked(f"Profile editable-file validator {declaration.key} failed safely; details were logged.")
        if not valid_capability_verdict(verdict):
            return blocked(f"Profile editable-file validator {declaration.key} returned an invalid verdict")
        if not verdict.allowed:
            return verdict

    return await async_validate_profile(
        runtime,
        client,
        target=ValidatorTarget.EDITABLE_FILE,
        validator_keys=declaration.validators,
        payload={"file_key": declaration.key, "parsed": parsed, "proposed_text": proposed_text},
        now=now,
    )


async def _parse(runtime: ServiceRuntime, declaration: EditableFileDeclaration, text: str) -> Any:
    if declaration.parser is None:
        return text
    try:
        return await async_invoke_profile(declaration.parser, text)
    except Exception as err:
        log_profile_failure(
            _LOGGER,
            "editable_parser",
            err,
            profile_id="unknown",
            key=declaration.key,
        )
        raise ProfileExtensionError(blocked(f"{declaration.name} parser failed safely; details were logged.")) from None


async def _proposed_text(
    runtime: ServiceRuntime,
    declaration: EditableFileDeclaration,
    proposed_value: Any,
    *,
    source_text: str,
    value_is_parsed: bool,
) -> tuple[Any, str]:
    if value_is_parsed:
        if declaration.editor_patcher is not None:
            try:
                text = await async_invoke_profile(declaration.editor_patcher, source_text, proposed_value)
            except Exception as err:
                log_profile_failure(
                    _LOGGER,
                    "editable_patcher",
                    err,
                    profile_id="unknown",
                    key=declaration.key,
                )
                raise ProfileExtensionError(
                    blocked(f"{declaration.name} structured edit was rejected safely; details were logged.")
                ) from None
            if type(text) is not str:
                raise ProfileExtensionError(blocked(f"{declaration.name} structured editor returned non-text content"))
            parsed = await _parse(runtime, declaration, text)
            return parsed, await _serialize(declaration, parsed)
        parsed = proposed_value
        text = await _serialize(declaration, parsed)
        return parsed, text
    text = str(proposed_value)
    parsed = await _parse(runtime, declaration, text)
    return parsed, await _serialize(declaration, parsed)


async def _editor_model(
    runtime: ServiceRuntime,
    declaration: EditableFileDeclaration,
    text: str,
) -> Any | None:
    if declaration.editor_modeler is None:
        return None
    try:
        model = await async_invoke_profile(declaration.editor_modeler, text)
        return await async_run_profile_sync(
            _editable_worker_identity(runtime, "editable_model_output", declaration.key),
            _detach_editable_json,
            model,
            timeout=15.0,
        )
    except Exception as err:
        log_profile_failure(
            _LOGGER,
            "editable_modeler",
            err,
            profile_id="unknown",
            key=declaration.key,
        )
        raise ProfileExtensionError(
            blocked(f"{declaration.name} structured editor could not load safely; details were logged.")
        ) from None


async def _public_editable_value(
    runtime: ServiceRuntime,
    declaration: EditableFileDeclaration,
    value: Any,
) -> Any | None:
    """Return a detached JSON representation without constraining private parser objects."""

    try:
        return await async_run_profile_sync(
            _editable_worker_identity(runtime, "editable_public_output", declaration.key),
            _detach_editable_json,
            value,
            timeout=15.0,
        )
    except Exception:
        _LOGGER.warning(
            "Profile editable-file parser output is not transport-safe profile=%s key=%s",
            getattr(runtime.profile, "profile_id", "unknown"),
            declaration.key,
        )
        return None


def _editable_worker_identity(runtime: ServiceRuntime, phase: str, key: str) -> tuple[Any, ...]:
    """Scope output-worker quarantine to one service and profile generation."""

    return (
        phase,
        runtime.state.identity.service_id,
        getattr(runtime.profile, "profile_id", "unknown"),
        runtime.profile_generation,
        runtime.profile_registry_generation_seen,
        key,
    )


async def _serialize(declaration: EditableFileDeclaration, parsed: Any) -> str:
    if declaration.serializer is None:
        if type(parsed) is not str:
            raise ProfileExtensionError(blocked(f"{declaration.name} requires an explicit serializer"))
        return parsed
    try:
        text = await async_invoke_profile(declaration.serializer, parsed)
    except Exception as err:
        log_profile_failure(
            _LOGGER,
            "editable_serializer",
            err,
            profile_id="unknown",
            key=declaration.key,
        )
        raise ProfileExtensionError(
            blocked(f"{declaration.name} serializer failed safely; details were logged.")
        ) from None
    if type(text) is not str:
        raise ProfileExtensionError(blocked(f"{declaration.name} serializer returned non-text content"))
    return text


def _detach_editable_json(value: Any) -> Any:
    """Detach one bounded parser/model result before it reaches HA surfaces."""

    encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 1_048_576:
        raise ValueError("editable profile output exceeds the supported size")
    return json.loads(encoded)


def _state_prerequisite(runtime: ServiceRuntime, declaration: EditableFileDeclaration) -> CapabilityVerdict:
    if not runtime.status_fresh or runtime.using_cached_data or runtime.server is None:
        return blocked(f"{declaration.name} requires a fresh, non-cached server status before editing.")
    status = runtime.server.raw_status if runtime.server else None
    if declaration.requires_stopped and status != STOPPED_STATUS:
        return blocked(f"{declaration.name} can only be edited while the server is stopped.")
    if declaration.requires_running and status != RUNNING_STATUS:
        return blocked(f"{declaration.name} can only be edited while the server is running.")
    return SUPPORTED


async def _redact(declaration: EditableFileDeclaration, text: str) -> str:
    if declaration.redactor is not None:
        try:
            redacted = await async_invoke_profile(declaration.redactor, text)
        except Exception as err:
            log_profile_failure(
                _LOGGER,
                "editable_redactor",
                err,
                profile_id="unknown",
                key=declaration.key,
            )
            raise ProfileExtensionError(
                blocked(f"{declaration.name} redactor failed safely; details were logged.")
            ) from None
        if type(redacted) is not str:
            raise ProfileExtensionError(blocked(f"{declaration.name} redactor returned non-text content"))
        return redacted
    return redact_editable_text(text)


def redact_editable_text(text: str) -> str:
    """Redact common secret-looking settings from editable text."""

    redacted = text
    for pattern in (
        r'(?i)(password\s*=\s*)("[^"]*"|[^,\)\r\n]*)',
        r'(?i)(adminpassword\s*=\s*)("[^"]*"|[^,\)\r\n]*)',
        r'(?i)(token\s*=\s*)("[^"]*"|[^,\)\r\n]*)',
        r'(?i)(secret\s*=\s*)("[^"]*"|[^,\)\r\n]*)',
    ):
        redacted = re.sub(pattern, r"\1[redacted]", redacted)
    return redacted


def _unified_diff(current: str, proposed: str, *, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def _text_revision(text: str) -> str:
    """Return a stable non-secret revision for compare-and-preview binding."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _backup_path(path: str, *, now: int | None, suffix: str) -> str:
    epoch = int(now if now is not None else time.time())
    stamp = f"{epoch}-{secrets.token_hex(8)}"
    return f"{path}.nitrado_gameserver.{suffix}.{stamp}"


def _validated_backup_path(declaration: EditableFileDeclaration, target_path: str, backup_path: str) -> str:
    """Return a generated backup path only when it belongs to this editable file."""

    normalized = str(backup_path or "").strip().replace("\\", "/")
    prefix = f"{target_path}.nitrado_gameserver."
    if not normalized.startswith(prefix):
        raise ProfileExtensionError(
            blocked(f"{declaration.name} rollback path is not a generated backup for this file.")
        )

    suffix = normalized[len(prefix) :]
    kind, separator, stamp = suffix.partition(".")
    if (
        separator != "."
        or kind not in {"backup", "pre-rollback"}
        or re.fullmatch(r"\d+(?:-[0-9a-f]{16})?", stamp) is None
    ):
        raise ProfileExtensionError(
            blocked(f"{declaration.name} rollback path is not a generated backup for this file.")
        )
    return normalized


async def _refresh_and_require_state(
    runtime: ServiceRuntime,
    declaration: EditableFileDeclaration,
    refresh_status: Callable[[], Awaitable[Any]] | None,
) -> None:
    """Refresh when possible and fail closed unless mutation state is fresh."""

    if refresh_status is not None:
        try:
            await refresh_status()
        except Exception:
            _LOGGER.exception("Editable-file status refresh failed for %s", declaration.key)
            raise ProfileExtensionError(
                blocked(f"{declaration.name} could not refresh server status; details were logged.")
            ) from None
    verdict = _state_prerequisite(runtime, declaration)
    if not verdict.allowed:
        raise ProfileExtensionError(verdict)


async def _upload_verified(
    client: NitradoClient,
    service_id: str,
    path: str,
    content: str,
    *,
    label: str,
) -> None:
    """Upload text and verify the exact remote bytes through read-back."""

    await client.upload_text_file(service_id, path, content)
    remote = await _download_authoritative(client, service_id, path)
    if remote != content:
        raise RuntimeError(f"{label} read-back did not match uploaded content")


async def _download_authoritative(client: NitradoClient, service_id: str, path: str) -> str:
    """Prefer the core FTP authority while retaining lightweight test doubles."""

    reader = getattr(client, "download_text_file_authoritative", None)
    if callable(reader) and bool(getattr(client, "supports_authoritative_file_transactions", True)):
        return await reader(service_id, path)
    return await client.download_file(service_id, path)


async def _restore_after_failed_write(
    client: NitradoClient,
    service_id: str,
    path: str,
    original: str,
) -> Exception | None:
    """Best-effort restore of the pre-write content, including verification."""

    try:
        await _upload_verified(client, service_id, path, original, label="automatic rollback")
    except Exception as err:  # The caller reports both the primary and recovery failures.
        return err
    return None


async def _compare_and_swap_restore(
    client: NitradoClient,
    service_id: str,
    path: str,
    original: str,
    *,
    expected_content: str,
) -> Exception | None:
    """Restore only if the file still contains this operation's result."""

    compare_and_swap = getattr(client, "write_text_compare_and_swap", None)
    if not callable(compare_and_swap):
        return RuntimeError("authoritative compare-and-swap transport is unavailable")
    try:
        await compare_and_swap(
            service_id,
            path,
            original,
            expected_content=expected_content,
            backup_path=None,
        )
    except Exception as err:
        return err
    return None


async def _await_cleanup_despite_repeated_cancellation(awaitable: Awaitable[Any]) -> Any:
    """Drain critical recovery even if the parent task is cancelled again.

    The original cancellation is re-raised by the caller after recovery. Each
    later cancellation request is consumed only long enough to let the
    separately owned recovery task finish and verify the remote file.
    """

    cleanup_task = asyncio.create_task(awaitable, name="nitrado-editable-file-recovery")
    while True:
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if cleanup_task.done():
                return cleanup_task.result()


def _payload(preview: EditableFilePreview, *, backup_path: str | None) -> dict[str, Any]:
    return {
        "file_key": preview.key,
        "path": preview.path,
        "backup_path": backup_path,
        "requires_restart": preview.requires_restart,
        "changed": preview.changed,
    }


def _rollback_payload(
    snapshot: EditableFileSnapshot,
    *,
    backup_path: str,
    pre_rollback_backup_path: str | None,
) -> dict[str, Any]:
    return {
        "file_key": snapshot.key,
        "path": snapshot.path,
        "backup_path": backup_path,
        "pre_rollback_backup_path": pre_rollback_backup_path,
        "requires_restart": snapshot.requires_restart,
        "changed": True,
    }
