#!/usr/bin/env python3
"""Verify one annotated release tag and create its deterministic local archive."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path

try:
    from .release_version import read_pin
except ImportError:
    from release_version import read_pin


def fail(message: str) -> None:
    raise SystemExit(message)


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=not binary,
    )
    if result.returncode:
        stderr = result.stderr.decode() if binary else result.stderr
        fail(stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _tree_digest(root: Path, prefix: str) -> str:
    digest = hashlib.sha256()
    entries = str(_git(root, "ls-files", prefix)).splitlines()
    if not entries:
        fail(f"release tree has no tracked files below {prefix}")
    for relative in sorted(entries):
        path = root / relative
        content = path.read_bytes()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(str(path.stat().st_mode & 0o777).encode("ascii") + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def verify(root: Path, tag: str, output: Path) -> dict[str, object]:
    root = root.resolve()
    output = output.resolve()
    if Path(str(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        fail("release provenance requires a standalone repository root")
    if str(_git(root, "status", "--porcelain=v1", "--untracked-files=all")).strip():
        fail("release provenance requires a clean Git tree")

    pin = read_pin(root / "RELEASE-METADATA.json")
    version = str(pin["version"])
    if tag != version:
        fail(f"tag {tag!r} does not match pinned version {version!r}")
    if str(_git(root, "cat-file", "-t", f"refs/tags/{tag}")).strip() != "tag":
        fail("release tag must be annotated")
    head = str(_git(root, "rev-parse", "HEAD")).strip()
    tagged = str(_git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")).strip()
    if tagged != head:
        fail("release tag does not resolve to HEAD")

    component = root / "custom_components" / "nitrado_gameserver"
    manifest = json.loads((component / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != version:
        fail("source manifest does not match the pinned version")
    asset = version.replace(".", "-")
    if f'PANEL_ASSET_VERSION = "{asset}"' not in (component / "panel.py").read_text(encoding="utf-8"):
        fail("panel asset identity does not match the pinned version")
    if f"nitrado-game-server-panel-{asset}" not in (component / "frontend" / "nitrado-game-server-panel.js").read_text(
        encoding="utf-8"
    ):
        fail("frontend custom-element identity does not match the pinned version")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## {version}\n" not in changelog:
        fail("changelog does not own the pinned version")
    if not re.search(r"(?ms)^## Unreleased\n\n_No changes yet\._\n\n^## " + re.escape(version) + r"$", changelog):
        fail("changelog Unreleased section is not empty above the pinned release")

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"home-assistant-nitrado-gameserver-{version}.tar.gz"
    if archive.exists():
        fail(f"release archive already exists: {archive}")
    tar_bytes = bytes(
        _git(
            root,
            "archive",
            "--format=tar",
            f"--prefix=home-assistant-nitrado-gameserver-{version}/",
            tagged,
            binary=True,
        )
    )
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(tar_bytes)
    archive.write_bytes(compressed.getvalue())

    payload: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "commit": head,
        "tag": tag,
        "archive": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "runtime_tree_sha256": _tree_digest(root, "custom_components/nitrado_gameserver"),
    }
    provenance = output / f"PROVENANCE-{version}.json"
    provenance.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = verify(args.root, args.tag, args.output)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
