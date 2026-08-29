"""Home Assistant persistence for provider-native backup restore facts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .const import DOMAIN


class HomeAssistantNativeBackupJournalStore:
    """Persist bounded native restore metadata through HA's atomic Store."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, 1, f"{DOMAIN}.native_backup_journal.{entry_id}")
        self._quarantine_store = Store(hass, 1, f"{DOMAIN}.native_backup_journal_quarantine.{entry_id}")

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_save(self, payload: Mapping[str, Any]) -> None:
        await self._store.async_save(dict(payload))

    async def async_quarantine(self, payload: Any) -> None:
        """Preserve the rejected payload before an explicit reset."""

        await self._quarantine_store.async_save(
            {
                "quarantined_at": datetime.now(UTC).isoformat(),
                "payload": payload,
            }
        )

    async def async_remove(self) -> None:
        """Remove both accepted and quarantined metadata for this account."""

        first_error: Exception | None = None
        for store in (self._store, self._quarantine_store):
            try:
                await store.async_remove()
            except Exception as err:  # noqa: BLE001 - attempt both exact stores.
                first_error = first_error or err
        if first_error is not None:
            raise first_error


__all__ = ("HomeAssistantNativeBackupJournalStore",)
