"""End-to-end readiness gate: manifest closure, anchors and the L4 claim."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aevrel.cli import main
from aevrel.dcp import export_dcp
from aevrel.executor import apply
from aevrel.local.dcp import DCPStateProvider
from aevrel.planning import plan
from aevrel.state import build_manifest
from aevrel.verification import (
    ResumeStateEvidence,
    TargetReadyReport,
    write_target_ready_report,
)
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


DIGEST = "sha256:" + "0" * 64
REQUIRED = ("model", "optimizer", "global_step", "dataloader", "rng")


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


def _record(source: Path):
    return DCPStateProvider().inventory(str(source))


def _manifest(
    record,
    *,
    present: tuple[str, ...] = REQUIRED,
    metadata_digest: str | None = None,
):
    return build_manifest(
        checkpoint_uid=record.job_uid,
        version=record.version,
        metadata_digest=metadata_digest or record.metadata_digest,
        world_size=1,
        present={kind: "lrs.training" for kind in present},
        digests={kind: DIGEST for kind in present},
    )


def _write_manifest(tmp_path: Path, manifest, name: str = "resume-state.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    return path


def _write_reports(source: Path, tmp_path: Path, manifest) -> Path:
    record = _record(source)
    directory = tmp_path / "reports"
    directory.mkdir()
    evidence = ResumeStateEvidence.from_manifest(manifest)
    for rank in [item.rank for item in record.ranks]:
        write_target_ready_report(
            model_loaded=True,
            optimizer_loaded=True,
            steps_ran=2,
            migration_uid=record.job_uid,
            version=record.version,
            rank=rank,
            resume_state=evidence,
            path=directory / f"rank{rank}.json",
        )
    return directory


def test_closure_is_reported_without_claiming_l4(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    manifest = _manifest(_record(source))
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(_write_reports(source, tmp_path, manifest)),
            "--resume-state",
            str(_write_manifest(tmp_path, manifest)),
            "--json",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    readiness = payload["readiness"]
    assert readiness["status"] == "READY"
    assert readiness["state_complete"] is True
    assert readiness["readiness_level"] == "L3"
    assert payload["resume_state"]["status"] == "COMPLETE"


def test_anchored_closure_claims_l4(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    manifest = _manifest(_record(source))
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(_write_reports(source, tmp_path, manifest)),
            "--resume-state",
            str(_write_manifest(tmp_path, manifest)),
            "--require-state-complete",
            "--json",
        ]
    )
    assert result == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["status"] == "READY"
    assert readiness["loadable"] is True
    assert readiness["state_complete"] is True
    assert readiness["readiness_level"] == "L4"


def test_missing_dataloader_fails_the_l4_gate(tmp_path, capsys) -> None:
    """RFC #5054 shaped failure: loads, steps, but the cursor is missing."""
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    partial = _manifest(_record(source), present=("model", "optimizer", "global_step", "rng"))
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(_write_reports(source, tmp_path, partial)),
            "--resume-state",
            str(_write_manifest(tmp_path, partial)),
            "--require-state-complete",
            "--strict",
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    readiness = payload["readiness"]
    assert readiness["status"] == "TARGET_BLOCKED"
    assert readiness["loadable"] is False
    assert readiness["state_complete"] is False
    assert readiness["readiness_level"] == "L3"
    codes = [reason["code"] for reason in readiness["reasons"]]
    assert "STATE_CLOSURE_INCOMPLETE" in codes


def test_gate_requires_a_manifest(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--require-state-complete",
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "RESUME_MANIFEST_INVALID"


def test_manifest_for_another_checkpoint_is_rejected(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    record = _record(source)
    manifest = _manifest(record)
    foreign = _manifest(record, metadata_digest="sha256:" + "9" * 64)
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(_write_reports(source, tmp_path, manifest)),
            "--resume-state",
            str(_write_manifest(tmp_path, foreign)),
            "--require-state-complete",
            "--json",
        ]
    )
    assert result == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["status"] == "TARGET_BLOCKED"
    assert readiness["loadable"] is False
    codes = [reason["code"] for reason in readiness["reasons"]]
    assert "RESUME_MANIFEST_INVALID" in codes


def test_unanchored_drill_reports_are_rejected(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    record = _record(source)
    assessed = _manifest(record)
    restored = _manifest(record, present=("model", "optimizer"))
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(_write_reports(source, tmp_path, restored)),
            "--resume-state",
            str(_write_manifest(tmp_path, assessed)),
            "--require-state-complete",
            "--json",
        ]
    )
    assert result == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["status"] == "TARGET_BLOCKED"
    assert readiness["loadable"] is False
    assert readiness["readiness_level"] == "L3"
    codes = [reason["code"] for reason in readiness["reasons"]]
    assert "RESUME_COMPONENT_DIGEST_MISMATCH" in codes


def test_l4_is_refused_for_legacy_drill_reports(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    record = _record(source)
    manifest = _manifest(record)
    directory = tmp_path / "legacy"
    directory.mkdir()
    for rank in [item.rank for item in record.ranks]:
        TargetReadyReport(
            migration_uid=record.job_uid,
            version=record.version,
            rank=rank,
            role="worker",
            model_loaded=True,
            optimizer_loaded=True,
            steps_ran=2,
            state_complete=True,
            schema_version="1",
        ).write(directory / f"rank{rank}.json")
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(directory),
            "--resume-state",
            str(_write_manifest(tmp_path, manifest)),
            "--require-state-complete",
            "--json",
        ]
    )
    assert result == 0
    readiness = json.loads(capsys.readouterr().out)["readiness"]
    assert readiness["status"] == "READY"
    assert readiness["readiness_level"] == "L3"


def test_invalid_manifest_is_rejected(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"schemaVersion": "1"}), encoding="utf-8")
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--resume-state",
            str(broken),
            "--json",
        ]
    )
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "RESUME_MANIFEST_INVALID"


def test_human_output_states_the_claim_level(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    manifest = _manifest(_record(source))
    result = main(
        [
            "readiness",
            "--checkpoint",
            str(source),
            "--drill-report",
            str(_write_reports(source, tmp_path, manifest)),
            "--resume-state",
            str(_write_manifest(tmp_path, manifest)),
            "--require-state-complete",
        ]
    )
    assert result == 0
    out = capsys.readouterr().out
    assert "state_complete" in out
    assert "true" in out
    assert "readiness_level" in out
    assert "L4" in out
