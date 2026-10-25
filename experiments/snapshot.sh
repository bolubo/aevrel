#!/usr/bin/env bash
set -euo pipefail

phase="${1:?usage: AEVREL_SNAPSHOT_ROOT=/path/to/parent snapshot.sh <phase>}"
root="${AEVREL_SNAPSHOT_ROOT:-$(pwd)}"
if [ "$(basename "$root")" = "aevrel" ]; then
  root="$(dirname "$root")"
fi
timestamp="${2:-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$root/snapshots"
out="$root/snapshots/aevrel-${phase}-${timestamp}.tar.gz"
tar czf "$out" \
  --exclude='aevrel/.venv' \
  --exclude='aevrel/.pytest_cache' \
  --exclude='*__pycache__*' \
  --exclude='*.pyc' \
  -C "$root" aevrel
echo "$out"
