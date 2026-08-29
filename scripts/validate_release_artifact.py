#!/usr/bin/env python3
"""Validate the exact repository tree intended for GitHub and HACS."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

# The validator is shipped inside the artifact and is expected to be runnable
# from there.  Suppress its helper import bytecode so validation cannot create
# the very generated content it is designed to reject.
sys.dont_write_bytecode = True

try:
    from .privacy_scan import scan_tree
    from .release_version import read_pin
except ImportError:  # Direct script execution places scripts/ on sys.path.
    from privacy_scan import scan_tree
    from release_version import read_pin

VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}\.\d+$")
HACS_MANIFEST_KEYS = {
    "content_in_root",
    "country",
    "filename",
    "hacs",
    "hide_default_branch",
    "homeassistant",
    "name",
    "persistent_directory",
    "zip_release",
}
FORBIDDEN_PARTS = {
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "dist",
    "htmlcov",
}
FORBIDDEN_PUBLIC_FILES = {
    "ADVERSARIAL-REPAIR-2026-08-15.md",
    "AUDIT-2026-08-14.md",
    "AUDIT-REMEDIATION-2026-08-15.md",
    "AUDIT-REVIEW-2026-08-15.md",
    "EXTENSIBILITY_ROADMAP.md",
    "FILESYSTEM-IMPLEMENTATION-REVIEW-2026-08-16.md",
    "IMPLEMENTATION_NOTES.md",
    "NATIVE-BACKUP-API-EVIDENCE-2026-08-16.md",
    "PROFILE-BOUNDARY-REVIEW-2026-08-15.md",
    "PROVIDER-FILESYSTEM-SPIKE-2026-08-16.md",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        fail(f"invalid JSON {path}: {err}")
    if not isinstance(value, dict):
        fail(f"JSON root must be an object: {path}")
    return value


def validate_hacs_manifest(hacs: dict[str, object]) -> None:
    """Reject keys and shapes that the published HACS schema does not accept."""

    unsupported = sorted(set(hacs) - HACS_MANIFEST_KEYS)
    if unsupported:
        fail(f"hacs.json contains unsupported keys: {', '.join(unsupported)}")
    if hacs.get("name") != "Nitrado Game Server":
        fail("hacs.json name is incorrect")
    homeassistant = hacs.get("homeassistant")
    if not isinstance(homeassistant, str) or not homeassistant.strip():
        fail("hacs.json homeassistant must be a non-empty version string")


def validate_checksums(root: Path) -> None:
    """Verify the checksum manifest covers the exact immutable artifact tree."""

    checksum_path = root / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as err:
        fail(f"could not read SHA256SUMS: {err}")
    expected: dict[str, str] = {}
    line_pattern = re.compile(r"^([0-9a-f]{64})  (\./.+)$")
    for line_number, line in enumerate(lines, start=1):
        match = line_pattern.fullmatch(line)
        if match is None:
            fail(f"invalid SHA256SUMS line {line_number}")
        digest, relative_value = match.groups()
        relative = relative_value.removeprefix("./")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            fail(f"SHA256SUMS path escapes the artifact: {relative}")
        if relative == "SHA256SUMS" or relative in expected:
            fail(f"invalid duplicate or self-referential SHA256SUMS entry: {relative}")
        expected[relative] = digest

    actual_files = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path != checksum_path
    }
    expected_files = set(expected)
    if expected_files != actual_files:
        missing = sorted(actual_files - expected_files)
        extra = sorted(expected_files - actual_files)
        details = []
        if missing:
            details.append(f"unlisted files: {', '.join(missing)}")
        if extra:
            details.append(f"missing files: {', '.join(extra)}")
        fail(f"SHA256SUMS file set does not match artifact: {'; '.join(details)}")
    for relative in sorted(actual_files):
        actual_digest = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual_digest != expected[relative]:
            fail(f"SHA256SUMS digest mismatch: {relative}")


def validate(root: Path) -> None:
    if not root.is_dir():
        fail(f"artifact is not a directory: {root}")

    required = {
        ".github/workflows/validate.yml",
        "CHANGELOG.md",
        "LICENSE",
        "README.md",
        "RELEASE-METADATA.json",
        "SHA256SUMS",
        "custom_components/nitrado_gameserver/manifest.json",
        "hacs.json",
        "release-files.txt",
    }
    missing = sorted(path for path in required if not (root / path).is_file())
    if missing:
        fail(f"release artifact is missing required files: {', '.join(missing)}")

    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            fail(f"release artifact contains a symlink: {relative}")
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            fail(f"release artifact contains generated/private content: {relative}")
        if path.name in FORBIDDEN_PUBLIC_FILES:
            fail(f"release artifact contains an internal work record: {relative}")
        if path.is_file() and (path.suffix in {".pyc", ".pyo"} or path.name.endswith("~")):
            fail(f"release artifact contains a generated file: {relative}")

    validate_checksums(root)

    manifest = load_json(root / "custom_components/nitrado_gameserver/manifest.json")
    hacs = load_json(root / "hacs.json")
    validate_hacs_manifest(hacs)
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        fail(f"manifest version is not Calendar Versioning: {version!r}")
    if manifest.get("domain") != "nitrado_gameserver":
        fail("manifest domain is incorrect")
    if manifest.get("config_flow") is not True:
        fail("manifest must declare config_flow=true")
    try:
        pin = read_pin(root / "RELEASE-METADATA.json")
    except ValueError as err:
        fail(str(err))
    if pin["version"] != version:
        fail("release metadata and manifest versions do not match")
    if not root.name.startswith(f"release-{version}"):
        fail("artifact directory name does not contain its pinned release version")

    asset_version = version.replace(".", "-")
    panel_host = (root / "custom_components/nitrado_gameserver/panel.py").read_text(encoding="utf-8")
    if f'PANEL_ASSET_VERSION = "{asset_version}"' not in panel_host:
        fail("panel asset version does not match the manifest")
    panel_source = (root / "custom_components/nitrado_gameserver/frontend/nitrado-game-server-panel.js").read_text(
        encoding="utf-8"
    )
    if f'const PANEL_ELEMENT = "nitrado-game-server-panel-{asset_version}";' not in panel_source:
        fail("panel custom-element identity does not match the manifest")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## {version}" not in changelog:
        fail(f"CHANGELOG.md does not contain manifest version {version}")

    findings = scan_tree(root)
    if findings:
        fail(f"release privacy scan failed: {findings[0]}")

    markdown_link = re.compile(r"\[[^]]+\]\(([^)]+\.md)(?:#[^)]+)?\)")
    for document in root.rglob("*.md"):
        text = document.read_text(encoding="utf-8")
        for target in markdown_link.findall(text):
            if "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                fail(f"markdown link escapes release root: {document.relative_to(root)} -> {target}")
            if not resolved.is_file():
                fail(f"markdown link target is missing: {document.relative_to(root)} -> {target}")


def main() -> None:
    if len(sys.argv) != 2:
        fail(f"usage: {Path(sys.argv[0]).name} ARTIFACT_ROOT")
    validate(Path(sys.argv[1]).resolve())
    print("release artifact validation passed")


if __name__ == "__main__":
    main()
