from __future__ import annotations

import json
from pathlib import Path

import yaml

from aevrel.cli import main
from aevrel.doctor import doctor
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def _materialized_spec(tmp_path: Path) -> Path:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    spec["target"]["storage"]["uri"] = (tmp_path / "new-target").as_uri()
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def test_doctor_reports_ok_for_materialized_spec(tmp_path) -> None:
    result = doctor(str(_materialized_spec(tmp_path)))
    assert result["status"] == "ok"
    assert all(check["ok"] for check in result["checks"])
    assert not (tmp_path / "new-target").exists()


def test_doctor_reports_missing_source_without_writing(tmp_path) -> None:
    spec_path = _materialized_spec(tmp_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["source"]["shards"][0]["uri"] = (tmp_path / "missing.bin").as_uri()
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    result = doctor(str(spec_path))

    assert result["status"] == "failed"
    assert any(
        not check["ok"] and check["name"] == "source-shards"
        for check in result["checks"]
    )
    assert not (tmp_path / "new-target").exists()


def test_doctor_cli_returns_zero_for_ok_spec(tmp_path, capsys) -> None:
    spec_path = _materialized_spec(tmp_path)
    assert main(["doctor", "--spec", str(spec_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"


def test_doctor_checkpoint_missing_is_actionable(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.dcp"
    assert main(["doctor", "--checkpoint", str(missing), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    checkpoint = next(
        check for check in payload["checks"] if check["name"] == "checkpoint"
    )
    assert checkpoint["ok"] is False
    assert "hint" in checkpoint
