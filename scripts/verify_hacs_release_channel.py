#!/usr/bin/env python3
"""Reject GitHub release metadata that makes HACS fall back to commit mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_release_channel(releases: list[dict[str, Any]]) -> str:
    """Return the newest HACS-trackable tag or abort for a prerelease-only feed."""
    published = [release for release in releases if not release.get("draft", False)]
    if not published:
        raise SystemExit("no published GitHub releases were found")

    trackable = [release for release in published if not release.get("prerelease", False)]
    if not trackable:
        raise SystemExit(
            "all published GitHub releases are marked prerelease; HACS will leave "
            "last_version empty and can install/display the default-branch commit SHA"
        )

    tag = trackable[0].get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        raise SystemExit("newest HACS-trackable release has no tag_name")
    return tag


def main() -> None:
    """Validate a GitHub releases API response captured as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("releases_json", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.releases_json.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise SystemExit("GitHub releases payload must be a JSON array of objects")

    tag = validate_release_channel(payload)
    print(f"HACS release channel has a versioned baseline: {tag}")


if __name__ == "__main__":
    main()
