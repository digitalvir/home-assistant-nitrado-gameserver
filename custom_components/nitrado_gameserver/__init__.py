"""Nitrado Game Server integration."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

try:
    from homeassistant.const import Platform
except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.

    class Platform:  # type: ignore[no-redef]
        """Fallback platform names for local import/compile checks."""

        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        BUTTON = "button"
        SWITCH = "switch"
        NUMBER = "number"
        SELECT = "select"


from .api.nitrado import (
    NitradoApiError,
    NitradoAuthError,
    NitradoClient,
    fallback_account_identity,
)
from .cockpit import async_register_builtin_cockpit_assets, publish_cockpit_account_epoch
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_UUID,
    CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS,
    CONF_API_TOKEN,
    CONF_DISCOVERY_INTERVAL,
    CONF_DISCOVERY_MODE,
    CONF_DRY_RUN_SERVICE_IDS,
    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
    CONF_IGNORED_SERVICE_IDS,
    CONF_IMPORTED_SERVICE_IDS,
    CONF_MAINTENANCE_SERVICE_IDS,
    CONF_MISSING_SERVICE_THRESHOLD,
    CONF_SETTLE_SECONDS,
    CONF_STATUS_INTERVAL,
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_DISCOVERY_MODE,
    DEFAULT_MISSING_SERVICE_THRESHOLD,
    DEFAULT_SETTLE_SECONDS,
    DEFAULT_STATUS_INTERVAL,
    DOMAIN,
)
from .coordinator import (
    AccountCoordinatorOptions,
    NitradoAccountCoordinator,
    NitradoControlError,
)
from .discovery import async_update_discovery_flows
from .editable_file_journal import EditableFileJournal, HomeAssistantEditableFileJournalStore
from .filesystem import MAX_TREE_RECOVERY_BLOB_BYTES, NitradoFilesystemService
from .filesystem_journal import FilesystemJournalError, FilesystemTransactionJournal
from .ha_filesystem_storage import HomeAssistantJournalMetadataStore, HomeAssistantRecoveryBlobStore
from .panel import async_register_extension_panel, async_unregister_extension_panel, device_configuration_url
from .plugins.api import profile_registry_setup_guard
from .plugins.base import (
    ExtensionAccess,
    ProfileManifestError,
)
from .plugins.registry import discover_builtin_profiles
from .profile_logging import log_profile_failure
from .provider_api import ProviderServiceRef
from .provider_runtime import (
    async_delete_provider_account_grants,
    async_get_or_create_provider_runtime,
    get_provider_runtime,
)
from .registry_cleanup import (
    async_clear_entry_issues,
    async_clear_service_issue,
    async_remove_entry_registry_entries,
    async_remove_service_registry_entries,
)
from .repairs import async_update_repair_issues
from .runtime import runtime_profile_extension_manifest
from .save_bundle_jobs import async_cancel_save_bundle_jobs
from .service_options import (
    idle_minutes_map,
    normalized_service_options,
    profile_option_acknowledgements_map,
    profile_options_map,
    service_area_id_map,
    service_display_name_map,
    service_id_set,
    startup_cooldown_minutes_map,
    updated_idle_toggle_options,
    updated_service_options,
)
from .storage_cleanup import async_purge_entry_local_data
from .views import async_register_extension_views

_LOGGER = logging.getLogger(__name__)
_MAX_PROFILE_ACTION_PAYLOAD_BYTES = 65_536

PLATFORMS: list[Platform | str] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
]


async def _async_drain_cleanup(awaitable: Any) -> Any:
    """Own cleanup through repeated cancellation before propagating it."""

    task = asyncio.create_task(awaitable)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as err:
            cancelled = err
            continue
    result = task.result()
    if cancelled is not None:
        raise cancelled
    return result


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up one Nitrado account config entry."""

    async with profile_registry_setup_guard(hass, entry.entry_id):
        return await _async_setup_entry_unlocked(hass, entry)


