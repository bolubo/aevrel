from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from aevrel.manifest import load_spec

EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "toy-4to3" / "checkpoint.yaml"
)


def test_inspect_requires_input_process_exit_code() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aevrel.cli", "inspect"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "required" in result.stderr


def test_version_process_exit_code() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aevrel.cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "0.3.0" in result.stdout


def test_module_entrypoint_and_product_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aevrel", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "aevrel demo" in result.stdout
    assert "readiness" in result.stdout
    assert "state" in result.stdout
    assert "dcp-migrate" not in result.stdout


def test_inspect_process_writes_valid_spec(tmp_path) -> None:
    output = tmp_path / "migration.yaml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aevrel.cli",
            "inspect",
            "--input",
            str(EXAMPLE),
            "--out",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert load_spec(output)["checkpoint"]["format"] == "toy"
