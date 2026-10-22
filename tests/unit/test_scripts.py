from __future__ import annotations

import json
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


def test_cdp_benchmark_runs_target_ready_probe(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "cdp_benchmark.py"),
            "--size-mb",
            "2",
            "--source-world",
            "4",
            "--target-world",
            "2",
            "--versions",
            "2",
            "--work-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "cdp-benchmark.json").read_text())
    assert report["target_probe"] is True
    assert report["cutover"]["target_ready_seconds"] >= 0
