from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aevrel.state import ResumeStateManifest, build_manifest
from aevrel.verification.target_report import (
    ResumeStateEvidence,
    TargetReadyReport,
    TargetReportError,
    environment_rank,
    load_target_ready_report,
    write_target_ready_report,
)


ROOT = Path(__file__).resolve().parents[2]
ZERO_DIGEST = "sha256:" + "0" * 64
REQUIRED_KINDS = ("model", "optimizer", "global_step", "dataloader", "rng")


def _manifest(
    *,
    present: tuple[str, ...] = REQUIRED_KINDS,
    world_size: int = 1,
) -> ResumeStateManifest:
    return build_manifest(
        checkpoint_uid="default/job42",
        version="step-100",
        metadata_digest=ZERO_DIGEST,
        world_size=world_size,
        present={kind: "lrs.training" for kind in present},
        digests={kind: ZERO_DIGEST for kind in present},
    )


def _evidence(
    *,
    present: tuple[str, ...] = REQUIRED_KINDS,
) -> ResumeStateEvidence:
    return ResumeStateEvidence.from_manifest(_manifest(present=present))


def _schema(version: str) -> dict:
    path = (
        ROOT
        / "spec"
        / version
        / "target-ready.schema.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_write_and_load_target_ready_report(tmp_path: Path) -> None:
    path = tmp_path / "rank-3.json"
    payload = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100000",
        rank=3,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=10,
        resume_state=_evidence(),
        path=path,
    )
    assert payload["schemaVersion"] == "2"
    assert payload["rank"] == 3
    assert payload["stateComplete"] is True
    assert payload["resumeState"]["closure"] == "COMPLETE"
    assert payload["resumeState"]["manifestDigest"].startswith("sha256:")
    assert load_target_ready_report(path) == payload


def test_report_binds_manifest_digest(tmp_path: Path) -> None:
    manifest = _manifest()
    payload = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=1,
        resume_state=ResumeStateEvidence.from_manifest(manifest),
        path=tmp_path / "rank.json",
    )
    assert payload["resumeState"]["manifestDigest"] == manifest.compute_digest()


def test_partial_manifest_keeps_report_incomplete(tmp_path: Path) -> None:
    evidence = _evidence(present=("model",))
    payload = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=1,
        resume_state=evidence,
        path=tmp_path / "rank.json",
    )
    assert payload["stateComplete"] is False
    assert payload["resumeState"]["closure"] == "PARTIAL"
    assert evidence.missing_required() == (
        "dataloader",
        "global_step",
        "optimizer",
        "rng",
    )


def test_report_is_fail_closed() -> None:
    base = {
        "migration_uid": "default/job42",
        "version": "step-1",
        "rank": 0,
        "role": "worker",
        "model_loaded": True,
        "optimizer_loaded": True,
        "steps_ran": 1,
    }
    with pytest.raises(TargetReportError, match="migrationUID"):
        TargetReadyReport(
            **{**base, "migration_uid": ""},
            resume_state=_evidence(),
        )
    with pytest.raises(TargetReportError, match="ok report"):
        TargetReadyReport(
            **base,
            resume_state=_evidence(),
            ok=True,
            error="contradiction",
        )
    with pytest.raises(TargetReportError, match="requires resumeState"):
        TargetReadyReport(**base)
    with pytest.raises(TargetReportError, match="must not carry resumeState"):
        TargetReadyReport(**base, schema_version="1", resume_state=_evidence())
    with pytest.raises(TargetReportError, match="disagrees with resumeState"):
        TargetReadyReport(
            **base,
            state_complete=True,
            resume_state=_evidence(present=("model",)),
        )


def test_evidence_is_fail_closed() -> None:
    with pytest.raises(TargetReportError, match="sha256"):
        ResumeStateEvidence(
            manifest_digest="deadbeef",
            closure="COMPLETE",
            components=(),
        )
    with pytest.raises(TargetReportError, match="unsupported resumeState closure"):
        ResumeStateEvidence(
            manifest_digest=ZERO_DIGEST,
            closure="UNKNOWN",
            components=(),
        )


def test_report_matches_v2_schema(tmp_path: Path) -> None:
    path = tmp_path / "rank.json"
    payload = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=10,
        resume_state=_evidence(),
        path=path,
    )
    schema = _schema("v2")
    Draft202012Validator(schema).validate(payload)

    payload["unexpected"] = True
    with pytest.raises(Exception):
        Draft202012Validator(schema).validate(payload)