async def _async_setup_entry_unlocked(hass: Any, entry: Any) -> bool:
    """Set up one account while owning or participating in the registry guard."""

    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.event import async_track_time_interval

    builtin_profiles = await hass.async_add_executor_job(discover_builtin_profiles)
    await async_register_builtin_cockpit_assets(
        hass,
        profiles=builtin_profiles,
        owner_root=Path(__file__).parent,
    )
    session = async_get_clientsession(hass)
    client = NitradoClient(session, entry.data[CONF_API_TOKEN])
    await _async_ensure_account_identity(hass, entry, client)
    entry_options = normalized_service_options(dict(entry.options))
    if entry_options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=entry_options)
    filesystem_journal = FilesystemTransactionJournal(
        HomeAssistantJournalMetadataStore(hass, entry.entry_id),
        HomeAssistantRecoveryBlobStore(
            hass,
            entry.entry_id,
            max_blob_bytes=MAX_TREE_RECOVERY_BLOB_BYTES,
        ),
        max_blob_bytes=MAX_TREE_RECOVERY_BLOB_BYTES,
        max_total_blob_bytes=MAX_TREE_RECOVERY_BLOB_BYTES * 4,
    )
    filesystem = NitradoFilesystemService(
        client,
        allow_plaintext_ftp_service_ids=set(service_id_set(entry_options, CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS)),
        account_entry_id=entry.entry_id,
        journal=filesystem_journal,
    )
    coordinator = NitradoAccountCoordinator(
        client=client,
        account_entry_id=entry.entry_id,
        filesystem=filesystem,
        filesystem_journal=filesystem_journal,
        options=AccountCoordinatorOptions(
            discovery_mode=entry_options.get(CONF_DISCOVERY_MODE, DEFAULT_DISCOVERY_MODE),
            discovery_interval=entry_options[CONF_DISCOVERY_INTERVAL],
            status_interval=entry_options[CONF_STATUS_INTERVAL],
            missing_service_threshold=entry_options.get(
                CONF_MISSING_SERVICE_THRESHOLD,
                DEFAULT_MISSING_SERVICE_THRESHOLD,
            ),
            settle_seconds=entry_options.get(CONF_SETTLE_SECONDS, DEFAULT_SETTLE_SECONDS),
            imported_service_ids=frozenset(service_id_set(entry_options, CONF_IMPORTED_SERVICE_IDS)),
            ignored_service_ids=frozenset(service_id_set(entry_options, CONF_IGNORED_SERVICE_IDS)),
            service_area_ids=service_area_id_map(entry_options),
            service_display_names=service_display_name_map(entry_options),
            idle_shutdown_service_ids=frozenset(service_id_set(entry_options, CONF_IDLE_SHUTDOWN_SERVICE_IDS)),
            maintenance_service_ids=frozenset(service_id_set(entry_options, CONF_MAINTENANCE_SERVICE_IDS)),
            dry_run_service_ids=frozenset(service_id_set(entry_options, CONF_DRY_RUN_SERVICE_IDS)),
            allow_plaintext_ftp_service_ids=frozenset(
                service_id_set(entry_options, CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS)
            ),
            idle_minutes=idle_minutes_map(entry_options),
            startup_cooldown_minutes=startup_cooldown_minutes_map(entry_options),
            profile_options=profile_options_map(entry_options),
            profile_option_acknowledgements=profile_option_acknowledgements_map(entry_options),
        ),
    )
    await coordinator.async_bind_editable_file_journal(
        EditableFileJournal(HomeAssistantEditableFileJournalStore(hass, entry.entry_id))
    )

    try:
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        provider_runtime = await async_get_or_create_provider_runtime(hass)
        await provider_runtime.async_bind_account(entry.entry_id, coordinator)
        await coordinator.async_initialize_filesystem()
        await coordinator.async_refresh_discovery()
        _clear_stale_reauth(hass, entry, abort_active=False)
        await coordinator.async_refresh_managed_services()
        dependency_options = entry_options
        for service_id in sorted(coordinator.services):
            if coordinator.unmet_idle_shutdown_profile_options(service_id):
                dependency_options = updated_idle_toggle_options(
                    dependency_options,
                    service_id,
                    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
                    False,
                )
        if dependency_options != entry_options:
            entry_options = dependency_options
            hass.config_entries.async_update_entry(entry, options=entry_options)
            coordinator.options.idle_shutdown_service_ids = frozenset(
                service_id_set(entry_options, CONF_IDLE_SHUTDOWN_SERVICE_IDS)
            )
        recovery_service_ids = {
            fact.service.service_id
            for fact in coordinator.filesystem_recovery_facts
            if fact.service is not None and fact.service.service_id in coordinator.services
        }
        for service_id in sorted(recovery_service_ids):
            try:
                await coordinator.async_recover_filesystem_transactions(service_id)
            except NitradoAuthError:
                raise
            except (NitradoApiError, FilesystemJournalError):
                _LOGGER.warning(
                    "Nitrado service %s still requires provider-file recovery; a Repair issue will remain",
                    service_id,
                    exc_info=True,
                )
        await coordinator.async_run_scheduled_validations()
        await async_update_discovery_flows(hass, entry, coordinator)
        await async_update_repair_issues(hass, entry, coordinator)

        # Config entries can initialize before HA has loaded the durable issue
        # registry.  Reconcile once more after startup so stale integration-owned
        # issues from a previous process cannot be resurrected by load ordering.
        from homeassistant.helpers.start import async_at_started

        async def reconcile_repairs_after_start(_hass: Any) -> None:
            if _entry_coordinator_active(hass, entry, coordinator):
                await async_register_builtin_cockpit_assets(
                    hass,
                    profiles=builtin_profiles,
                    owner_root=Path(__file__).parent,
                )
                await async_update_repair_issues(hass, entry, coordinator)

        coordinator.unload_callbacks.append(async_at_started(hass, reconcile_repairs_after_start))
    except asyncio.CancelledError as cancelled:
        await _async_drain_cleanup(_async_rollback_entry_setup(hass, entry, coordinator))
        raise cancelled
    except NitradoAuthError as err:
        await _async_rollback_entry_setup(hass, entry, coordinator)
        raise ConfigEntryAuthFailed("Nitrado rejected the configured API token") from err
    except Exception as err:
        await _async_rollback_entry_setup(hass, entry, coordinator)
        _LOGGER.exception("Nitrado initial refresh failed")
        raise ConfigEntryNotReady("Nitrado initial refresh failed; details were logged") from err

    platforms_started = False
    try:
        _LOGGER.info(
            "Nitrado Game Server setup prepared %s managed service(s), %s pending service(s)",
            len(coordinator.services),
            len(coordinator.snapshot().pending_service_ids),
        )
        _async_migrate_composite_registry_identity(hass, entry, coordinator)
        platforms_started = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _async_apply_device_registry_overrides(hass, coordinator)
    except asyncio.CancelledError as cancelled:
        await _async_drain_cleanup(
            _async_rollback_entry_setup(hass, entry, coordinator, platforms_started=platforms_started)
        )
        raise cancelled
    except Exception as err:
        await _async_rollback_entry_setup(hass, entry, coordinator, platforms_started=platforms_started)
        _LOGGER.exception("Nitrado platform setup failed")
        raise ConfigEntryNotReady("Nitrado platform setup failed; details were logged") from err

    async def status_refresh(_now: Any) -> None:
        before_profile_ids = _managed_profile_ids(coordinator)
        account_probe_count = int(getattr(client, "verified_account_probe_count", 0))
        try:
            await coordinator.async_refresh_managed_services()
        except NitradoAuthError:
            if _entry_coordinator_active(hass, entry, coordinator):
                provider_runtime = get_provider_runtime(hass)
                if provider_runtime is not None:
                    await provider_runtime.async_invalidate_account(entry.entry_id)
                entry.async_start_reauth_if_available(hass)
            return
        if not _entry_coordinator_active(hass, entry, coordinator):
            return
        if int(getattr(client, "verified_account_probe_count", 0)) > account_probe_count:
            _clear_stale_reauth(hass, entry)
        if _managed_profile_ids(coordinator) != before_profile_ids:
            await hass.config_entries.async_reload(entry.entry_id)

    async def discovery_refresh(_now: Any) -> None:
        before_service_ids = set(coordinator.services)
        before_profile_ids = _managed_profile_ids(coordinator)
        try:
            await coordinator.async_refresh_all()
            await coordinator.async_run_scheduled_validations()
        except NitradoAuthError:
            if _entry_coordinator_active(hass, entry, coordinator):
                provider_runtime = get_provider_runtime(hass)
                if provider_runtime is not None:
                    await provider_runtime.async_invalidate_account(entry.entry_id)
                entry.async_start_reauth_if_available(hass)
            return
        if not _entry_coordinator_active(hass, entry, coordinator):
            return
        _clear_stale_reauth(hass, entry)
        if set(coordinator.services) != before_service_ids or _managed_profile_ids(coordinator) != before_profile_ids:
            await hass.config_entries.async_reload(entry.entry_id)
            return
        await async_update_discovery_flows(hass, entry, coordinator)
        await async_update_repair_issues(hass, entry, coordinator)

    try:
        coordinator.unload_callbacks.extend(
            [
                async_track_time_interval(
                    hass,
                    status_refresh,
                    timedelta(seconds=entry_options.get(CONF_STATUS_INTERVAL, DEFAULT_STATUS_INTERVAL)),
                ),
                async_track_time_interval(
                    hass,
                    discovery_refresh,
                    timedelta(seconds=entry_options.get(CONF_DISCOVERY_INTERVAL, DEFAULT_DISCOVERY_INTERVAL)),
                ),
            ]
        )
        await _async_register_services(hass)
        async_register_extension_views(hass)
        await async_register_extension_panel(hass)
        coordinator.unload_callbacks.append(entry.add_update_listener(_async_entry_updated))
        coordinator.setup_committed = True
        publish_cockpit_account_epoch(hass, entry.entry_id, "account_loaded")
        return True
    except asyncio.CancelledError as cancelled:
        await _async_drain_cleanup(
            _async_rollback_entry_setup(hass, entry, coordinator, platforms_started=platforms_started)
        )
        raise cancelled
    except Exception as err:
        await _async_rollback_entry_setup(hass, entry, coordinator, platforms_started=platforms_started)
        _LOGGER.exception("Nitrado final setup registration failed")
        raise ConfigEntryNotReady("Nitrado final setup registration failed; details were logged") from err


