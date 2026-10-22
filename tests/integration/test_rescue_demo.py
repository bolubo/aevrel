from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the local rescue demo uses the Linux migration path",
)
def test_rescue_demo_interrupts_and_resumes(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    work = tmp_path / "rescue"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "rescue_demo.py"),
            "--work-dir",
            str(work),
            "--dim",
            "64",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    proof = json.loads((work / "rescue-proof.json").read_text())
    assert proof["resumed"] is True
    assert proof["source_unchanged"] is True
    assert proof["completed_nodes_before_resume"] >= 1
    assert proof["target_load"]["status"] == "ok"
    assert (work / "timeline.html").is_file()
