#!/usr/bin/env sh
set -eu

root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
python="$root/.venv/bin/python"
if [ ! -x "$python" ]; then
  python="${PYTHON:-python3}"
fi
exec "$python" -m shardferry demo "$@"