async def _async_rollback_entry_setup(
    hass: Any,
    entry: Any,
    coordinator: NitradoAccountCoordinator,
    *,
    platforms_started: bool = False,
) -> None:
    """Best-effort reverse runtime-owned setup without hiding the original failure."""

    coordinator.setup_committed = False
    for unsubscribe in tuple(coordinator.unload_callbacks):
        with suppress(Exception):
            unsubscribe()
    coordinator.unload_callbacks.clear()
    if platforms_started:
        with suppress(Exception):
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if hass.data.get(DOMAIN, {}).get(entry.entry_id) is coordinator:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not any(
        isinstance(candidate, NitradoAccountCoordinator) and candidate.setup_committed
        for candidate in hass.data.get(DOMAIN, {}).values()
    ):
        with suppress(Exception):
            async_unregister_extension_panel(hass)
    with suppress(Exception):
        await coordinator.async_finalize_shutdown()
    provider_runtime = get_provider_runtime(hass)
    if provider_runtime is not None:
        with suppress(Exception):
            await provider_runtime.async_unbind_account(entry.entry_id, coordinator=coordinator)
    with suppress(Exception):
        await _async_close_provider_runtime_if_unused(hass)


def _entry_coordinator_active(hass: Any, entry: Any, coordinator: NitradoAccountCoordinator) -> bool:
    """Return whether a periodic callback still belongs to a live entry."""

    return (
        hass.data.get(DOMAIN, {}).get(entry.entry_id) is coordinator
        and coordinator.setup_committed
        and not coordinator.shutting_down
    )


