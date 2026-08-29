"""Tests for repair issue planning."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.nitrado_gameserver.api.nitrado import NitradoService
from custom_components.nitrado_gameserver.coordinator import NitradoAccountCoordinator
from custom_components.nitrado_gameserver.filesystem_journal import (
    FileTransactionOperation,
    JournalIssue,
    ServiceReference,
    TransactionState,
    UnresolvedTransactionFact,
)
from custom_components.nitrado_gameserver.models import ManagedServiceState
from custom_components.nitrado_gameserver.native_backup_journal import (
    NativeRestoreFact,
    NativeRestoreFailureCode,
    NativeRestoreIssue,
    NativeRestoreState,
    NativeRestoreTarget,
)
from custom_components.nitrado_gameserver.plugins.palworld import PalworldProfile
from custom_components.nitrado_gameserver.repairs import build_repair_issue_plan
from custom_components.nitrado_gameserver.runtime import ServiceRuntime


class DummyClient:
    """Unused client placeholder."""


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


class RepairPlanTests(unittest.TestCase):
    """Repair plan tests."""

    def test_pending_discovered_services_do_not_create_repair_issue(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.known = {
            "123": ManagedServiceState("123", pending_discovery=True),
            "456": ManagedServiceState("456", pending_discovery=True),
        }
        coordinator.discovered_services = {
            "123": service("123", "Palworld Xbox"),
            "456": service("456", "ARK"),
        }

        plan = build_repair_issue_plan(coordinator, issue_prefix="entry_")

        self.assertEqual(plan.desired, {})

    def test_missing_service_creates_per_service_issue(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.known = {
            "123": ManagedServiceState("123", missing_count=3, available=False),
        }

        plan = build_repair_issue_plan(coordinator, issue_prefix="entry_")

        self.assertEqual(tuple(plan.desired), ("entry_missing_service_123",))
        issue = plan.desired["entry_missing_service_123"]
        self.assertEqual(issue.translation_key, "missing_service")
        self.assertEqual(issue.translation_placeholders["service_id"], "123")
        self.assertEqual(issue.severity, "error")

    def test_recovered_service_marks_old_issue_stale(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.known = {
            "123": ManagedServiceState("123", missing_count=0, available=True),
        }

        plan = build_repair_issue_plan(
            coordinator,
            known_issue_ids={"entry_missing_service_123"},
            issue_prefix="entry_",
        )

        self.assertEqual(plan.desired, {})
        self.assertEqual(plan.stale_issue_ids, ("entry_missing_service_123",))

    def test_ignored_pending_service_does_not_create_issue(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.known = {
            "123": ManagedServiceState("123", pending_discovery=False, ignored=True),
        }

        plan = build_repair_issue_plan(coordinator)

        self.assertEqual(plan.desired, {})

    def test_unresolved_filesystem_transaction_creates_persistent_issue(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.filesystem_recovery_facts = (
            UnresolvedTransactionFact(
                issue=JournalIssue.RECOVERY_REQUIRED,
                transaction_id="abc123",
                service=ServiceReference("entry-1", "123"),
                operation=FileTransactionOperation.REPLACE_TREE,
                state=TransactionState.ROLLBACK_REQUIRED,
                recovery_available=True,
                created_at=datetime.now(UTC),
                failure_code=None,
            ),
        )

        plan = build_repair_issue_plan(coordinator, issue_prefix="entry_")

        issue = plan.desired["entry_filesystem_recovery_required_abc123"]
        self.assertEqual(issue.translation_key, "filesystem_recovery_required")
        self.assertEqual(issue.translation_placeholders["service_id"], "123")
        self.assertEqual(issue.translation_placeholders["operation"], "replace_tree")
        self.assertEqual(issue.severity, "error")

    def test_repeated_transport_failure_creates_service_scoped_issue(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.services = {
            "123": SimpleNamespace(service=service("123", "Palworld Xbox"), server=None),
        }
        coordinator.profile_transport.ftp._failure_counts["123"] = 2
        coordinator.profile_transport.ftp._last_failure_code["123"] = "credentials_rejected"

        plan = build_repair_issue_plan(coordinator, issue_prefix="entry_")

        issue = plan.desired["entry_ftp_credentials_unavailable_123"]
        self.assertEqual(issue.translation_key, "ftp_credentials_unavailable")
        self.assertEqual(issue.translation_placeholders["service_id"], "123")
        self.assertEqual(issue.severity, "error")

    def test_unresolved_palworld_security_choice_creates_fixable_repair(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        runtime = ServiceRuntime(
            ManagedServiceState("123"),
            service=service("123", "Palworld Xbox"),
            profile=PalworldProfile(),
        )
        coordinator.services = {"123": runtime}

        plan = build_repair_issue_plan(coordinator, issue_prefix="entry_")

        issue = plan.desired["entry_profile_option_unacknowledged_123_allow_insecure_rest"]
        self.assertTrue(issue.is_fixable)
        self.assertEqual(issue.translation_key, "profile_option_acknowledgement_required")
        self.assertEqual(issue.severity, "warning")
        self.assertEqual(issue.data["action"], "profile_option_acknowledgement")

    def test_provider_store_and_native_restore_repairs_are_errors(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        target = NativeRestoreTarget(
            account_entry_id="entry-1",
            service_id="123",
            folder="palworldxb",
            backup_id="7",
            expected_size_bytes=42,
            expected_created_at="2026-08-25T12:00:00Z",
            expected_status="ready",
        )
        native_fact = NativeRestoreFact(
            issue=NativeRestoreIssue.RESTORE_REVIEW_REQUIRED,
            operation_id="operation-1",
            target=target,
            state=NativeRestoreState.OUTCOME_UNKNOWN,
            created_at=datetime.now(UTC),
            failure_code=NativeRestoreFailureCode.OBSERVATION_TIMEOUT,
        )

        plan = build_repair_issue_plan(
            coordinator,
            issue_prefix="entry_",
            provider_grant_store_error="corrupt",
            native_backup_facts=(native_fact,),
        )

        self.assertEqual(plan.desired["entry_provider_grant_store_corrupt"].severity, "error")
        self.assertEqual(
            plan.desired["entry_native_backup_restore_review_required_operation-1"].severity,
            "error",
        )

    def test_explicit_legacy_palworld_choice_does_not_create_repair(self) -> None:
        coordinator = NitradoAccountCoordinator(DummyClient())  # type: ignore[arg-type]
        coordinator.options.profile_options = {"palworld": {"allow_insecure_rest": {"123": False}}}
        runtime = ServiceRuntime(
            ManagedServiceState("123"),
            service=service("123", "Palworld Xbox"),
            profile=PalworldProfile(),
        )
        coordinator.services = {"123": runtime}

        plan = build_repair_issue_plan(coordinator, issue_prefix="entry_")

        self.assertNotIn("entry_profile_option_unacknowledged_123_allow_insecure_rest", plan.desired)


if __name__ == "__main__":
    unittest.main()
