#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 'DESTINATION-{version}'" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination_template="$1"

if [[ "$destination_template" != *'{version}'* ]]; then
  echo "destination must contain the literal {version} placeholder" >&2
  exit 2
fi
if [[ "$(basename "$destination_template")" != release-'{version}'* ]]; then
  echo "destination basename must begin with release-{version}" >&2
  exit 2
fi

version_reservation=""
cleanup() {
  if [[ -n "$version_reservation" && -d "$version_reservation" ]]; then
    rmdir -- "$version_reservation" 2>/dev/null || true
  fi
}
trap cleanup EXIT

pin_source="$repo_root/RELEASE-METADATA.json"
if [[ ! -f "$pin_source" ]]; then
  echo "source tree is not release-prepared: RELEASE-METADATA.json is missing" >&2
  exit 2
fi
version="$(python3 "$repo_root/scripts/release_version.py" verify "$pin_source")"

destination="${destination_template//\{version\}/$version}"

if [[ -e "$destination" ]]; then
  echo "destination already exists: $destination" >&2
  exit 2
fi

destination_parent="$(dirname "$destination")"
mkdir -p "$destination_parent"
version_reservation="$destination_parent/.release-$version.reservation"
if ! mkdir -- "$version_reservation" 2>/dev/null; then
  echo "release version collision in $destination_parent: $version" >&2
  exit 2
fi
shopt -s nullglob
same_version_candidates=("$destination_parent"/release-"$version"*)
shopt -u nullglob
if (( ${#same_version_candidates[@]} > 0 )); then
  echo "release version collision in $destination_parent: $version" >&2
  exit 2
fi

if ! mkdir -- "$destination" 2>/dev/null; then
  echo "destination already exists: $destination" >&2
  exit 2
fi
cp -a "$pin_source" "$destination/RELEASE-METADATA.json"

while IFS= read -r item; do
  [[ -n "$item" ]] || continue
  [[ "$item" != \#* ]] || continue
  source_path="$repo_root/$item"
  if [[ ! -e "$source_path" ]]; then
    echo "release item is missing: $item" >&2
    exit 1
  fi
  tar \
    --create \
    --file=- \
    --directory="$repo_root" \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='.mypy_cache' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    -- "$item" \
    | tar --extract --file=- --directory="$destination"
done < "$repo_root/release-files.txt"

(
  cd "$destination"
  find . -type f ! -name SHA256SUMS -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)
python3 "$repo_root/scripts/validate_release_artifact.py" "$destination"

echo "release artifact ready: $destination"
