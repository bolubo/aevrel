from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aevrel.cli import main
from aevrel.dcp import export_dcp
from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.state import build_manifest
from aevrel.verification import ResumeStateEvidence, write_target_ready_report
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def _source_dcp(tmp_path: Path) -> Path:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    output = tmp_path / "source.dcp"
    export_dcp(spec, output)
    return output

_DIGEST = "sha256:" + "0" * 64


def _evidence(uid: str) -> ResumeStateEvidence:
    """Complete resume-state evidence for a report written by a test."""
    return ResumeStateEvidence.from_manifest(
        build_manifest(
            checkpoint_uid=uid,
            version=uid,
            metadata_digest=_DIGEST,
            world_size=1,
            present={
                "model": "lrs.training",
                "optimizer": "lrs.training",
                "global_step": "lrs.metadata",
                "dataloader": "lrs.training",
                "rng": "lrs.rng",
            },
            digests={
                "model": _DIGEST,
                "optimizer": _DIGEST,
                "global_step": _DIGEST,
                "dataloader": _DIGEST,
                "rng": _DIGEST,
            },
        )
    )


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_readiness_cli_reports_complete_checkpoint(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--target-world",
            "3",
            "--json",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["complete"] is True
    assert len(payload["record"]["ranks"]) == 4
    assert payload["readiness"]["status"] == "UNVERIFIED"
    assert payload["readiness"]["loadable"] is None
    assert _tree_digest(source) == before


def test_readiness_cli_reports_missing_shard(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    next(source.glob("*.distcp")).unlink()
    result = main(["readiness", "--checkpoint", str(source), "--json"])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record"]["complete"] is False
    assert payload["readiness"]["status"] == "INCOMPLETE"
    assert payload["readiness"]["reasons"]


def test_readiness_cli_uses_complete_drill_reports(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    for rank in range(4):
        write_target_ready_report(
            model_loaded=True,
            optimizer_loaded=True,
            steps_ran=2,
            migration_uid=source.name,
            version=source.name,
            rank=rank,
            resume_state=_evidence(source.name),
            path=reports / f"rank{rank}.json",
        )

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--target-world",
            "3",
            "--drill-report",
            str(reports),
            "--json",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["readiness"]["status"] == "READY"
    assert payload["readiness"]["loadable"] is True
    assert payload["drill"]["phase"] == "Succeeded"


def test_readiness_cli_fails_closed_on_partial_drill_reports(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    report = tmp_path / "rank0.json"
    write_target_ready_report(
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=2,
        migration_uid=source.name,
        version=source.name,
        rank=0,
        resume_state=_evidence(source.name),
        path=report,
    )

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(report),
            "--json",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["readiness"]["status"] == "TARGET_BLOCKED"
    assert payload["readiness"]["loadable"] is False
    assert "missing drill reports" in payload["drill"]["detail"]


def test_readiness_cli_rejects_drill_report_version_mismatch(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    report = tmp_path / "rank0.json"
    write_target_ready_report(
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=2,
        migration_uid=source.name,
        version="mismatched-version",
        rank=0,
        resume_state=_evidence(source.name),
        path=report,
    )

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(report),
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "DRILL_REPORT_INVALID"
    assert (
        "report version 'mismatched-version' does not match checkpoint"
        in payload["error"]["message"]
    )
    assert "readiness" not in payload
    assert _tree_digest(source) == before


def test_readiness_cli_rejects_drill_report_migration_uid_mismatch(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    report = tmp_path / "rank0.json"
    write_target_ready_report(
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=2,
        migration_uid="mismatched-uid",
        version=source.name,
        rank=0,
        resume_state=_evidence(source.name),
        path=report,
    )

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(report),
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "DRILL_REPORT_INVALID"
    assert (
        "report migrationUID 'mismatched-uid' does not match checkpoint"
        in payload["error"]["message"]
    )
    assert "readiness" not in payload
    assert _tree_digest(source) == before


def test_readiness_cli_rejects_duplicate_rank_drill_reports(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    reports = tmp_path / "reports"
    reports.mkdir()
    write_target_ready_report(
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=2,
        migration_uid=source.name,
        version=source.name,
        rank=0,
        resume_state=_evidence(source.name),
        path=reports / "rank0_a.json",
    )
    write_target_ready_report(
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=2,
        migration_uid=source.name,
        version=source.name,
        rank=0,
        resume_state=_evidence(source.name),
        path=reports / "rank0_b.json",
    )

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(reports),
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "DRILL_REPORT_INVALID"
    assert payload["error"]["message"] == "duplicate drill report for rank 0"
    assert "readiness" not in payload
    assert _tree_digest(source) == before


def test_readiness_cli_rejects_malformed_json_drill_report(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    report = tmp_path / "malformed.json"
    report.write_text("{malformed json content: ", encoding="utf-8")

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(report),
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "DRILL_REPORT_INVALID"
    assert f"could not read drill report {report}" in payload["error"]["message"]
    assert "readiness" not in payload
    assert _tree_digest(source) == before


def test_readiness_cli_rejects_nonexistent_drill_report_path(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    nonexistent = tmp_path / "nonexistent.json"

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(nonexistent),
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "DRILL_REPORT_INVALID"
    assert f"drill report path not found: {nonexistent}" in payload["error"]["message"]
    assert "readiness" not in payload
    assert _tree_digest(source) == before


def test_readiness_cli_rejects_drill_report_plain_text_error(
    tmp_path,
    capsys,
) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    before = _tree_digest(source)
    nonexistent = tmp_path / "nonexistent.json"

    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(nonexistent),
        ]
    )
    assert result == 2
    captured = capsys.readouterr()
    assert (
        f"readiness: DRILL_REPORT_INVALID: drill report path not found: {nonexistent}"
        in captured.err
    )
    assert "READY" not in captured.out
    assert _tree_digest(source) == before
