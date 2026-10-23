from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_repository_hygiene_check_passes() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_repo.py"), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["errors"] == []
