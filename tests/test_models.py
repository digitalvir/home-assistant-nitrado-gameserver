"""Tests for pure integration models."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.models import (
    ManagedServiceState,
    ServiceIdentity,
    reconcile_services,
)


class ModelTests(unittest.TestCase):
    """Model and reconciliation tests."""

    def test_service_identity_uses_service_id_only(self) -> None:
        identity = ServiceIdentity("123456")

        self.assertEqual(identity.device_identifier, ("nitrado_gameserver", "service:123456"))
        self.assertEqual(identity.entity_unique_id("status"), "service:123456:status")
        self.assertEqual(
            identity.entity_unique_id("save_health", profile_id="palworld"),
            "service:123456:profile:palworld:save_health",
        )

    def test_reconcile_discovers_new_services_without_auto_add(self) -> None:
        result = reconcile_services(
            known={},
            discovered_service_ids=["123456"],
            missing_threshold=3,
            auto_add=False,
        )

        self.assertEqual(result.newly_discovered, {"123456"})
        self.assertTrue(result.managed["123456"].pending_discovery)

    def test_reconcile_imported_service_is_managed_without_auto_add(self) -> None:
        result = reconcile_services(
            known={},
            discovered_service_ids=["123456"],
            missing_threshold=3,
            auto_add=False,
            imported_service_ids=["123456"],
        )

        self.assertEqual(result.newly_discovered, set())
        self.assertFalse(result.managed["123456"].pending_discovery)
        self.assertFalse(result.managed["123456"].ignored)

    def test_reconcile_auto_added_service_is_not_pending_discovered(self) -> None:
        result = reconcile_services(
            known={},
            discovered_service_ids=["123456"],
            missing_threshold=3,
            auto_add=True,
        )

        self.assertEqual(result.newly_discovered, set())
        self.assertFalse(result.managed["123456"].pending_discovery)

    def test_reconcile_ignored_service_is_quiet(self) -> None:
        result = reconcile_services(
            known={},
            discovered_service_ids=["123456"],
            missing_threshold=3,
            auto_add=False,
            ignored_service_ids=["123456"],
        )

        self.assertEqual(result.newly_discovered, set())
        self.assertFalse(result.managed["123456"].pending_discovery)
        self.assertTrue(result.managed["123456"].ignored)

    def test_reconcile_missing_threshold_counts_successful_absence(self) -> None:
        known = {"123456": ManagedServiceState("123456", missing_count=2)}

        result = reconcile_services(
            known=known,
            discovered_service_ids=[],
            missing_threshold=3,
            auto_add=False,
        )

        self.assertEqual(result.newly_missing, {"123456"})
        self.assertIn("123456", result.removable_notice_services)
        self.assertFalse(result.managed["123456"].available)

    def test_reconcile_recovery_clears_missing_count(self) -> None:
        known = {"123456": ManagedServiceState("123456", missing_count=3, available=False)}

        result = reconcile_services(
            known=known,
            discovered_service_ids=["123456"],
            missing_threshold=3,
            auto_add=False,
        )

        self.assertEqual(result.recovered, {"123456"})
        self.assertEqual(result.managed["123456"].missing_count, 0)
        self.assertTrue(result.managed["123456"].available)


if __name__ == "__main__":
    unittest.main()
