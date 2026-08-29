#!/usr/bin/env python3
"""Stamp one source tree with its already-pinned release identity."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    from .release_version import read_pin
except ImportError:  # Direct script execution places scripts/ on sys.path.
    from release_version import read_pin


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    """Replace exactly one release marker in a text file."""

    source = path.read_text(encoding="utf-8")
    compiled = re.compile(pattern, flags=re.MULTILINE)
    if len(compiled.findall(source)) != 1:
        raise ValueError(f"expected exactly one release marker in {path}")
    updated = compiled.sub(replacement, source, count=1)
    path.write_text(updated, encoding="utf-8")


def stamp(root: Path) -> str:
    """Stamp manifest, changelog, Python host, and JavaScript element identity.

    Release preparation promotes the complete Unreleased body into the new
    version.  It never renames an older release heading.
    """

    pin = read_pin(root / "RELEASE-METADATA.json")
    version = str(pin["version"])
    asset_version = version.replace(".", "-")
    component = root / "custom_components" / "nitrado_gameserver"
    manifest_path = component / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_version = manifest.get("version")
    if not isinstance(old_version, str):
        raise ValueError("source manifest does not contain a version string")
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    changelog = root / "CHANGELOG.md"
    changelog_source = changelog.read_text(encoding="utf-8")
    unreleased = re.search(
        r"(?ms)^## Unreleased\n\n(?P<body>.*?)(?=^## |\Z)",
        changelog_source,
    )
    if unreleased is None:
        raise ValueError("CHANGELOG.md must contain an Unreleased section before the first version")
    body = unreleased.group("body").strip()
    if not body or body == "_No changes yet._":
        raise ValueError("CHANGELOG.md Unreleased section is empty")
    replacement = f"## Unreleased\n\n_No changes yet._\n\n## {version}\n\n{body}\n\n"
    changelog.write_text(
        changelog_source[: unreleased.start()] + replacement + changelog_source[unreleased.end() :],
        encoding="utf-8",
    )
    replace_once(
        component / "panel.py",
        r'^PANEL_ASSET_VERSION = "[^"]+"$',
        f'PANEL_ASSET_VERSION = "{asset_version}"',
    )
    replace_once(
        component / "frontend" / "nitrado-game-server-panel.js",
        r'^const PANEL_ELEMENT = "nitrado-game-server-panel-[^"]+";$',
        f'const PANEL_ELEMENT = "nitrado-game-server-panel-{asset_version}";',
    )
    return version


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} ARTIFACT_ROOT")
    try:
        version = stamp(Path(sys.argv[1]).resolve())
    except (OSError, ValueError, json.JSONDecodeError) as err:
        raise SystemExit(str(err)) from err
    print(f"stamped release artifact {version}")


if __name__ == "__main__":
    main()
