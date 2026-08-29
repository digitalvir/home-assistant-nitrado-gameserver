#!/usr/bin/env python3
"""Pin and stamp a clean standalone source tree before its release commit."""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

try:
    from .release_version import write_pin
    from .stamp_release_artifact import stamp
except ImportError:
    from release_version import write_pin
    from stamp_release_artifact import stamp


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def prepare(root: Path, *, build_epoch: int | None = None) -> str:
    root = root.resolve()
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("release preparation requires a standalone repository root")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("release preparation requires a clean Git tree")

    pin_path = root / "RELEASE-METADATA.json"
    if pin_path.exists():
        raise ValueError("RELEASE-METADATA.json already exists; start from a clean development commit")
    payload = write_pin(pin_path, int(time.time()) if build_epoch is None else build_epoch)
    version = str(payload["version"])
    try:
        if (
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"refs/tags/{version}"],
                check=False,
            ).returncode
            == 0
        ):
            raise ValueError(f"release tag already exists: {version}")
        stamp(root)
    except Exception:
        pin_path.unlink(missing_ok=True)
        _git(root, "restore", "--worktree", ".")
        raise
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--epoch", type=int)
    args = parser.parse_args()
    try:
        version = prepare(args.root, build_epoch=args.epoch)
    except (OSError, ValueError) as err:
        raise SystemExit(str(err)) from err
    print(version)


if __name__ == "__main__":
    main()
