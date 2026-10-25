from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]


def _check_repo_module() -> ModuleType:
    """Load `scripts/check_repo.py` as a module so its gates can be unit tested."""
    spec = importlib.util.spec_from_file_location(
        "_aevrel_check_repo", ROOT / "scripts/check_repo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_native_binary_gate_accepts_the_real_build() -> None:
    """The gate is silent on a tree where `cargo build --release` has run."""
    module = _check_repo_module()
    errors: list[str] = []
    module._check_native_binary(errors)
    assert errors == []


def test_native_binary_gate_fires_when_the_crate_is_not_built(monkeypatch) -> None:
    """An unbuilt crate must fail the gate and name the build command."""
    module = _check_repo_module()
    monkeypatch.setattr(
        module, "NATIVE_RELEASE", Path("native/aevrel-fast/target/absent")
    )
    errors: list[str] = []
    module._check_native_binary(errors)
    assert len(errors) == 1
    assert "cargo build --release" in errors[0]


def test_native_binary_gate_requires_both_artefacts(monkeypatch, tmp_path) -> None:
    """Building only the binary leaves the Python kernels without a module."""
    module = _check_repo_module()
    release = tmp_path / "release"
    release.mkdir()
    (release / "aevrel-fast").write_bytes(b"")
    monkeypatch.setattr(module, "REPO", tmp_path)
    monkeypatch.setattr(module, "NATIVE_RELEASE", Path("release"))
    errors: list[str] = []
    module._check_native_binary(errors)
    assert len(errors) == 1
    assert "extension module" in errors[0]
