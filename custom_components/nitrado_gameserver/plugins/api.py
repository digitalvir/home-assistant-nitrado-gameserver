"""Stable public API for separately packaged Nitrado game profiles."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from ..api.nitrado import (
    NitradoApiError,
    NitradoService,
    ParsedServer,
    first_string,
    parse_int,
)
from ..cockpit import (
    async_prepare_cockpit_assets,
    ensure_cockpit_assets_registerable,
    publish_cockpit_epoch,
    register_cockpit_assets,
    unregister_cockpit_assets,
)
from ..const import DOMAIN, PROFILE_REGISTRY_TRANSITION_DATA_KEY, RUNNING_STATUS, TRANSITION_STATUSES
from ..save_bundle_jobs import async_cancel_save_bundle_jobs
from .base import (
    COCKPIT_API_VERSION,
    PROFILE_API_VERSION,
    SUPPORTED,
    ActionDeclaration,
    ActionInputDeclaration,
    ActionInputType,
    BaseGameProfile,
    CapabilityState,
    CapabilityVerdict,
    CockpitDeclaration,
    CockpitSnapshotContext,
    ControlContext,
    DataSource,
    EditableFileDeclaration,
    EntityDeclaration,
    ExtensionAccess,
    GameProfile,
    LifecycleEvent,
    LifecycleHookDeclaration,
    LocalCockpitAsset,
    MatchResult,
    ProfileActionContext,
    ProfileEntityContext,
    ProfileOptionDeclaration,
    ProfileOptionType,
    ProfileReadTransport,
    ProfileStatus,
    ResourceContentFamily,
    ResourceDeclaration,
    SaveBundleDeclaration,
    SurfaceDeclaration,
    ValidatorDeclaration,
    ValidatorDomain,
    ValidatorTarget,
    blocked,
    unsupported,
    validate_action_payload,
    validate_profile_option_value,
)
from .registry import (
    PROFILE_REGISTRATION_TIMEOUT_SECONDS,
)
from .registry import (
    commit_profile_registration as _commit_profile_registration,
)
from .registry import (
    prepare_profile_registration as _prepare_profile_registration,
)

_TRANSITION_DATA_KEY = PROFILE_REGISTRY_TRANSITION_DATA_KEY


def _complete_registration_validation(
    loop: asyncio.AbstractEventLoop,
    future: asyncio.Future[Any],
    export: Any,
) -> None:
    """Validate one external export on an isolated daemon worker."""

    try:
        result = _prepare_profile_registration(export)
    except Exception as err:  # noqa: BLE001 - propagate arbitrary plugin failure safely.
        callback = future.set_exception
        value = err
    except BaseException:  # noqa: BLE001 - never inject SystemExit/KeyboardInterrupt into HA's loop.
        callback = future.set_exception
        value = RuntimeError("External profile registration terminated abnormally")
    else:
        callback = future.set_result
        value = result
    try:
        loop.call_soon_threadsafe(callback, value)
    except RuntimeError:
        # Home Assistant may be shutting down after a timed-out factory.
        return


async def _async_prepare_external_profile(hass: Any, export: Any) -> Any:
    """Run one bounded registration probe without consuming HA's executor."""

    state = hass.data.setdefault(_TRANSITION_DATA_KEY, {})
    slot_lock = state.get("validation_slot_lock")
    if not isinstance(slot_lock, asyncio.Lock):
        slot_lock = asyncio.Lock()
        state["validation_slot_lock"] = slot_lock

    async with slot_lock:
        active = state.get("validation_future")
        if isinstance(active, asyncio.Future) and not active.done():
            # The only way the slot lock can be free while work is active is
            # that the previous caller timed out. Do not queue more daemon
            # workers behind a factory that has failed to return.
            raise RuntimeError("Another external profile factory validation is still running")

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        state["validation_future"] = future

        def clear_active(completed: asyncio.Future[Any]) -> None:
            if not completed.cancelled():
                completed.exception()
            if state.get("validation_future") is completed:
                state.pop("validation_future", None)

        future.add_done_callback(clear_active)
        worker = threading.Thread(
            target=_complete_registration_validation,
            args=(loop, future, export),
            name="nitrado-profile-registration",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:  # noqa: BLE001 - normalize thread resource failures.
            future.cancel()
            if state.get("validation_future") is future:
                state.pop("validation_future", None)
            raise RuntimeError("External profile registration worker could not be started") from None
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=PROFILE_REGISTRATION_TIMEOUT_SECONDS,
            )
        except TimeoutError as err:
            raise TimeoutError("Profile registration validation exceeded its execution budget") from err


