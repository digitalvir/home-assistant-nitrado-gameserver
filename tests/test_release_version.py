"""Tests for pinned, DST-safe release identities and artifact stamping."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.curate_public_repository import curate
from scripts.release_version import pin_payload, read_pin, release_version, write_pin
from scripts.stamp_release_artifact import stamp

ROOT = Path(__file__).resolve().parents[1]
TIMEZONE = ZoneInfo("America/New_York")


def epoch(year: int, month: int, day: int, hour: int, minute: int, second: int, *, fold: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=TIMEZONE, fold=fold).timestamp())


def prepared_candidate(parent: Path, build_epoch: int) -> Path:
    root = parent / "candidate"
    curate(ROOT, root)
    changelog = root / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8").replace(
            "## Unreleased\n\n_No changes yet._",
            "## Unreleased\n\n- Builder regression fixture.",
            1,
        ),
        encoding="utf-8",
    )
    write_pin(root / "RELEASE-METADATA.json", build_epoch)
    stamp(root)
    return root


class ReleaseVersionTests(unittest.TestCase):
    def test_ordinary_day_uses_elapsed_epoch_seconds(self) -> None:
        self.assertEqual(release_version(epoch(2026, 8, 21, 12, 0, 0)), "2026.8.21.21600")

    def test_spring_transition_remains_monotonic(self) -> None:
        before = epoch(2026, 3, 8, 1, 59, 58)
        after = epoch(2026, 3, 8, 3, 0, 0)
        self.assertEqual(after - before, 2)
        self.assertEqual(release_version(before), "2026.3.8.3599")
        self.assertEqual(release_version(after), "2026.3.8.3600")

    def test_fall_transition_remains_monotonic_when_wall_clock_repeats(self) -> None:
        first = epoch(2026, 11, 1, 1, 59, 58, fold=0)
        second = epoch(2026, 11, 1, 1, 0, 0, fold=1)
        self.assertEqual(second - first, 2)
        self.assertEqual(release_version(first), "2026.11.1.3599")
        self.assertEqual(release_version(second), "2026.11.1.3600")

    def test_two_seconds_in_one_bucket_collide(self) -> None:
        first = epoch(2026, 8, 21, 12, 0, 0)
        self.assertEqual(release_version(first), release_version(first + 1))
        self.assertNotEqual(release_version(first), release_version(first + 2))

    def test_pin_rejects_tampered_version(self) -> None:
        payload = pin_payload(epoch(2026, 8, 21, 12, 0, 0))
        payload["version"] = "2026.8.21.9"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pin.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match"):
                read_pin(path)

    def test_pin_creation_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pin.json"
            build_epoch = epoch(2026, 8, 21, 12, 0, 0)
            write_pin(path, build_epoch)
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_pin(path, build_epoch + 2)

    def test_builder_fails_when_pinned_version_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build_epoch = epoch(2026, 8, 21, 12, 0, 0)
            root = prepared_candidate(Path(directory), build_epoch)
            version = release_version(build_epoch)
            (root / f"release-{version}-rc0").mkdir()
            result = subprocess.run(
                [
                    str(root / "scripts/build_release_artifact.sh"),
                    str(root / "release-{version}-rc1"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("release version collision", result.stderr)

    def test_concurrent_builders_reserve_one_version_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build_epoch = epoch(2026, 8, 21, 12, 0, 0)
            root = prepared_candidate(Path(directory), build_epoch)
            commands = [
                [
                    str(root / "scripts/build_release_artifact.sh"),
                    str(root / f"release-{{version}}-rc{candidate}"),
                ]
                for candidate in (1, 2)
            ]
            processes = [
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for command in commands
            ]
            results = [process.communicate(timeout=30) for process in processes]

            self.assertEqual(sorted(process.returncode for process in processes), [0, 2])
            failure_stderr = next(
                stderr for process, (_, stderr) in zip(processes, results, strict=True) if process.returncode
            )
            self.assertIn("release version collision", failure_stderr)
            version = release_version(build_epoch)
            self.assertEqual(len(list(root.glob(f"release-{version}-rc*"))), 1)
            self.assertFalse((root / f".release-{version}.reservation").exists())

    def test_builder_checksum_manifest_covers_exact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build_epoch = epoch(2026, 8, 21, 12, 0, 0)
            root = prepared_candidate(Path(directory), build_epoch)
            version = release_version(build_epoch)
            artifact = root / f"release-{version}-rc1"
            result = subprocess.run(
                [
                    str(root / "scripts/build_release_artifact.sh"),
                    str(root / "release-{version}-rc1"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            checksum_manifest = artifact / "SHA256SUMS"
            self.assertTrue(checksum_manifest.is_file())

            readme = artifact / "README.md"
            original = readme.read_bytes()
            readme.write_bytes(original + b"\ntampered\n")
            tampered = subprocess.run(
                [str(ROOT / "scripts/validate_release_artifact.py"), str(artifact)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("digest mismatch", tampered.stderr)

            readme.write_bytes(original)
            (artifact / "unlisted.txt").write_text("not checksummed", encoding="utf-8")
            unlisted = subprocess.run(
                [str(ROOT / "scripts/validate_release_artifact.py"), str(artifact)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unlisted.returncode, 0)
            self.assertIn("file set does not match", unlisted.stderr)


class ArtifactStampTests(unittest.TestCase):
    def test_one_pin_stamps_every_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component = root / "custom_components/nitrado_gameserver"
            (component / "frontend").mkdir(parents=True)
            (root / "RELEASE-METADATA.json").write_text(
                json.dumps(pin_payload(epoch(2026, 8, 21, 12, 0, 0))),
                encoding="utf-8",
            )
            (component / "manifest.json").write_text(json.dumps({"version": "2026.8.21.7"}), encoding="utf-8")
            (root / "CHANGELOG.md").write_text(
                "# Changelog\n\n## Unreleased\n\n- Added release fixture.\n\n## 2026.8.21.7\n",
                encoding="utf-8",
            )
            (component / "panel.py").write_text('PANEL_ASSET_VERSION = "profile-cockpits"\n', encoding="utf-8")
            panel = component / "frontend/nitrado-game-server-panel.js"
            panel.write_text(
                'const PANEL_ELEMENT = "nitrado-game-server-panel-profile-cockpits";\n',
                encoding="utf-8",
            )

            version = stamp(root)

            self.assertEqual(version, "2026.8.21.21600")
            manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], version)
            self.assertIn("## 2026.8.21.21600", (root / "CHANGELOG.md").read_text())
            self.assertIn(
                'PANEL_ASSET_VERSION = "2026-8-21-21600"',
                (component / "panel.py").read_text(),
            )
            self.assertIn(
                'const PANEL_ELEMENT = "nitrado-game-server-panel-2026-8-21-21600";',
                panel.read_text(),
            )


if __name__ == "__main__":
    unittest.main()