def _clear_stale_reauth(hass: Any, entry: Any, *, abort_active: bool = True) -> None:
    """Abort a disproven reauth flow and remove its Home Assistant repair issue."""

    from homeassistant.config_entries import SOURCE_REAUTH
    from homeassistant.helpers import issue_registry as ir

    flows = tuple(
        hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            include_uninitialized=True,
            match_context={"entry_id": entry.entry_id, "source": SOURCE_REAUTH},
        )
    )
    if flows and not abort_active:
        # Setup can run inside a legitimate token-replacement flow. Let that
        # flow finish and remove its own issue through Home Assistant's normal
        # lifecycle instead of aborting the handler that is awaiting reload.
        return
    for flow in flows:
        if flow_id := flow.get("flow_id"):
            hass.config_entries.flow.async_abort(flow_id)

    issue_id = f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
    issue_registry = ir.async_get(hass)
    issue_existed = issue_registry.async_get_issue("homeassistant", issue_id) is not None
    ir.async_delete_issue(hass, "homeassistant", issue_id)
    if flows or issue_existed:
        _LOGGER.info(
            "Cleared stale Nitrado reauthentication after a verified account refresh for entry %s",
            entry.entry_id,
        )


async def _async_close_provider_runtime_if_unused(hass: Any) -> None:
    """Close the singleton when setup/unload leaves no account coordinator."""

    if hass.data.get(DOMAIN):
        return
    provider_runtime = get_provider_runtime(hass)
    if provider_runtime is not None:
        await provider_runtime.async_close()


async def async_migrate_entry(hass: Any, entry: Any) -> bool:
    """Migrate persisted config-entry data/options to the current contract."""

    if entry.version > 2:
        return False
    data = dict(entry.data)
    data.setdefault(CONF_ACCOUNT_UUID, str(uuid.uuid4()))
    options = normalized_service_options(dict(entry.options))
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=2,
    )
    return True


async def _async_ensure_account_identity(hass: Any, entry: Any, client: NitradoClient) -> None:
    """Backfill and enforce a stable account identity for legacy entries."""

    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError

    persisted_account_id = str(entry.data.get(CONF_ACCOUNT_ID) or "")
    try:
        account_id = await client.account_identity()
    except NitradoAuthError as err:
        raise ConfigEntryAuthFailed("Nitrado rejected the configured API token") from err
    except NitradoApiError:
        if persisted_account_id:
            # A narrower replacement token may lose /account permission. Keep
            # a previously verified stable identity (or the existing fallback)
            # rather than silently changing the config-entry identity.
            account_id = persisted_account_id
        else:
            try:
                services = await client.service_list()
            except NitradoAuthError as err:
                raise ConfigEntryAuthFailed("Nitrado rejected the configured API token") from err
            account_id = fallback_account_identity(services, str(entry.data[CONF_API_TOKEN]))

    unique_id = f"account:{account_id}"
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        if other.unique_id == unique_id or str(other.data.get(CONF_ACCOUNT_ID) or "") == str(account_id):
            raise ConfigEntryError("This Nitrado account is already configured in another entry")

    if entry.data.get(CONF_ACCOUNT_ID) != account_id or entry.unique_id != unique_id:
        data = dict(entry.data)
        data[CONF_ACCOUNT_ID] = str(account_id)
        hass.config_entries.async_update_entry(entry, data=data, unique_id=unique_id)


