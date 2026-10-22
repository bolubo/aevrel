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
    reason="the CPU4 proof uses gloo and Linux filesystems",
)
def test_cpu4_live_split_short_run(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence = evidence_dir / "live-split.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "LRS_CPU4_S": "2",
            "LRS_CPU4_CUTOVER": "4",
            "LRS_CPU4_STEPS": "8",
            "LRS_CPU4_BATCH_PER_RANK": "4",
            "LRS_CPU4_PER_RANK_DATA": "16",
            "LRS_CPU4_EVIDENCE": str(evidence_dir),
        }
    )

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lrs_cpu4_live.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, (
        completed.stdout + "\n" + completed.stderr
    )
    assert "CPU4 PASSED" in completed.stdout
    proof = json.loads(evidence.read_text(encoding="utf-8"))
    assert proof["topology"] == "4 -> 3 -> 4"
    assert proof["steps_lost"] == 0
    assert proof["global_stop"] is True
    assert proof["live_collective"] is False
    assert all(proof["checks"].values())
