from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from aevrel.cdp import TargetLock


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock crash harness")
def test_target_lock_is_released_when_holder_dies(tmp_path: Path) -> None:
    root = tmp_path / "target"
    marker = tmp_path / "locked"
    worker = Path(__file__).resolve().parents[1] / "fixtures" / "target_lock_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            "--root",
            str(root),
            "--marker",
            str(marker),
        ],
        cwd=Path(__file__).resolve().parents[2],
    )
    try:
        deadline = time.monotonic() + 2
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.is_file()
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    acquired: list[bool] = []

    def acquire() -> None:
        with TargetLock(root):
            acquired.append(True)

    thread = threading.Thread(target=acquire, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert acquired == [True]
