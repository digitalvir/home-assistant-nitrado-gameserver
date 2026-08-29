"""Executable checks for published profile-author examples."""

from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path

from custom_components.nitrado_gameserver.api.nitrado import NitradoService, ParsedServer
from custom_components.nitrado_gameserver.plugins.base import ControlContext, ProfileStatus


class DocumentationTests(unittest.TestCase):
    """Keep copy/paste examples aligned with the public runtime contract."""

    def test_minimal_profile_example_executes_enrichment_contract(self) -> None:
        guide = (Path(__file__).resolve().parents[1] / "DEVELOPING_PROFILES.md").read_text(encoding="utf-8")
        section = guide.split("## Minimal Profile", 1)[1]
        code = re.search(r"```python\n(.*?)\n```", section, flags=re.DOTALL)
        self.assertIsNotNone(code)
        namespace: dict[str, object] = {}
        exec(code.group(1), namespace)
        profile = namespace["ExampleProfile"]()
        service = NitradoService("123", "Example", "example", "Example", "example", "Gameserver", {})
        server = ParsedServer(
            service_id="123",
            raw_status="started",
            server_name="Example",
            address=None,
            game_short="example",
            game_human="Example",
            player_count=None,
            player_max=None,
            player_names=(),
            query_valid=False,
            player_source=None,
            raw_redacted={},
        )
        context = ControlContext(service, server, True, False)

        result = asyncio.run(profile.enrich_status(object(), service, server, context))

        self.assertIsInstance(result, ProfileStatus)


if __name__ == "__main__":
    unittest.main()
