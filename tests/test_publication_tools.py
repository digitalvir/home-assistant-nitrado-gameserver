"""Tests for public-repository privacy and release helpers."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.privacy_scan import scan_tree
from scripts.validate_public_repository import validate
from scripts.validate_release_artifact import validate_hacs_manifest
from scripts.verify_hacs_release_channel import validate_release_channel
from scripts.verify_local_hacs_lifecycle import _safe_extract


class PrivacyScanTests(unittest.TestCase):
    def test_rejects_private_material_in_every_shipped_text_family(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            (root / "frontend.js").write_text('const token = "' + "ghp_" + 'abcdefghijklmnopqrstuvwxyz";\n')
            (root / "page.html").write_text("<p>/" + "home/private-user/project</p>\n")
            (root / "style.css").write_text("/* 192.168." + "44.5 */\n")
            self.assertEqual(len(scan_tree(root)), 3)

    def test_allows_only_reviewed_exact_hostile_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            fixture = "Password=" + r"old-secret\ndifficulty=1\n"
            (root / "fixture.py").write_text(fixture)
            self.assertEqual(scan_tree(root), [])
            (root / "fixture.py").write_text('value = "password=' + 'actual-unreviewed-value"\n')
            self.assertEqual(scan_tree(root), ["fixture.py: likely password assignment"])


class LocalHacsArchiveSafetyTests(unittest.TestCase):
    def test_rejects_archive_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            archive = root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                payload = b"escape"
                member = tarfile.TarInfo("repository/../escape.txt")
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
            with self.assertRaisesRegex(SystemExit, "unsafe path"):
                _safe_extract(archive, root / "output")
            self.assertFalse((root / "escape.txt").exists())

    def test_rejects_archive_links(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            archive = root / "link.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("repository/runtime-link")
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                bundle.addfile(member)
            with self.assertRaisesRegex(SystemExit, "link or device"):
                _safe_extract(archive, root / "output")


class PublicRepositoryRemotePolicyTests(unittest.TestCase):
    @staticmethod
    def _git_with_remote(root: Path, *args: str) -> str:
        if args[0] == "rev-parse":
            return str(root)
        if args[0] == "remote":
            return "origin"
        if args[0] == "status":
            return ""
        raise AssertionError(f"unexpected Git command: {args!r}")

    def test_local_candidate_rejects_configured_remote(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value).resolve()
            with (
                patch(
                    "scripts.validate_public_repository._git",
                    side_effect=lambda candidate, *args: self._git_with_remote(candidate, *args),
                ),
                self.assertRaisesRegex(SystemExit, "must not have a Git remote"),
            ):
                validate(root, require_clean=False)

    def test_ci_checkout_allows_configured_remote(self) -> None:
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value).resolve()
            with (
                patch(
                    "scripts.validate_public_repository._git",
                    side_effect=lambda candidate, *args: self._git_with_remote(candidate, *args),
                ),
                patch("scripts.validate_public_repository.scan_tree", return_value=[]),
                self.assertRaisesRegex(SystemExit, "missing required files"),
            ):
                validate(root, require_clean=True, allow_remote=True)


class HacsManifestContractTests(unittest.TestCase):
    def test_accepts_only_current_published_contract(self) -> None:
        validate_hacs_manifest(
            {
                "name": "Nitrado Game Server",
                "homeassistant": "2026.8.2",
            }
        )

    def test_rejects_retired_hacs_keys(self) -> None:
        with self.assertRaisesRegex(SystemExit, "unsupported keys: domains, render_readme"):
            validate_hacs_manifest(
                {
                    "name": "Nitrado Game Server",
                    "homeassistant": "2026.8.2",
                    "domains": ["nitrado_gameserver"],
                    "render_readme": True,
                }
            )

    def test_workflow_uses_visibility_appropriate_hacs_gate(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("if: github.event.repository.private == true", workflow)
        self.assertIn("if: github.event.repository.private == false", workflow)
        self.assertIn("uses: hacs/action@", workflow)
        self.assertIn("+refs/tags/${GITHUB_REF_NAME}:refs/tags/${GITHUB_REF_NAME}", workflow)


class HacsReleaseChannelTests(unittest.TestCase):
    def test_accepts_published_release(self) -> None:
        self.assertEqual(
            validate_release_channel(
                [
                    {
                        "tag_name": "2026.8.29.1750",
                        "name": "2026.8.29.1750",
                        "draft": False,
                        "prerelease": False,
                    }
                ]
            ),
            "2026.8.29.1750",
        )

    def test_rejects_prerelease_only_feed_that_makes_hacs_show_commit(self) -> None:
        with self.assertRaisesRegex(SystemExit, "HACS will leave last_version empty"):
            validate_release_channel(
                [
                    {
                        "tag_name": "2026.8.29.1750",
                        "draft": False,
                        "prerelease": True,
                    },
                    {
                        "tag_name": "2026.8.27.16978",
                        "draft": False,
                        "prerelease": True,
                    },
                ]
            )

    def test_release_metadata_workflow_runs_on_release_changes(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "release-channel.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("types: [published, released, prereleased, edited]", workflow)
        self.assertIn("verify_hacs_release_channel.py", workflow)
