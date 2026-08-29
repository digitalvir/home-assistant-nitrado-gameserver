#!/usr/bin/env python3
"""Derive and verify pinned Nitrado release versions."""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RELEASE_TIMEZONE = "America/New_York"
BUCKET_SECONDS = 2
PIN_SCHEMA_VERSION = 1


def release_version(build_epoch: int) -> str:
    """Return the release version for one pinned Unix build timestamp."""

    if isinstance(build_epoch, bool) or not isinstance(build_epoch, int) or build_epoch < 0:
        raise ValueError("build_epoch must be a non-negative integer Unix timestamp")
    timezone = ZoneInfo(RELEASE_TIMEZONE)
    local_build = datetime.fromtimestamp(build_epoch, timezone)
    local_midnight = datetime(
        local_build.year,
        local_build.month,
        local_build.day,
        tzinfo=timezone,
    )
    elapsed_seconds = build_epoch - math.floor(local_midnight.timestamp())
    bucket = elapsed_seconds // BUCKET_SECONDS
    return f"{local_build.year}.{local_build.month}.{local_build.day}.{bucket}"


def pin_payload(build_epoch: int) -> dict[str, object]:
    """Return the immutable metadata persisted with a release candidate."""

    return {
        "schema_version": PIN_SCHEMA_VERSION,
        "version": release_version(build_epoch),
        "build_epoch": build_epoch,
        "timezone": RELEASE_TIMEZONE,
        "bucket_seconds": BUCKET_SECONDS,
    }


def validate_pin(payload: object) -> dict[str, object]:
    """Validate a persisted pin and return its normalized payload."""

    if not isinstance(payload, dict):
        raise ValueError("release pin must be a JSON object")
    expected_keys = {
        "schema_version",
        "version",
        "build_epoch",
        "timezone",
        "bucket_seconds",
    }
    if set(payload) != expected_keys:
        raise ValueError("release pin has an unsupported shape")
    if payload["schema_version"] != PIN_SCHEMA_VERSION:
        raise ValueError("release pin schema version is unsupported")
    if payload["timezone"] != RELEASE_TIMEZONE:
        raise ValueError("release pin timezone is unsupported")
    if payload["bucket_seconds"] != BUCKET_SECONDS:
        raise ValueError("release pin bucket size is unsupported")
    build_epoch = payload["build_epoch"]
    if isinstance(build_epoch, bool) or not isinstance(build_epoch, int):
        raise ValueError("release pin build_epoch must be an integer")
    expected_version = release_version(build_epoch)
    if payload["version"] != expected_version:
        raise ValueError(f"release pin version does not match its build epoch: expected {expected_version}")
    return dict(payload)


def read_pin(path: Path) -> dict[str, object]:
    """Read and validate a release pin."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValueError(f"invalid release pin {path}: {err}") from err
    return validate_pin(payload)


def write_pin(path: Path, build_epoch: int) -> dict[str, object]:
    """Create a new release pin without overwriting an existing candidate."""

    payload = pin_payload(build_epoch)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(serialized)
    except FileExistsError as err:
        raise ValueError(f"release pin already exists: {path}") from err
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    derive = commands.add_parser("derive", help="derive a version without creating a pin")
    derive.add_argument("--epoch", type=int, required=True)

    pin = commands.add_parser("pin", help="create a new immutable release pin")
    pin.add_argument("--output", type=Path, required=True)
    pin.add_argument("--epoch", type=int, default=None)

    verify = commands.add_parser("verify", help="validate and inspect an existing pin")
    verify.add_argument("pin", type=Path)
    verify.add_argument("--print", choices=("version", "epoch", "json"), default="version")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "derive":
            print(release_version(args.epoch))
            return
        if args.command == "pin":
            build_epoch = int(time.time()) if args.epoch is None else args.epoch
            payload = write_pin(args.output, build_epoch)
            print(payload["version"])
            return
        payload = read_pin(args.pin)
        if args.print == "epoch":
            print(payload["build_epoch"])
        elif args.print == "json":
            print(json.dumps(payload, sort_keys=True))
        else:
            print(payload["version"])
    except ValueError as err:
        raise SystemExit(str(err)) from err


if __name__ == "__main__":
    main()
