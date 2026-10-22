#!/usr/bin/env bash
set -euo pipefail

phase="${1:?usage: snapshot.sh <phase>}"
root="/root/lines/oss/aicluster"
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
