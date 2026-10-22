from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_gpu_codec_probe_runs_with_cpu_fallback() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gpu_codec_probe.py"),
            "--codec",
            "zlib",
            "--pattern",
            "zero",
            "--size",
            str(1024 * 1024),
            "--range-bytes",
            str(256 * 1024),
            "--max-ranges",
            "4",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["codec"] == "zlib"
    assert report["processed_ranges"] == 4
    assert report["raw_bytes"] == 1024 * 1024
    assert report["roundtrip_ok"] is True
    assert report["compression_ratio"] < 0.5
    assert report["frame_bytes"] < report["raw_bytes"]


def test_gpu_codec_probe_reports_missing_nvcomp_cleanly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gpu_codec_probe.py"),
            "--codec",
            "nvcomp-zstd",
            "--pattern",
            "zero",
            "--size",
            str(1024 * 1024),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        report = json.loads(result.stdout)
        assert report["ok"] is True
        return
    assert result.returncode == 2
    assert "nvcomp" in result.stderr.lower() or "cuda" in result.stderr.lower()
