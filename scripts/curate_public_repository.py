#!/usr/bin/env python3
"""Create a standalone local repository from the explicit public-source allowlist."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(message)


def curate(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        fail(f"destination already exists: {destination}")
    if destination == source or source in destination.parents or destination in source.parents:
        fail("destination must be independent of the source tree")

    allowlist = source / "public-source-files.txt"
    items = [
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    destination.mkdir(parents=True)
    try:
        for item in items:
            relative = Path(item)
            if relative.is_absolute() or ".." in relative.parts:
                fail(f"unsafe public-source allowlist entry: {item}")
            source_path = source / relative
            if not source_path.exists():
                fail(f"public-source allowlist item is missing: {item}")
            for candidate in (source_path, *source_path.rglob("*")) if source_path.is_dir() else (source_path,):
                if candidate.is_symlink():
                    fail(f"public-source allowlist contains a symlink: {candidate.relative_to(source)}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source_path.is_dir():
                shutil.copytree(
                    source_path,
                    target,
                    ignore=shutil.ignore_patterns(
                        "__pycache__",
                        ".pytest_cache",
                        ".ruff_cache",
                        ".mypy_cache",
                        "*.pyc",
                        "*.pyo",
                    ),
                )
            else:
                shutil.copy2(source_path, target)
        subprocess.run(["git", "init", "--initial-branch=main", str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "add", "--all"], check=True)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    curate(args.source, args.destination)
    print(f"curated standalone repository: {args.destination.resolve()}")


if __name__ == "__main__":
    main()
