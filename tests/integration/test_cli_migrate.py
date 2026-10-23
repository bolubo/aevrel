from __future__ import annotations

import json
from pathlib import Path

import pytest

from shardferry.cli import main
from shardferry.dcp import export_dcp
from shardferry.executor import apply
from shardferry.planning import plan
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def _source_dcp(
    tmp_path: Path,
    source_shape: tuple[int, ...] = (4,),
    source_placements: tuple[str, ...] = ("Shard(0)",),
) -> Path:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        source_shape,
        source_placements,
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    output = tmp_path / "source.dcp"
    export_dcp(spec, output)
    return output


def test_migrate_cli_4to3_json(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    output = tmp_path / "target.dcp"
    work = tmp_path / "work"

    result = main(
        [
            "migrate",
            "--checkpoint",
            str(source),
            "--target-world",
            "3",
            "--output",
            str(output),
            "--work-dir",
            str(work),
            "--json",
        ]
    )

    assert result == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["status"] == "success"
    assert payload["output"] == str(output.resolve())
    assert payload["topology"]["source_world_size"] == 4
    assert payload["topology"]["target_world_size"] == 3
    assert payload["verification"]["passed"] is True
    assert (output / ".metadata").is_file()
    assert len(list(output.glob("*.distcp"))) == 3
    assert (work / "plan.json").is_file()
    assert (work / "report.json").is_file()
    assert not (work / "source").exists()


def test_migrate_cli_2d_mesh_to_1d_target(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(
        tmp_path,
        source_shape=(2, 2),
        source_placements=("Shard(0)", "Shard(1)"),
    )
    output = tmp_path / "target.dcp"
    work = tmp_path / "work"

    result = main(
        [
            "migrate",
            "--checkpoint",
            str(source),
            "--source-mesh-shape",
            "2,2",
            "--source-placements",
            "Shard(0),Shard(1)",
            "--target-world",
            "3",
            "--target-mesh-shape",
            "3",
            "--target-placements",
            "Shard(0)",
            "--output",
            str(output),
            "--work-dir",
            str(work),
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["topology"]["source_mesh_shape"] == [2, 2]
    assert payload["topology"]["target_mesh_shape"] == [3]
    assert payload["verification"]["passed"] is True
    assert len(list(output.glob("*.distcp"))) == 3


def test_migrate_cli_prints_transfer_progress(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    output = tmp_path / "target.dcp"
    work = tmp_path / "work"

    result = main(
        [
            "migrate",
            "--checkpoint",
            str(source),
            "--target-world",
            "3",
            "--output",
            str(output),
            "--work-dir",
            str(work),
        ]
    )

    assert result == 0
    captured = capsys.readouterr()
    assert "transfer [" in captured.err
    assert "commit [" in captured.err
    assert "Migration complete" in captured.out


def test_doctor_cli_checks_real_dcp(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    source = _source_dcp(tmp_path)
    output = tmp_path / "target.dcp"

    result = main(
        [
            "doctor",
            "--checkpoint",
            str(source),
            "--output",
            str(output),
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert {check["name"] for check in payload["checks"]} >= {
        "python",
        "dcp-runtime",
        "checkpoint",
        "dcp-metadata",
        "dcp-storage",
        "output",
        "work-dir",
    }
    assert not output.exists()
