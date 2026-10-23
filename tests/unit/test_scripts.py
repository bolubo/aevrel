from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "script",
    [
        "scripts/ab_target.py",
        "scripts/benchmark.py",
        "scripts/planner_benchmark.py",
        "scripts/summarize_reports.py",
        "scripts/gpu_source_save.py",
        "scripts/gpu_target_load.py",
        "scripts/gpu_codec_probe.py",
    ],
)
def test_script_help_is_available(script: str) -> None:
    if "gpu_" in script:
        pytest.importorskip("torch")
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
