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
    assert report["recommendation"]["enabled"] is False


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


def test_gpu_codec_probe_adaptive_mode_reports_range_decisions() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gpu_codec_probe.py"),
            "--codec",
            "zlib",
            "--pattern",
            "repeat",
            "--size",
            str(1024 * 1024),
            "--range-bytes",
            str(256 * 1024),
            "--max-ranges",
            "4",
            "--network-gbps",
            "0.01",
            "--adaptive",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["adaptive"]["enabled_ranges"] == 4
    assert report["adaptive"]["skipped_ranges"] == 0
    assert report["roundtrip_ok"] is True


def test_gpu_codec_probe_batch_mode_reports_batch_decisions() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gpu_codec_probe.py"),
            "--codec",
            "zlib",
            "--pattern",
            "repeat",
            "--size",
            str(1024 * 1024),
            "--range-bytes",
            str(256 * 1024),
            "--max-ranges",
            "4",
            "--network-gbps",
            "0.01",
            "--adaptive",
            "--batch-ranges",
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
    assert report["adaptive"]["batch_ranges"] == 4
    assert report["adaptive"]["enabled_batches"] == 1
    assert report["adaptive"]["skipped_batches"] == 0
    assert report["roundtrip_ok"] is True
