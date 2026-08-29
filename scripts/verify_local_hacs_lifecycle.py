#!/usr/bin/env python3
"""Verify the exact annotated-tag runtime with a local HACS-shaped lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
RUNTIME_PARTS = ("custom_components", "nitrado_gameserver")


def fail(message: str) -> None:
    raise SystemExit(message)


def _digest_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _safe_extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        files = [member for member in members if member.isfile()]
        if len(files) > MAX_ARCHIVE_FILES or sum(member.size for member in files) > MAX_ARCHIVE_BYTES:
            fail("tag archive exceeds local lifecycle bounds")
        roots: set[str] = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                fail("tag archive contains an unsafe path")
            roots.add(path.parts[0])
            if member.issym() or member.islnk() or member.isdev():
                fail("tag archive contains a link or device")
        if len(roots) != 1:
            fail("tag archive must contain exactly one repository root")
        destination.mkdir(parents=True)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                fail("tag archive file could not be read")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    extracted = destination / next(iter(roots))
    if not extracted.is_dir():
        fail("tag archive did not extract its repository root")
    return extracted


def verify(archive: Path, artifact: Path, previous_runtime: Path | None = None) -> dict[str, Any]:
    archive = archive.resolve()
    artifact = artifact.resolve()
    artifact_runtime = artifact.joinpath(*RUNTIME_PARTS)
    if not archive.is_file() or not artifact_runtime.is_dir():
        fail("archive or artifact runtime is missing")

    with tempfile.TemporaryDirectory(prefix="nitrado-hacs-shaped-") as temporary:
        temporary_root = Path(temporary)
        extracted = _safe_extract(archive, temporary_root / "archive")
        archive_runtime = extracted.joinpath(*RUNTIME_PARTS)
        if not archive_runtime.is_dir():
            fail("tag archive does not contain the HACS runtime subtree")
        archive_digest = _digest_tree(archive_runtime)
        artifact_digest = _digest_tree(artifact_runtime)
        if archive_digest != artifact_digest:
            fail("tag archive and reviewed artifact runtime subtrees differ")

        config_root = temporary_root / "config"
        installed = config_root.joinpath(*RUNTIME_PARTS)
        installed.parent.mkdir(parents=True)
        if previous_runtime is not None:
            previous_runtime = previous_runtime.resolve()
            if not previous_runtime.is_dir():
                fail("previous runtime is missing")
            shutil.copytree(previous_runtime, installed)
            previous_manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
            shutil.rmtree(installed)
        else:
            previous_manifest = None
        shutil.copytree(archive_runtime, installed)
        installed_manifest = json.loads((installed / "manifest.json").read_text(encoding="utf-8"))
        if _digest_tree(installed) != archive_digest:
            fail("local HACS-shaped install changed runtime bytes")

        retained = config_root / ".storage" / "nitrado_gameserver-retained-sentinel"
        retained.parent.mkdir(parents=True)
        retained.write_text("HA-owned retained data", encoding="utf-8")
        removed = temporary_root / "code-uninstalled"
        installed.rename(removed)
        if not retained.is_file():
            fail("code uninstall simulation altered retained Home Assistant data")
        shutil.copytree(archive_runtime, installed)
        if (
            _digest_tree(installed) != archive_digest
            or retained.read_text(encoding="utf-8") != "HA-owned retained data"
        ):
            fail("reinstall did not restore exact code while preserving HA-owned data")

        version = installed_manifest.get("version")
        if not isinstance(version, str):
            fail("installed manifest has no version")
        result: dict[str, Any] = {
            "classification": "local_hacs_shaped_only",
            "version": version,
            "runtime_files": len(archive_digest),
            "archive_runtime_sha256": _combined_digest(archive_digest),
            "install_exact": True,
            "code_uninstall_preserved_ha_data": True,
            "reinstall_exact": True,
        }
        if previous_manifest is not None:
            result["previous_version"] = previous_manifest.get("version")
            result["upgrade_replaced_runtime_exactly"] = True
        return result


def _combined_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, value in sorted(files.items()):
        digest.update(path.encode("utf-8") + b"\0" + value.encode("ascii") + b"\0")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--previous-runtime", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.archive, args.artifact, args.previous_runtime), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
