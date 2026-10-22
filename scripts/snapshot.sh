#!/usr/bin/env bash
set -euo pipefail

phase="${1:?usage: SHARDFERRY_SNAPSHOT_ROOT=/path/to/parent snapshot.sh <phase>}"
root="${SHARDFERRY_SNAPSHOT_ROOT:-$(pwd)}"
if [ "$(basename "$root")" = "shardferry" ]; then
  root="$(dirname "$root")"
fi
timestamp="${2:-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$root/snapshots"
out="$root/snapshots/shardferry-${phase}-${timestamp}.tar.gz"
tar czf "$out" \
  --exclude='shardferry/.venv' \
  --exclude='shardferry/.pytest_cache' \
  --exclude='*__pycache__*' \
  --exclude='*.pyc' \
  -C "$root" shardferry
echo "$out"
