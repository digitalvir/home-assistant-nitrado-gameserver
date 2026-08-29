"""Pure config-entry migration and option-listener tests."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver import (
    _async_entry_updated,
    async_migrate_entry,
)
from custom_components.nitrado_gameserver.const import (
    CONF_ACCOUNT_UUID,
    CONF_IDLE_SHUTDOWN_SERVICE_IDS,
    DOMAIN,
)
from custom_components.nitrado_gameserver.coordinator import AccountCoordinatorOptions


class FakeEntry:
    """Minimal config-entry fixture."""

    def __init__(self, *, version: int = 1, options: dict | None = None) -> None:
        self.entry_id = "entry-1"
        self.version = version
        self.data = {"api_token": "secret"}
        self.options = options or {}


class FakeConfigEntries:
    """Record config-entry writes and reloads."""

    def __init__(self) -> None:
        self.reloads: list[str] = []

    def async_update_entry(self, entry: FakeEntry, **changes) -> None:
        for key, value in changes.items():
            setattr(entry, key, value)

    async def async_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class FakeHass:
    """Small Home Assistant fixture."""

    def __init__(self, coordinator=None) -> None:
        self.config_entries = FakeConfigEntries()
        self.data = {DOMAIN: {"entry-1": coordinator} if coordinator is not None else {}}


class FakeCoordinator:
    """Coordinator fixture exposing active account options."""

    def __init__(self) -> None:
        self.options = AccountCoordinatorOptions()
        self.services = {}
        self.revoked: list[tuple[str, str]] = []

    def revoke_pending_shutdown(self, service_id: str, reason: str) -> None:
        self.revoked.append((service_id, reason))


class EntryLifecycleTests(unittest.TestCase):
    """Config-entry lifecycle regression coverage."""

    def test_migration_normalizes_options_without_losing_auto_shutdown(self) -> None:
        async def run() -> None:
            entry = FakeEntry(
                options={
                    "status_interval": "broken",
                    CONF_IDLE_SHUTDOWN_SERVICE_IDS: ["123"],
                }
            )
            hass = FakeHass()

            self.assertTrue(await async_migrate_entry(hass, entry))
            self.assertEqual(entry.version, 2)
            self.assertIn(CONF_ACCOUNT_UUID, entry.data)
            self.assertEqual(entry.options["status_interval"], 60)
            self.assertEqual(entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS], ["123"])

        asyncio.run(run())

    def test_update_listener_reloads_for_polling_change_only(self) -> None:
        async def run() -> None:
            coordinator = FakeCoordinator()
            hass = FakeHass(coordinator)
            entry = FakeEntry(version=2, options={"status_interval": 90})

            await _async_entry_updated(hass, entry)
            self.assertEqual(hass.config_entries.reloads, ["entry-1"])

            hass.config_entries.reloads.clear()
            coordinator.options = AccountCoordinatorOptions(status_interval=90)
            entry.options[CONF_IDLE_SHUTDOWN_SERVICE_IDS] = ["123"]
            await _async_entry_updated(hass, entry)
            self.assertEqual(hass.config_entries.reloads, [])
            self.assertEqual(coordinator.options.idle_shutdown_service_ids, frozenset({"123"}))
            self.assertEqual(coordinator.revoked[0][0], "123")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
