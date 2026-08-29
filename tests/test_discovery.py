"""Tests for Home Assistant discovery-flow planning."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import NitradoService
from custom_components.nitrado_gameserver.coordinator import NitradoAccountCoordinator
from custom_components.nitrado_gameserver.discovery import (
    build_discovery_flow_requests,
    discovery_title,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState


class DummyClient:
    """Unused client placeholder."""


class DummyEntry:
    """Small config-entry test double."""

    entry_id = "entry123"


def service(service_id: str, name: str) -> NitradoService:
    """Build a service fixture."""

    return NitradoService(
        service_id=service_id,
        name=name,
        game="palworldxb",
        game_human="Palworld",
        folder_short="palworldxb",
        type_human="Gameserver",
        raw_redacted={},
    )


class DiscoveryFlowPlanTests(unittest.TestCase):
    """Discovery-flow plan tests."""

    def test_pending_services_create_discovery_requests(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.known = {
            "123": ManagedServiceState("123", pending_discovery=True),
            "456": ManagedServiceState("456", pending_discovery=True),
        }
        coordinator.discovered_services = {
            "123": service("123", "Palworld Xbox"),
            "456": service("456", "ARK"),
        }

        requests = build_discovery_flow_requests(DummyEntry(), coordinator)

        self.assertEqual([request.service_id for request in requests], ["123", "456"])
        self.assertEqual(requests[0].flow_key, "entry123:service:123")
        self.assertEqual(requests[0].service_name, "Palworld Xbox")
        self.assertEqual(requests[0].game, "Palworld")
        self.assertEqual(requests[0].title, "Palworld Xbox - ID 123")

    def test_discovery_title_includes_useful_summary(self) -> None:
        self.assertEqual(
            discovery_title(
                service_name="Example Palworld Server",
                game="Palworld Xbox",
                service_id="12345678",
            ),
            "Example Palworld Server - Palworld Xbox - ID 12345678",
        )
        self.assertEqual(
            discovery_title(
                service_name="Palworld Xbox Server",
                game="Palworld Xbox",
                service_id="12345678",
            ),
            "Palworld Xbox Server - ID 12345678",
        )

    def test_ignored_or_imported_services_do_not_create_discovery_requests(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.known = {
            "123": ManagedServiceState("123", pending_discovery=True, ignored=True),
            "456": ManagedServiceState("456", pending_discovery=False),
        }

        requests = build_discovery_flow_requests(DummyEntry(), coordinator)

        self.assertEqual(requests, ())

    def test_options_menu_translation_labels_are_defined(self) -> None:
        strings = json.loads((ROOT / "custom_components/nitrado_gameserver/strings.json").read_text())
        translations = json.loads((ROOT / "custom_components/nitrado_gameserver/translations/en.json").read_text())

        menu_options = strings["options"]["step"]["init"]["menu_options"]
        translated_menu_options = translations["options"]["step"]["init"]["menu_options"]

        self.assertEqual(strings["config"]["flow_title"], "{name}")
        self.assertEqual(translations["config"]["flow_title"], "{name}")
        self.assertEqual(menu_options["services"], "Manage Nitrado services")
        self.assertEqual(menu_options["settings"], "Settings")
        self.assertEqual(translated_menu_options, menu_options)


if __name__ == "__main__":
    unittest.main()
