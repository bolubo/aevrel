from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from shardferry.cli import main
from shardferry.dcp import export_dcp
from shardferry.executor import apply
from shardferry.planning import plan
from shardferry.verification import write_target_ready_report
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