def test_v2_schema_requires_resume_state() -> None:
    schema = _schema("v2")
    payload = {
        "schemaVersion": "2",
        "migrationUID": "default/job42",
        "version": "step-100",
        "rank": 0,
        "role": "worker",
        "modelLoaded": True,
        "optimizerLoaded": True,
        "stepsRan": 10,
        "stateComplete": False,
        "ok": True,
        "error": "",
    }
    with pytest.raises(Exception):
        Draft202012Validator(schema).validate(payload)
    payload["resumeState"] = {
        "manifestDigest": ZERO_DIGEST,
        "closure": "MISSING",
        "components": [],
    }
    Draft202012Validator(schema).validate(payload)


def test_legacy_v1_report_still_loads(tmp_path: Path) -> None:
    report = TargetReadyReport(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        role="worker",
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=10,
        state_complete=True,
        schema_version="1",
    )
    path = tmp_path / "rank.json"
    report.write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator(_schema("v1")).validate(payload)
    assert "resumeState" not in payload
    assert load_target_ready_report(path) == payload


def test_failure_aggregate_matches_v2_schema(tmp_path: Path) -> None:
    schema = _schema("v2")
    rank_report = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=0,
        resume_state=_evidence(present=("model",)),
        ok=False,
        error="state closure incomplete",
        path=tmp_path / "rank0.json",
    )
    payload = {
        "schemaVersion": "2",
        "migrationUID": "default/job42",
        "version": "step-100",
        "ready": False,
        "condition": "StateClosureIncomplete",
        "message": "rank 0 resume state closure is incomplete",
        "reasonCodes": ["RESUME_COMPONENT_MISSING"],
        "ranksReady": 0,
        "ranksTotal": 1,
        "stepsRan": 0,
        "stateComplete": False,
        "completedAt": "2026-10-22T00:00:00Z",
        "reports": [rank_report],
    }
    Draft202012Validator(schema).validate(payload)
    payload["reports"] = None
    with pytest.raises(Exception):
        Draft202012Validator(schema).validate(payload)


def test_report_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "rank.json"
    path.write_text(
        json.dumps(
            {
                **TargetReadyReport(
                    migration_uid="default/job42",
                    version="step-100",
                    rank=0,
                    role="worker",
                    model_loaded=True,
                    optimizer_loaded=True,
                    steps_ran=10,
                    resume_state=_evidence(),
                ).to_dict(),
                "extra": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TargetReportError, match="unknown fields"):
        load_target_ready_report(path)


def test_report_rejects_missing_resume_state(tmp_path: Path) -> None:
    path = tmp_path / "rank.json"
    payload = TargetReadyReport(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        role="worker",
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=10,
        resume_state=_evidence(),
    ).to_dict()
    payload.pop("resumeState")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TargetReportError, match="missing fields: resumeState"):
        load_target_ready_report(path)


def test_environment_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "7")
    assert environment_rank() == 7
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "not-a-number")
    with pytest.raises(TargetReportError, match="JOB_COMPLETION_INDEX"):
        environment_rank()


def _cli_env(tmp_path: Path) -> dict[str, str]:
    manifest_path = tmp_path / "resume-state.json"
    manifest_path.write_text(
        json.dumps(_manifest().to_dict()),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "AEVREL_REPORT_PATH": str(tmp_path / "termination.json"),
            "AEVREL_MIGRATION_UID": "default/job42",
            "AEVREL_VERSION": "step-100",
            "JOB_COMPLETION_INDEX": "2",
            "AEVREL_TEST_MANIFEST": str(manifest_path),
        }
    )
    return env


def test_target_report_cli(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aevrel.commands.target_report",
            "--model-loaded",
            "--optimizer-loaded",
            "--steps-ran",
            "10",
            "--resume-state",
            env["AEVREL_TEST_MANIFEST"],
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    payload = load_target_ready_report(Path(env["AEVREL_REPORT_PATH"]))
    assert payload["rank"] == 2
    assert payload["modelLoaded"] is True
    assert payload["resumeState"]["closure"] == "COMPLETE"
    assert payload["resumeState"]["manifestDigest"] == _manifest().compute_digest()


def test_target_report_cli_requires_resume_state(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aevrel.commands.target_report",
            "--steps-ran",
            "10",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode != 0
    assert "--resume-state" in completed.stderr


def test_target_report_cli_rejects_invalid_manifest(tmp_path: Path) -> None:
    env = _cli_env(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"schemaVersion": "1"}), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "aevrel.commands.target_report",
            "--steps-ran",
            "10",
            "--resume-state",
            str(broken),
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 2
    assert "resume-state manifest validation failed" in completed.stderr
