#!/usr/bin/env python3
"""Validate that a curated public repository is standalone and private-data free."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

try:
    from .privacy_scan import scan_tree
except ImportError:
    from privacy_scan import scan_tree

FORBIDDEN_PARTS = {
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-ha",
    ".venv-unit",
    "__pycache__",
    "backups",
    "dist",
    "node_modules",
    "reports",
}
FORBIDDEN_PUBLIC_FILES = {
    "ADVERSARIAL-REPAIR-2026-08-15.md",
    "ADVERSARIAL-REPAIR-PLAN-2026-08-25.md",
    "AUDIT-2026-08-14.md",
    "AUDIT-REMEDIATION-2026-08-15.md",
    "AUDIT-REVIEW-2026-08-15.md",
    "DEPLOYMENT-CHECKPOINT-2026-08-25.md",
    "EXTENSIBILITY_ROADMAP.md",
    "FILESYSTEM-IMPLEMENTATION-REVIEW-2026-08-16.md",
    "IMPLEMENTATION_NOTES.md",
    "NATIVE-BACKUP-API-EVIDENCE-2026-08-16.md",
    "PROFILE-BOUNDARY-REVIEW-2026-08-15.md",
    "PROVIDER-FILESYSTEM-SPIKE-2026-08-16.md",
    "PUBLIC-RELEASE-READINESS-AUDIT-2026-08-26.md",
    "PUBLIC-RELEASE-REPAIR-PLAN-2026-08-26.md",
    "RELEASE-UIUX-CHECKPOINT-2026-08-25.md",
    "UI-UX-ADVERSARIAL-AUDIT-2026-08-25.md",
    "UI-UX-REPAIR-PLAN-2026-08-25.md",
    "UI-UX-REPAIR-REPORT-2026-08-25.md",
}


def fail(message: str) -> None:
    raise SystemExit(message)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True, text=True)
    if result.returncode:
        fail(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def validate(root: Path, *, require_clean: bool, allow_remote: bool = False) -> None:
    root = root.resolve()
    if not root.is_dir():
        fail(f"repository root is not a directory: {root}")
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        fail("repository is not a standalone Git root")
    if not allow_remote and _git(root, "remote"):
        fail("local publication candidate must not have a Git remote")
    if require_clean and _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        fail("repository tree is not clean")

    required = {
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/workflows/validate.yml",
        "COMPATIBILITY.md",
        "CONTRIBUTING.md",
        "LIFECYCLE.md",
        "PRIVACY.md",
        "SECURITY.md",
        "SUPPORT.md",
        "browser_tests/run_uiux_acceptance.mjs",
        "custom_components/nitrado_gameserver/manifest.json",
        "frontend_tests/profile_cockpits.test.js",
        "ha_tests/test_integration_lifecycle.py",
        "package-lock.json",
        "package.json",
        "requirements_test.txt",
        "uv.lock",
    }
    missing = sorted(path for path in required if not (root / path).is_file())
    if missing:
        fail(f"public repository is missing required files: {', '.join(missing)}")

    files = [path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts]
    relative_files = {path.relative_to(root).as_posix() for path in files}
    tracked = set(_git(root, "ls-files").splitlines())
    if tracked != relative_files:
        missing_from_git = sorted(relative_files - tracked)
        missing_from_tree = sorted(tracked - relative_files)
        fail(
            "Git index does not match the curated tree: "
            f"untracked={missing_from_git[:10]!r} missing={missing_from_tree[:10]!r}"
        )

    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if path.is_symlink():
            fail(f"public repository contains a symlink: {relative}")
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            fail(f"public repository contains generated/private content: {relative}")
        if path.name in FORBIDDEN_PUBLIC_FILES:
            fail(f"public repository contains an internal work record: {relative}")
    findings = scan_tree(root)
    if findings:
        fail(f"public repository privacy scan failed: {findings[0]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a configured Git remote for CI or clean-clone validation",
    )
    args = parser.parse_args()
    validate(args.root, require_clean=args.require_clean, allow_remote=args.allow_remote)
    print("public repository validation passed")


if __name__ == "__main__":
    main()
