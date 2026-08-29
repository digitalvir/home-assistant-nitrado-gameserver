"""Tests for human-facing service naming."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import (
    NitradoService,
    ParsedServer,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.naming import (
    service_display_name,
    service_metadata_display_name,
)
from custom_components.nitrado_gameserver.runtime import ServiceRuntime


def service_fixture(*, name: str = "ni", game_human: str = "Palworld Xbox") -> NitradoService:
    """Return a Nitrado service fixture."""

    return NitradoService(
        service_id="12345678",
        name=name,
        game="palworldxb",
        game_human=game_human,
        folder_short="palworldxb",
        type_human="Gameserver",
        raw_redacted={},
    )


class NamingTests(unittest.TestCase):
    """Human-facing naming tests."""

    def test_service_metadata_rejects_short_nitrado_label(self) -> None:
        self.assertEqual(service_metadata_display_name(service_fixture()), "Palworld Xbox Server")

    def test_explicit_display_name_wins(self) -> None:
        runtime = ServiceRuntime(ManagedServiceState("12345678"))
        runtime.update_service(service_fixture())

        self.assertEqual(
            service_display_name(runtime, {"12345678": "Example Palworld Server"}),
            "Example Palworld Server",
        )

    def test_default_palworld_server_name_is_rejected(self) -> None:
        runtime = ServiceRuntime(ManagedServiceState("12345678"))
        runtime.update_service(service_fixture())
        runtime.update_server(
            ParsedServer(
                service_id="12345678",
                raw_status="stopped",
                server_name="Palserver hosted by nitrado.net",
                address="203.0.113.10:8211",
                game_short="palworldxb",
                game_human="Palworld Xbox",
                player_count=None,
                player_max=None,
                player_names=(),
                query_valid=False,
                player_source="unavailable",
                raw_redacted={},
            ),
            observed_at=1000,
        )

        self.assertEqual(service_display_name(runtime), "Palworld Xbox Server")


if __name__ == "__main__":
    unittest.main()