async def async_register_profile(
    hass: Any,
    export: Any,
    *,
    owner_domain: str | None = None,
    cockpit_assets: dict[str, LocalCockpitAsset] | None = None,
) -> Callable[[], Awaitable[None]]:
    """Register a companion profile and reload active Nitrado consumers.

    The returned async callback is suitable for ``entry.async_on_unload``.
    Registration changes are applied immediately instead of waiting for the
    next account poll, which may be many hours away.
    """

    _ensure_registry_transition_not_reentrant(hass)
    prepared = await _async_prepare_external_profile(hass, export)
    manifest = prepared.manifest
    records = await async_prepare_cockpit_assets(
        hass,
        owner_domain=owner_domain or "",
        profile_id=manifest.profile_id,
        declaration=manifest.cockpit,
        assets=cockpit_assets,
        export=export,
    )
    lock = _profile_registry_transition_lock(hass)
    async with lock:
        _ensure_registry_transition_not_reentrant(hass)
        entry_ids = _loaded_entry_ids(hass)
        transition_state = hass.data.setdefault(_TRANSITION_DATA_KEY, {})
        transition_state["save_job_admission_blocked"] = True
        try:
            await async_cancel_save_bundle_jobs(hass)
            coordinators = await _async_quiesce_loaded_entries(hass)
        finally:
            transition_state["save_job_admission_blocked"] = False
        try:
            # This check must happen under the registry transition lock and
            # before the profile generation changes.  A collision discovered
            # after commit would make every quiesced coordinator stale even
            # if the new registration were immediately unregistered.
            ensure_cockpit_assets_registerable(hass, records)
            registration = _commit_profile_registration(prepared)
            register_cockpit_assets(hass, records)
        except BaseException as err:
            if "registration" in locals():
                registration.unregister()
                await _recover_or_raise(
                    err,
                    _async_reload_quiesced_entries(hass, coordinators, entry_ids),
                    "Profile registration commit failed and its generation recovery also failed",
                )
            else:
                _resume_quiesced_coordinators(coordinators)
            raise
        try:
            await _async_reload_entries(hass, entry_ids)
        except BaseException as err:
            unregister_cockpit_assets(hass, records)
            await _recover_or_raise(
                err,
                _async_rollback_registration(hass, registration, coordinators, entry_ids),
                "Profile registration failed and its rollback also failed",
            )
            raise
        publish_cockpit_epoch(hass, "profile_registered")
    active = True

    async def async_unregister() -> None:
        nonlocal active
        if not active:
            return
        _ensure_registry_transition_not_reentrant(hass)
        async with lock:
            if not active:
                return
            entry_ids = _loaded_entry_ids(hass)
            transition_state = hass.data.setdefault(_TRANSITION_DATA_KEY, {})
            transition_state["save_job_admission_blocked"] = True
            try:
                await async_cancel_save_bundle_jobs(hass)
                coordinators = await _async_quiesce_loaded_entries(hass)
            finally:
                transition_state["save_job_admission_blocked"] = False
            registration.unregister()
            unregister_cockpit_assets(hass, records)
            try:
                await _async_reload_entries(hass, entry_ids)
            except BaseException as err:
                register_cockpit_assets(hass, records)
                await _recover_or_raise(
                    err,
                    _async_rollback_unregistration(hass, registration, coordinators, entry_ids),
                    "Profile removal failed and its rollback also failed",
                )
                raise
            active = False
            publish_cockpit_epoch(hass, "profile_unregistered")

    return async_unregister


