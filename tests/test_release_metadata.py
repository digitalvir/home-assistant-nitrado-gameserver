"""Regression checks for the public GitHub/HACS repository shape."""

from __future__ import annotations

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    """Keep local release metadata aligned with hassfest and supply-chain policy."""

    def test_manifest_key_order_matches_hassfest(self) -> None:
        manifest = json.loads(
            (ROOT / "custom_components/nitrado_gameserver/manifest.json").read_text(encoding="utf-8"),
            object_pairs_hook=dict,
        )
        keys = list(manifest)
        self.assertEqual(keys[:2], ["domain", "name"])
        self.assertEqual(keys[2:], sorted(keys[2:]))

    def test_translation_source_matches_english_translation(self) -> None:
        strings = json.loads((ROOT / "custom_components/nitrado_gameserver/strings.json").read_text(encoding="utf-8"))
        english = json.loads(
            (ROOT / "custom_components/nitrado_gameserver/translations/en.json").read_text(encoding="utf-8")
        )
        self.assertEqual(strings, english)
        self.assertEqual(strings["config"]["flow_title"], "{name}")
        self.assertNotIn("flow_title", strings)

    def test_services_use_filtered_device_fields_not_filtered_targets(self) -> None:
        services = (ROOT / "custom_components/nitrado_gameserver/services.yaml").read_text(encoding="utf-8")
        self.assertNotRegex(services, r"(?m)^  target:\n    device:")
        self.assertEqual(services.count("      integration: nitrado_gameserver"), 4)

    def test_github_actions_are_commit_pinned(self) -> None:
        workflow = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        uses = re.findall(r"(?m)^\s+- uses: ([^\s#]+)", workflow)
        self.assertTrue(uses)
        for value in uses:
            _, separator, revision = value.rpartition("@")
            self.assertEqual(separator, "@")
            self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_release_allowlist_excludes_internal_work_records(self) -> None:
        allowlist = set((ROOT / "release-files.txt").read_text(encoding="utf-8").splitlines())
        self.assertFalse(
            allowlist
            & {
                "IMPLEMENTATION_NOTES.md",
                "FILESYSTEM-IMPLEMENTATION-REVIEW-2026-08-16.md",
                "PROVIDER-FILESYSTEM-SPIKE-2026-08-16.md",
                "NATIVE-BACKUP-API-EVIDENCE-2026-08-16.md",
            }
        )

    def test_release_allowlist_documents_new_cockpit_assets(self) -> None:
        allowlist = (ROOT / "release-files.txt").read_text(encoding="utf-8")
        self.assertIn("custom_components/nitrado_gameserver/cockpit.py", allowlist)
        self.assertIn("custom_components/nitrado_gameserver/frontend/palworld-cockpit.js", allowlist)

    def test_python_resolution_inputs_are_exact_and_locked(self) -> None:
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        group = set(configuration["dependency-groups"]["test"])
        requirements = set((ROOT / "requirements_test.txt").read_text(encoding="utf-8").splitlines())
        self.assertEqual(group, requirements)
        lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
        for requirement in sorted(requirements):
            name, version = requirement.split("==", 1)
            self.assertRegex(lock, rf'(?ms)^name = "{re.escape(name)}"$.*?^version = "{re.escape(version)}"$')


if __name__ == "__main__":
    unittest.main()
