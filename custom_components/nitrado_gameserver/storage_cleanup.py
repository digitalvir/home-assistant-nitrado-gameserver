"""Exact, local-only config-entry data cleanup."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .cockpit import purge_cockpit_account_state
from .const import DOMAIN
from .discovery import async_abort_entry_discovery_flows
from .editable_file_journal import HomeAssistantEditableFileJournalStore
from .ha_filesystem_storage import HomeAssistantJournalMetadataStore, HomeAssistantRecoveryBlobStore
from .ha_native_backup_storage import HomeAssistantNativeBackupJournalStore
from .operation_journal import HomeAssistantOperationJournalStore
from .save_bundle_jobs import async_cancel_save_bundle_jobs
from .service_options import remove_entry_option_update_lock


class EntryDataCleanupError(RuntimeError):
    """Raised when local entry data cannot be completely removed."""


async def async_purge_entry_local_data(hass: Any, entry_id: str) -> None:
    """Purge integration-owned local data without touching any provider files."""

    account_id = str(entry_id)
    if hass.data.get(DOMAIN, {}).get(account_id) is not None:
        raise EntryDataCleanupError("config entry is still active; local recovery data was preserved")

    await async_cancel_save_bundle_jobs(hass, account_entry_id=account_id)
    remove_entry_option_update_lock(hass, account_id)
    await async_abort_entry_discovery_flows(hass, account_id)
    purge_cockpit_account_state(hass, account_id)

    operations: tuple[Callable[[], Awaitable[None]], ...] = (
        HomeAssistantRecoveryBlobStore(hass, account_id).async_remove_all,
        HomeAssistantJournalMetadataStore(hass, account_id).async_remove,
        HomeAssistantOperationJournalStore(hass, account_id).async_remove,
        HomeAssistantEditableFileJournalStore(hass, account_id).async_remove,
        HomeAssistantNativeBackupJournalStore(hass, account_id).async_remove,
    )
    errors: list[BaseException] = []
    for operation in operations:
        try:
            await operation()
        except Exception as err:  # noqa: BLE001 - attempt every independent local cleanup.
            errors.append(err)
    if errors:
        raise EntryDataCleanupError(f"local entry cleanup failed in {len(errors)} storage operation(s)") from errors[0]


__all__ = ("EntryDataCleanupError", "async_purge_entry_local_data")