async def _async_entry_updated(hass: Any, entry: Any) -> None:
    """Apply live safety options and reload only when polling settings change."""

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return
    options = normalized_service_options(dict(entry.options))
    current = coordinator.options
    profile_values = profile_options_map(options)
    profile_acknowledgements = profile_option_acknowledgements_map(options)
    profile_changed = (
        profile_values != current.profile_options or profile_acknowledgements != current.profile_option_acknowledgements
    )
    idle_shutdown_service_ids = frozenset(service_id_set(options, CONF_IDLE_SHUTDOWN_SERVICE_IDS))
    maintenance_service_ids = frozenset(service_id_set(options, CONF_MAINTENANCE_SERVICE_IDS))
    dry_run_service_ids = frozenset(service_id_set(options, CONF_DRY_RUN_SERVICE_IDS))
    idle_minutes = idle_minutes_map(options)
    startup_cooldown_minutes = startup_cooldown_minutes_map(options)
    runtime_changed_service_ids = (
        current.idle_shutdown_service_ids ^ idle_shutdown_service_ids
        | current.maintenance_service_ids ^ maintenance_service_ids
        | current.dry_run_service_ids ^ dry_run_service_ids
        | {
            service_id
            for service_id in set(current.idle_minutes) | set(idle_minutes)
            if current.idle_minutes.get(service_id) != idle_minutes.get(service_id)
        }
        | {
            service_id
            for service_id in set(current.startup_cooldown_minutes) | set(startup_cooldown_minutes)
            if current.startup_cooldown_minutes.get(service_id) != startup_cooldown_minutes.get(service_id)
        }
    )
    current.profile_options = profile_values
    current.profile_option_acknowledgements = profile_acknowledgements
    current.idle_shutdown_service_ids = idle_shutdown_service_ids
    current.maintenance_service_ids = maintenance_service_ids
    current.dry_run_service_ids = dry_run_service_ids
    current.idle_minutes = idle_minutes
    current.startup_cooldown_minutes = startup_cooldown_minutes
    plaintext_service_ids = frozenset(service_id_set(options, CONF_ALLOW_PLAINTEXT_FTP_SERVICE_IDS))
    if plaintext_service_ids != current.allow_plaintext_ftp_service_ids:
        current.allow_plaintext_ftp_service_ids = plaintext_service_ids
        coordinator.profile_transport.update_plaintext_ftp_consent(set(plaintext_service_ids))
    if profile_changed:
        for runtime in tuple(coordinator.services.values()):
            await coordinator.async_profile_options_changed(
                runtime,
                "Profile security setting changed; pending shutdown revoked",
            )
    else:
        for service_id in sorted(runtime_changed_service_ids):
            coordinator.revoke_pending_shutdown(
                service_id,
                "Automatic shutdown setting changed; idle timer reset",
            )
    desired = (
        options[CONF_DISCOVERY_MODE],
        options[CONF_MISSING_SERVICE_THRESHOLD],
        options[CONF_SETTLE_SECONDS],
        options[CONF_STATUS_INTERVAL],
        options[CONF_DISCOVERY_INTERVAL],
    )
    active = (
        current.discovery_mode,
        current.missing_service_threshold,
        current.settle_seconds,
        current.status_interval,
        current.discovery_interval,
    )
    if desired != active:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload one Nitrado account config entry."""

    return await _async_drain_cleanup(_async_unload_entry_transaction(hass, entry))


async def _async_unload_entry_transaction(hass: Any, entry: Any) -> bool:
    """Own the full unload transaction through repeated caller cancellation."""

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        publish_cockpit_account_epoch(hass, entry.entry_id, "account_unloading")
        await _async_drain_cleanup(async_cancel_save_bundle_jobs(hass, account_entry_id=entry.entry_id))
        provider_runtime = get_provider_runtime(hass)
        if provider_runtime is not None:
            await provider_runtime.async_invalidate_account(entry.entry_id)
        # Revoke Stop and file-mutation intent before platform unload yields to
        # other tasks. A slow platform unload must not leave destructive work
        # alive long enough to cross its transport boundary.
        await coordinator.async_shutdown()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok and coordinator:
        coordinator.resume_after_failed_shutdown()
    if unload_ok:
        await _async_drain_cleanup(_async_finalize_entry_unload(hass, entry, coordinator))
    return unload_ok


async def _async_finalize_entry_unload(hass: Any, entry: Any, coordinator: Any | None) -> None:
    """Complete every teardown obligation even when one component fails."""

    first_error: Exception | None = None

    def remember(err: Exception) -> None:
        nonlocal first_error
        if first_error is None:
            first_error = err

    if coordinator is not None:
        for unsubscribe in tuple(coordinator.unload_callbacks):
            try:
                unsubscribe()
            except Exception as err:  # noqa: BLE001 - continue deterministic teardown.
                remember(err)
        coordinator.unload_callbacks.clear()
        try:
            await coordinator.async_finalize_shutdown()
        except Exception as err:  # noqa: BLE001 - provider/global cleanup must still run.
            remember(err)

    provider_runtime = get_provider_runtime(hass)
    if provider_runtime is not None:
        try:
            await provider_runtime.async_unbind_account(entry.entry_id, coordinator=coordinator)
        except Exception as err:  # noqa: BLE001 - continue deterministic teardown.
            remember(err)
    if coordinator is not None and hass.data.get(DOMAIN, {}).get(entry.entry_id) is coordinator:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    if not hass.data.get(DOMAIN):
        if provider_runtime is not None:
            try:
                await provider_runtime.async_close()
            except Exception as err:  # noqa: BLE001 - remove remaining global surfaces too.
                remember(err)
        async_unregister_extension_panel(hass)
        for service_name in (
            "start",
            "stop",
            "import_service",
            "ignore_service",
            "remove_service",
            "cancel_pending_shutdown",
            "profile_action",
        ):
            hass.services.async_remove(DOMAIN, service_name)
    if first_error is not None:
        raise first_error


async def async_remove_entry(hass: Any, entry: Any) -> None:
    """Clean integration-owned local state when the account entry is removed."""

    task = asyncio.create_task(
        _async_remove_entry_impl(hass, entry),
        name=f"nitrado-remove-entry-{entry.entry_id}",
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _async_remove_entry_impl(hass: Any, entry: Any) -> None:
    """Finish permanent local cleanup even if the caller is cancelled."""

    if hass.data.get(DOMAIN, {}).get(entry.entry_id) is not None:
        raise RuntimeError("Nitrado config entry is still active; refusing destructive local cleanup")

    await async_purge_entry_local_data(hass, entry.entry_id)

    service_ids = set(service_id_set(dict(entry.options), CONF_IMPORTED_SERVICE_IDS))
    service_ids.update(service_id_set(dict(entry.options), CONF_IGNORED_SERVICE_IDS))

    for service_id in service_ids:
        async_remove_service_registry_entries(hass, service_id, entry.entry_id)
        async_clear_service_issue(hass, entry, service_id)

    async_remove_entry_registry_entries(hass, entry)
    async_clear_entry_issues(hass, entry)
    await async_delete_provider_account_grants(hass, entry.entry_id)


async def _async_register_services(hass: Any) -> None:
    """Register domain actions once."""

    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    numeric_service_id = vol.Match(r"^\d+$")
    extension_key = vol.Match(r"^[a-z][a-z0-9_]*$")
    target_schema = {
        vol.Optional("service_id"): numeric_service_id,
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
    }

    async def start(call: Any) -> None:
        from homeassistant.exceptions import HomeAssistantError

        account_entry_id, service_id = _service_target_from_call(hass, call)
        coordinator = _find_target_coordinator(hass, account_entry_id, service_id)
        await _async_require_entity_control(hass, call, coordinator.account_entry_id, service_id, "start")
        if bool(call.data.get("force", False)):
            await _async_require_service_admin(hass, call)
        try:
            await coordinator.async_start_service(
                service_id,
                force=bool(call.data.get("force", False)),
            )
        except NitradoControlError as err:
            raise HomeAssistantError(str(err)) from err

    async def stop(call: Any) -> None:
        from homeassistant.exceptions import HomeAssistantError

        account_entry_id, service_id = _service_target_from_call(hass, call)
        coordinator = _find_target_coordinator(hass, account_entry_id, service_id)
        await _async_require_entity_control(hass, call, coordinator.account_entry_id, service_id, "stop")
        if bool(call.data.get("force", False)):
            await _async_require_service_admin(hass, call)
        try:
            await coordinator.async_stop_service(
                service_id,
                reason=str(call.data.get("reason") or "Stopped from Home Assistant"),
                force=bool(call.data.get("force", False)),
            )
        except NitradoControlError as err:
            raise HomeAssistantError(str(err)) from err

    async def import_service(call: Any) -> None:
        await _async_require_service_admin(hass, call)
        entry, coordinator = _find_entry_and_coordinator_for_known_service(hass, call.data["service_id"])
        await coordinator.import_service(str(call.data["service_id"]))
        _persist_service_choice(
            hass,
            entry,
            import_service_id=str(call.data["service_id"]),
            service_area_id=call.data.get("area_id"),
            service_display_name=call.data.get("display_name"),
        )
        coordinator.active_discovery_flow_ids.discard(f"{entry.entry_id}:service:{call.data['service_id']}")
        await async_update_repair_issues(hass, entry, coordinator)
        await hass.config_entries.async_reload(entry.entry_id)

    async def ignore_service(call: Any) -> None:
        await _async_require_service_admin(hass, call)
        entry, coordinator = _find_entry_and_coordinator_for_known_service(hass, call.data["service_id"])
        service_id = str(call.data["service_id"])
        provider_runtime = get_provider_runtime(hass)
        if provider_runtime is not None:
            await provider_runtime.async_invalidate_service(entry.entry_id, service_id)
        cancelled = coordinator.ignore_service(service_id)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        _persist_service_choice(hass, entry, ignore_service_id=service_id)
        coordinator.active_discovery_flow_ids.discard(f"{entry.entry_id}:service:{call.data['service_id']}")
        await async_update_repair_issues(hass, entry, coordinator)
        await hass.config_entries.async_reload(entry.entry_id)
        async_remove_service_registry_entries(hass, service_id, entry.entry_id)
        async_clear_service_issue(hass, entry, service_id)

    async def remove_service(call: Any) -> None:
        await _async_require_service_admin(hass, call)
        entry, coordinator = _find_entry_and_coordinator_for_known_service(hass, call.data["service_id"])
        service_id = str(call.data["service_id"])
        provider_runtime = get_provider_runtime(hass)
        if provider_runtime is not None:
            await provider_runtime.async_invalidate_service(entry.entry_id, service_id)
            await provider_runtime.authority.async_delete_service_grants(ProviderServiceRef(entry.entry_id, service_id))
        cancelled = coordinator.remove_service(service_id)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        _persist_service_choice(hass, entry, remove_service_id=service_id)
        await async_update_repair_issues(hass, entry, coordinator)
        await hass.config_entries.async_reload(entry.entry_id)
        async_remove_service_registry_entries(hass, service_id, entry.entry_id)
        async_clear_service_issue(hass, entry, service_id)

    async def cancel_pending_shutdown(call: Any) -> None:
        account_entry_id, service_id = _service_target_from_call(hass, call)
        coordinator = _find_target_coordinator(hass, account_entry_id, service_id)
        await _async_require_entity_control(
            hass,
            call,
            coordinator.account_entry_id,
            service_id,
            "cancel_pending_shutdown",
        )
        await coordinator.async_cancel_pending_shutdown(service_id)

    async def profile_action(call: Any) -> None:
        from homeassistant.exceptions import HomeAssistantError

        account_entry_id, service_id = _service_target_from_call(hass, call)
        # Profile actions have no one-to-one HA entity permission target. Keep
        # service-call access explicit: admins and internal/system automations.
        await _async_require_service_admin(hass, call)
        coordinator = _find_target_coordinator(hass, account_entry_id, service_id)
        action_key = str(call.data["action_key"])
        runtime = coordinator.get_runtime(service_id)
        expected_profile = coordinator.profile_dispatch_token(service_id)
        try:
            manifest = runtime_profile_extension_manifest(runtime) if runtime.profile else None
        except ProfileManifestError as err:
            log_profile_failure(
                _LOGGER,
                "service_action_manifest",
                err,
                profile_id=getattr(runtime.profile, "profile_id", "unknown"),
                key=action_key,
            )
            raise HomeAssistantError("The selected game profile manifest is invalid; details were logged.") from None
        declaration = next((item for item in manifest.actions if item.key == action_key), None) if manifest else None
        if declaration is not None and declaration.access == ExtensionAccess.ADMIN:
            await _async_require_service_admin(hass, call)
        try:
            await coordinator.async_run_profile_action(
                service_id,
                action_key,
                payload=_validated_profile_action_payload(call.data.get("payload")),
                confirmed=bool(call.data.get("confirm", False)),
                expected_profile=expected_profile,
            )
        except Exception as err:
            raise HomeAssistantError(str(err)) from err

    registrations = (
        (
            "start",
            start,
            vol.Schema({**target_schema, vol.Optional("force", default=False): cv.boolean}, extra=vol.PREVENT_EXTRA),
        ),
        (
            "stop",
            stop,
            vol.Schema(
                {
                    **target_schema,
                    vol.Optional("reason"): vol.All(cv.string, vol.Length(max=512)),
                    vol.Optional("force", default=False): cv.boolean,
                },
                extra=vol.PREVENT_EXTRA,
            ),
        ),
    )
    import_schema = vol.Schema(
        {
            vol.Required("service_id"): numeric_service_id,
            vol.Optional("display_name"): cv.string,
            vol.Optional("area_id"): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    service_id_schema = vol.Schema({vol.Required("service_id"): numeric_service_id}, extra=vol.PREVENT_EXTRA)
    registrations += (
        ("import_service", import_service, import_schema),
        ("ignore_service", ignore_service, service_id_schema),
        ("remove_service", remove_service, service_id_schema),
        (
            "cancel_pending_shutdown",
            cancel_pending_shutdown,
            vol.Schema(target_schema, extra=vol.PREVENT_EXTRA),
        ),
        (
            "profile_action",
            profile_action,
            vol.Schema(
                {
                    **target_schema,
                    vol.Required("action_key"): extension_key,
                    vol.Optional("payload"): object,
                    vol.Optional("confirm", default=False): cv.boolean,
                },
                extra=vol.PREVENT_EXTRA,
            ),
        ),
    )
    for service_name, handler, schema in registrations:
        if not hass.services.has_service(DOMAIN, service_name):
            hass.services.async_register(DOMAIN, service_name, handler, schema=schema)


def _service_target_from_call(hass: Any, call: Any) -> tuple[str | None, str]:
    """Resolve exactly one managed service from an explicit ID or HA device target."""

    from homeassistant.exceptions import HomeAssistantError

    explicit = call.data.get("service_id")
    device_ids = call.data.get("device_id")
    if explicit and device_ids:
        raise HomeAssistantError("Choose either service_id or a device target, not both")
    if explicit:
        return None, str(explicit)
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    if not isinstance(device_ids, list) or len(device_ids) != 1:
        raise HomeAssistantError("Target exactly one Nitrado game server device")

    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get(str(device_ids[0]))
    if device is None:
        raise HomeAssistantError("The selected Nitrado game server device does not exist")
    targets: set[tuple[str, str]] = set()
    for domain, identifier in device.identifiers:
        if domain != DOMAIN or not identifier.startswith("account:"):
            continue
        parts = identifier.split(":", 3)
        if len(parts) == 4 and parts[2] == "service":
            targets.add((parts[1], parts[3]))
    if len(targets) != 1:
        raise HomeAssistantError("The selected device is not one Nitrado game server service")
    return next(iter(targets))


def _validated_profile_action_payload(value: Any) -> Any:
    """Require a bounded finite-JSON payload at the HA service boundary."""

    from homeassistant.exceptions import HomeAssistantError

    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, OverflowError) as err:
        raise HomeAssistantError("Profile action payload must contain finite JSON data") from err
    if len(encoded) > _MAX_PROFILE_ACTION_PAYLOAD_BYTES:
        raise HomeAssistantError(f"Profile action payload exceeds {_MAX_PROFILE_ACTION_PAYLOAD_BYTES} bytes")
    return value


async def _async_require_service_admin(hass: Any, call: Any) -> None:
    """Require an administrator for an admin-only profile action."""

    from homeassistant.exceptions import Unauthorized

    user_id = getattr(getattr(call, "context", None), "user_id", None)
    if user_id is None:
        return  # Internal/system automation context.
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        raise Unauthorized()


async def _async_require_entity_control(
    hass: Any,
    call: Any,
    account_entry_id: str,
    service_id: str,
    entity_key: str,
) -> None:
    """Require control permission for the exact entity represented by an action."""

    from homeassistant.auth.permissions.const import CAT_ENTITIES, POLICY_CONTROL
    from homeassistant.exceptions import Unauthorized, UnknownUser
    from homeassistant.helpers import entity_registry as er

    context = getattr(call, "context", None)
    user_id = getattr(context, "user_id", None)
    if user_id is None:
        return  # Internal/system automation context.
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=context, permission=POLICY_CONTROL, user_id=user_id)
    if user.is_admin:
        return

    registry = er.async_get(hass)
    unique_id = f"account:{account_entry_id}:service:{service_id}:{entity_key}"
    entity = next(
        (item for item in registry.entities.values() if item.platform == DOMAIN and item.unique_id == unique_id),
        None,
    )
    if entity is not None and user.permissions.check_entity(entity.entity_id, POLICY_CONTROL):
        return
    raise Unauthorized(
        context=context,
        permission=POLICY_CONTROL,
        user_id=user_id,
        perm_category=CAT_ENTITIES,
    )


def _find_coordinator_for_service(hass: Any, service_id: str) -> NitradoAccountCoordinator:
    """Find the account coordinator managing a service ID."""

    from homeassistant.exceptions import HomeAssistantError

    matches = [
        coordinator
        for coordinator in hass.data.get(DOMAIN, {}).values()
        if coordinator.setup_committed and str(service_id) in coordinator.services
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HomeAssistantError(
            f"Nitrado service {service_id} exists in more than one account; use an account-bound entity or panel."
        )
    raise HomeAssistantError(f"Nitrado service {service_id} is not managed by this Home Assistant instance.")


def _find_target_coordinator(
    hass: Any,
    account_entry_id: str | None,
    service_id: str,
) -> NitradoAccountCoordinator:
    """Resolve a composite device target or a unique legacy service target."""

    from homeassistant.exceptions import HomeAssistantError

    if account_entry_id is None:
        return _find_coordinator_for_service(hass, service_id)
    coordinator = hass.data.get(DOMAIN, {}).get(str(account_entry_id))
    if coordinator is None or not coordinator.setup_committed or str(service_id) not in coordinator.services:
        raise HomeAssistantError("The exact Nitrado account/service target is not managed")
    return coordinator


def _find_entry_and_coordinator_for_known_service(hass: Any, service_id: str) -> tuple[Any, NitradoAccountCoordinator]:
    """Find the config entry/coordinator that knows about a service ID."""

    from homeassistant.exceptions import HomeAssistantError

    matches: list[tuple[Any, NitradoAccountCoordinator]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator and str(service_id) in coordinator.known:
            matches.append((entry, coordinator))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise HomeAssistantError(
            f"Nitrado service {service_id} exists in more than one account; use the account options flow."
        )
    raise HomeAssistantError(f"Nitrado service {service_id} is not known by this Home Assistant instance.")


def _persist_service_choice(
    hass: Any,
    entry: Any,
    *,
    import_service_id: str | None = None,
    ignore_service_id: str | None = None,
    remove_service_id: str | None = None,
    service_display_name: str | None = None,
    service_area_id: str | None = None,
) -> None:
    """Persist service import/ignore/remove choices in config entry options."""

    options = dict(entry.options)
    hass.config_entries.async_update_entry(
        entry,
        options=updated_service_options(
            options,
            import_service_id=import_service_id,
            ignore_service_id=ignore_service_id,
            remove_service_id=remove_service_id,
            service_area_id=service_area_id,
            service_display_name=service_display_name,
        ),
    )


def _async_apply_device_registry_overrides(hass: Any, coordinator: NitradoAccountCoordinator) -> None:
    """Apply current links plus stored user-facing names and areas to HA devices."""

    try:
        from homeassistant.helpers import device_registry as dr
    except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.
        return

    registry = dr.async_get(hass)
    for service_id, runtime in coordinator.services.items():
        device = registry.async_get_device(identifiers={runtime.state.identity.device_identifier})
        if device is None:
            continue

        updates: dict[str, str] = {
            "configuration_url": device_configuration_url(service_id, coordinator.account_entry_id)
        }
        if display_name := coordinator.options.service_display_names.get(service_id):
            updates["name_by_user"] = display_name
        if area_id := coordinator.options.service_area_ids.get(service_id):
            updates["area_id"] = area_id

        registry.async_update_device(device.id, **updates)


def _async_migrate_composite_registry_identity(
    hass: Any,
    entry: Any,
    coordinator: NitradoAccountCoordinator,
) -> None:
    """Migrate legacy service-only HA registry IDs without losing entity IDs."""

    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
    except ModuleNotFoundError:  # pragma: no cover - local pure tests run without HA installed.
        return

    entity_registry = er.async_get(hass)
    for entity in tuple(entity_registry.entities.values()):
        if entity.platform != DOMAIN or entity.config_entry_id != entry.entry_id:
            continue
        unique_id = str(entity.unique_id)
        if not unique_id.startswith("service:"):
            continue
        parts = unique_id.split(":", 2)
        if len(parts) != 3 or parts[1] not in coordinator.services:
            continue
        new_unique_id = f"account:{entry.entry_id}:service:{parts[1]}:{parts[2]}"
        entity_registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)

    device_registry = dr.async_get(hass)
    for device in tuple(device_registry.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue
        for domain, identifier in tuple(device.identifiers):
            if domain != DOMAIN or not identifier.startswith("service:"):
                continue
            service_id = identifier.removeprefix("service:")
            if service_id not in coordinator.services:
                continue
            device_registry.async_update_device(
                device.id,
                new_identifiers={(DOMAIN, f"account:{entry.entry_id}:service:{service_id}")},
            )
            break


def _managed_profile_ids(coordinator: NitradoAccountCoordinator) -> tuple[tuple[str, str | None, int], ...]:
    """Return a compact profile signature for managed service entity lifecycle."""

    return tuple(
        sorted(
            (service_id, runtime.state.profile_id, runtime.profile_registration_generation)
            for service_id, runtime in coordinator.services.items()
        )
    )


__all__ = ["DOMAIN"]
