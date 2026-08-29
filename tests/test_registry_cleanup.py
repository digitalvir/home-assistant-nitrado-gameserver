"""Tests for registry cleanup helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.registry_cleanup import (
    is_service_entity_unique_id,
)


class RegistryCleanupTests(unittest.TestCase):
    """Registry cleanup predicate tests."""

    def test_service_unique_id_predicate_matches_core_and_profile_entities(self) -> None:
        self.assertTrue(is_service_entity_unique_id("service:123456:status", "123456"))
        self.assertTrue(
            is_service_entity_unique_id(
                "service:123456:profile:palworld:palworld_player_source",
                "123456",
            )
        )

    def test_service_unique_id_predicate_rejects_other_services_and_bad_values(self) -> None:
        self.assertFalse(is_service_entity_unique_id("service:999999:status", "123456"))
        self.assertFalse(is_service_entity_unique_id("account:123456:status", "123456"))
        self.assertFalse(is_service_entity_unique_id(None, "123456"))


if __name__ == "__main__":
    unittest.main()
