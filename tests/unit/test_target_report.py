from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from shardferry.verification.target_report import (
    TargetReadyReport,
    TargetReportError,
    environment_rank,
    load_target_ready_report,
    write_target_ready_report,
)


ROOT = Path(__file__).resolve().parents[2]


def test_write_and_load_target_ready_report(tmp_path: Path) -> None:
    path = tmp_path / "rank-3.json"
    payload = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100000",
        rank=3,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=10,
        state_complete=False,
        path=path,
    )
    assert payload["rank"] == 3
    assert load_target_ready_report(path) == payload


def test_report_is_fail_closed() -> None:
    with pytest.raises(TargetReportError, match="migrationUID"):
        TargetReadyReport(
            migration_uid="",
            version="step-1",
            rank=0,
            role="worker",
            model_loaded=True,
            optimizer_loaded=True,
            steps_ran=1,
        )
    with pytest.raises(TargetReportError, match="ok report"):
        TargetReadyReport(
            migration_uid="default/job42",
            version="step-1",
            rank=0,
            role="worker",
            model_loaded=True,
            optimizer_loaded=True,
            steps_ran=1,
            ok=True,
            error="contradiction",
        )


def test_report_matches_schema(tmp_path: Path) -> None:
    path = tmp_path / "rank.json"
    payload = write_target_ready_report(
        migration_uid="default/job42",
        version="step-100",
        rank=0,
        model_loaded=True,
        optimizer_loaded=True,
        steps_ran=10,
        path=path,
    )
    schema = json.loads(
        (ROOT / "spec" / "v1" / "target-ready.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(payload)

    payload["unexpected"] = True
    with pytest.raises(Exception):
        Draft202012Validator(schema).validate(payload)


def test_failure_aggregate_matches_schema() -> None:
    schema = json.loads(
        (ROOT / "spec" / "v1" / "target-ready.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = {
        "schemaVersion": "1",
        "migrationUID": "default/job42",
        "version": "step-100",
        "ready": False,
        "condition": "JobFailed",
        "message": "target training failed",
        "ranksReady": 0,
        "ranksTotal": 2,
        "stepsRan": 0,
        "stateComplete": False,
        "completedAt": "2026-10-22T00:00:00Z",
        "reports": [],
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
                ).to_dict(),
                "extra": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TargetReportError, match="unknown fields"):
        load_target_ready_report(path)


def test_environment_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "7")
    assert environment_rank() == 7
    monkeypatch.setenv("JOB_COMPLETION_INDEX", "not-a-number")
    with pytest.raises(TargetReportError, match="JOB_COMPLETION_INDEX"):
        environment_rank()


def test_target_report_cli(tmp_path: Path) -> None:
    path = tmp_path / "termination.json"
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "SHARDFERRY_REPORT_PATH": str(path),
            "SHARDFERRY_MIGRATION_UID": "default/job42",
            "SHARDFERRY_VERSION": "step-100",
            "JOB_COMPLETION_INDEX": "2",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "shardferry.commands.target_report",
            "--model-loaded",
            "--optimizer-loaded",
            "--steps-ran",
            "10",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    payload = load_target_ready_report(path)
    assert payload["rank"] == 2
    assert payload["modelLoaded"] is True