def _profile_registry_transition_lock(hass: Any) -> asyncio.Lock:
    """Return the one serialized public-registry transition lock for this HA."""

    state = hass.data.setdefault(_TRANSITION_DATA_KEY, {})
    lock = state.get("lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        state["lock"] = lock
    return lock


@asynccontextmanager
async def profile_registry_setup_guard(hass: Any, entry_id: str):
    """Serialize ordinary entry setup against public registry transitions.

    Reloads owned by the active transition are allowed through because the
    transition itself is waiting for those exact config entries to set up.
    """

    state = hass.data.setdefault(_TRANSITION_DATA_KEY, {})
    owned_entry_ids = state.get("transition_reload_entry_ids", set())
    if entry_id in owned_entry_ids:
        yield
        return
    lock = _profile_registry_transition_lock(hass)
    async with lock:
        yield


def _loaded_coordinators(hass: Any) -> tuple[Any, ...]:
    """Return coordinators whose entries currently own active runtime state."""

    from homeassistant.config_entries import ConfigEntryState

    return tuple(
        coordinator
        for entry in tuple(hass.config_entries.async_entries(DOMAIN))
        if entry.state is ConfigEntryState.LOADED
        if (coordinator := hass.data.get(DOMAIN, {}).get(entry.entry_id)) is not None
    )


def _loaded_entry_ids(hass: Any) -> tuple[str, ...]:
    """Capture the complete entry set owned by one registry transition."""

    from homeassistant.config_entries import ConfigEntryState

    return tuple(
        entry.entry_id
        for entry in tuple(hass.config_entries.async_entries(DOMAIN))
        if entry.state is ConfigEntryState.LOADED
    )


def _ensure_registry_transition_not_reentrant(hass: Any) -> None:
    """Reject callback-originated transitions before they wait on the global lock."""

    for coordinator in _loaded_coordinators(hass):
        coordinator.ensure_profile_registry_transition_allowed()


async def _async_quiesce_loaded_entries(hass: Any) -> tuple[Any, ...]:
    """Block new work and drain/cancel profile-dependent work before a registry change."""

    coordinators = _loaded_coordinators(hass)
    for coordinator in coordinators:
        coordinator.ensure_profile_registry_transition_allowed()

    started: list[Any] = []
    try:
        for coordinator in coordinators:
            started.append(coordinator)
            await coordinator.async_quiesce_profile_registry_change()
    except BaseException:
        _resume_quiesced_coordinators(tuple(started))
        raise
    return coordinators


def _resume_quiesced_coordinators(coordinators: tuple[Any, ...]) -> None:
    """Resume still-loaded coordinators when a registry transition aborts."""

    for coordinator in coordinators:
        coordinator.resume_after_failed_shutdown()


async def _async_reload_entries(hass: Any, entry_ids: tuple[str, ...]) -> None:
    """Reload every entry captured by the transition, even after partial failure."""

    state = hass.data.setdefault(_TRANSITION_DATA_KEY, {})
    owned_entry_ids = state.setdefault("transition_reload_entry_ids", set())
    owned_entry_ids.update(entry_ids)
    try:
        for entry_id in entry_ids:
            if not await hass.config_entries.async_reload(entry_id):
                raise RuntimeError(f"Failed to reload Nitrado entry {entry_id} after profile registry change")
    finally:
        owned_entry_ids.difference_update(entry_ids)


async def _async_reload_quiesced_entries(
    hass: Any,
    coordinators: tuple[Any, ...],
    entry_ids: tuple[str, ...],
) -> None:
    """Reload without exposing generation-stale coordinators to new work."""

    try:
        await _async_reload_entries(hass, entry_ids)
    finally:
        loaded = tuple(hass.data.get(DOMAIN, {}).values())
        _resume_quiesced_coordinators(
            tuple(coordinator for coordinator in coordinators if any(coordinator is current for current in loaded))
        )


async def _async_rollback_registration(
    hass: Any,
    registration: Any,
    coordinators: tuple[Any, ...],
    entry_ids: tuple[str, ...],
) -> None:
    """Restore pre-registration state without invoking external factory code."""

    registration.unregister()
    await _async_reload_quiesced_entries(hass, coordinators, entry_ids)


async def _async_rollback_unregistration(
    hass: Any,
    registration: Any,
    coordinators: tuple[Any, ...],
    entry_ids: tuple[str, ...],
) -> None:
    """Restore the exact vetted registration after a failed removal."""

    registration.restore()
    await _async_reload_quiesced_entries(hass, coordinators, entry_ids)


async def _recover_or_raise(original: BaseException, recovery: Awaitable[Any], message: str) -> None:
    """Finish registry recovery despite cancellation and preserve both failures."""

    try:
        await _await_cleanup_despite_repeated_cancellation(recovery)
    except (Exception, asyncio.CancelledError) as recovery_error:  # noqa: BLE001 - preserve both transaction failures
        raise BaseExceptionGroup(message, [original, recovery_error]) from None


async def _await_cleanup_despite_repeated_cancellation(awaitable: Awaitable[Any]) -> Any:
    """Drain an atomic registry recovery even when its caller is cancelled."""

    cleanup_task = asyncio.create_task(awaitable, name="nitrado-profile-registry-recovery")
    while True:
        try:
            return await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            if cleanup_task.done():
                return cleanup_task.result()


__all__ = (
    "COCKPIT_API_VERSION",
    "PROFILE_API_VERSION",
    "RUNNING_STATUS",
    "SUPPORTED",
    "TRANSITION_STATUSES",
    "ActionDeclaration",
    "ActionInputDeclaration",
    "ActionInputType",
    "BaseGameProfile",
    "CapabilityState",
    "CapabilityVerdict",
    "CockpitDeclaration",
    "CockpitSnapshotContext",
    "ControlContext",
    "DataSource",
    "EditableFileDeclaration",
    "EntityDeclaration",
    "ExtensionAccess",
    "GameProfile",
    "LifecycleEvent",
    "LifecycleHookDeclaration",
    "LocalCockpitAsset",
    "MatchResult",
    "NitradoApiError",
    "NitradoService",
    "ParsedServer",
    "ProfileActionContext",
    "ProfileEntityContext",
    "ProfileOptionDeclaration",
    "ProfileOptionType",
    "ProfileReadTransport",
    "ProfileStatus",
    "ResourceContentFamily",
    "ResourceDeclaration",
    "SaveBundleDeclaration",
    "SurfaceDeclaration",
    "ValidatorDeclaration",
    "ValidatorDomain",
    "ValidatorTarget",
    "async_register_profile",
    "blocked",
    "first_string",
    "parse_int",
    "unsupported",
    "validate_action_payload",
    "validate_profile_option_value",
)
