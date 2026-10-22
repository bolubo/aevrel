from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shardferry.cdp import VersionMarker, write_marker
from shardferry.cutover_service import CutoverService
from shardferry.target_ready import CommandTargetReady, TargetReadyError


def make_version(root: Path, version_id: str, data: bytes) -> None:
    version = root / version_id
    version.mkdir(parents=True)
    (version / "w.bin").write_bytes(data)
    write_marker(
        version,
        VersionMarker(
            version="1",
            version_id=version_id,
            job_uid="job-1",
            step=100,
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )


def probe_command(target: Path, version_id: str) -> CommandTargetReady:
    return CommandTargetReady(
        [
            sys.executable,
            "-m",
            "shardferry.target_probe",
            "--target-root",
            str(target),
            "--version-id",
            version_id,
            "--timeout",
            "2",
        ]
    )


def test_cutover_runs_real_target_probe(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"checkpoint-data")
    target = tmp_path / "target"
    service = CutoverService(
        source,
        target,
        target_ready=probe_command(target, "{version_id}"),
    )
    service.follow_once()

    result = service.cutover("step-100")

    assert result["state"] == "promoted"
    assert result["target_ready_seconds"] >= 0
    status = service.status()
    assert status["last_cutover"]["version_id"] == "step-100"
    assert status["last_cutover"]["target_ready_seconds"] >= 0


def test_target_probe_failure_rolls_back_and_resumes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"checkpoint-data")
    target = tmp_path / "target"
    service = CutoverService(
        source,
        target,
        target_ready=probe_command(target, "step-missing"),
    )
    service.follow_once()

    with pytest.raises(TargetReadyError, match="did not become ready"):
        service.cutover("step-100")

    assert service.target.current() is None
