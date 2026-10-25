from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from aevrel import __version__
from aevrel.cli import main
from aevrel.manifest import load_spec
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)

EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "toy-4to3" / "checkpoint.yaml"
)


def test_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help() -> None:
    assert main([]) == 2


def test_inspect_requires_input() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["inspect"])
    assert exc.value.code == 2


def test_inspect_writes_valid_spec(tmp_path) -> None:
    output = tmp_path / "migration.yaml"
    assert main(["inspect", "--input", str(EXAMPLE), "--out", str(output)]) == 0
    spec = load_spec(output)
    assert spec["checkpoint"]["format"] == "toy"
    assert spec["target"]["mesh"]["world_size"] == 3


def test_inspect_reports_missing_input_file(capsys) -> None:
    assert main(["inspect", "--input", "missing-checkpoint.yaml"]) == 2
    assert "file not found" in capsys.readouterr().err


def test_plan_apply_verify_cli(tmp_path, capsys) -> None:
    spec, source_arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    plan_path = tmp_path / "plan.json"
    report_path = tmp_path / "report.json"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    assert main(["plan", "--spec", str(spec_path), "--out", str(plan_path)]) == 0
    assert plan_path.is_file()
    assert main(
        [
            "apply",
            "--spec",
            str(spec_path),
            "--plan",
            str(plan_path),
            "--report",
            str(report_path),
        ]
    ) == 0
    assert report_path.is_file()
    assert main(
        ["verify", "--spec", str(spec_path), "--plan", str(plan_path)]
    ) == 0
    assert "bitwise" in capsys.readouterr().out


def test_default_demo_cli(tmp_path, capsys) -> None:
    result = main(["demo", "--work-dir", str(tmp_path / "demo")])
    assert result == 0
    output = capsys.readouterr().out
    assert "Demo complete" in output
    assert "verification" in output and "bitwise passed" in output
    assert (tmp_path / "demo" / "target" / "w" / "rank0.bin").is_file()


def test_demo_json_cli(tmp_path, capsys) -> None:
    result = main(
        ["demo", "--work-dir", str(tmp_path / "demo"), "--json"]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"]["passed"] is True


def test_demo_dcp_cli(tmp_path, capsys) -> None:
    pytest.importorskip("torch")
    result = main(
        [
            "demo",
            "--dcp",
            "--work-dir",
            str(tmp_path / "demo"),
            "--json",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["dcp"]["source"], ".metadata").is_file()
    assert Path(payload["dcp"]["target"], ".metadata").is_file()
    assert payload["dcp"]["target_load"] == "ok"


def test_help_only_exposes_product_commands(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    for command in (
        "demo",
        "migrate",
        "doctor",
        "capabilities",
        "readiness",
        "restore-drill",
        "state",
        "tui",
    ):
        assert command in output
    for command in ("inspect", "dcp-migrate", "serve", "apply"):
        assert command not in output
    assert not re.search(r"^\s{2,}verify\b", output, re.MULTILINE)


def test_unknown_command_does_not_expose_internal_commands(capsys) -> None:
    assert main(["unknown"]) == 2
    error = capsys.readouterr().err
    assert "Available commands:" in error
    for command in ("inspect", "dcp-migrate", "serve", "apply"):
        assert command not in error


def test_migrate_rejects_output_inside_source(tmp_path, capsys) -> None:
    checkpoint = tmp_path / "source.dcp"
    checkpoint.mkdir()
    result = main(
        [
            "migrate",
            "--checkpoint",
            str(checkpoint),
            "--output",
            str(checkpoint / "migrated"),
        ]
    )
    assert result == 2
    assert "must not overlap" in capsys.readouterr().err


def test_summarize_cli_requires_complete_rank_set(tmp_path, capsys) -> None:
    spec, source_arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    plan_path = tmp_path / "plan.json"
    reports = tmp_path / "reports"
    reports.mkdir()
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    assert main(["plan", "--spec", str(spec_path), "--out", str(plan_path)]) == 0
    for rank in range(3):
        assert main(
            [
                "apply",
                "--spec",
                str(spec_path),
                "--plan",
                str(plan_path),
                "--target-rank",
                str(rank),
                "--target-root",
                str(tmp_path / f"target-rank{rank}"),
                "--report",
                str(reports / f"rank{rank}.json"),
            ]
        ) == 0

    summary_path = tmp_path / "summary.json"
    assert main(
        [
            "summarize",
            "--reports-dir",
            str(reports),
            "--plan",
            str(plan_path),
            "--spec",
            str(spec_path),
            "--out",
            str(summary_path),
        ]
    ) == 0
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["target_ranks"] == [0, 1, 2]

    (reports / "rank2.json").unlink()
    assert main(
        [
            "summarize",
            "--reports-dir",
            str(reports),
            "--plan",
            str(plan_path),
            "--spec",
            str(spec_path),
            "--out",
            str(summary_path),
        ]
    ) == 2
    assert "missing target ranks: 2" in capsys.readouterr().err


def test_proof_rank_build_and_merge_cli(tmp_path) -> None:
    spec, source_arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    plan_path = tmp_path / "plan.json"
    target_root = tmp_path / "target"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    assert main(["plan", "--spec", str(spec_path), "--out", str(plan_path)]) == 0
    assert main(
        [
            "apply",
            "--spec",
            str(spec_path),
            "--plan",
            str(plan_path),
            "--target-root",
            str(target_root),
        ]
    ) == 0

    proofs = []
    for rank in range(3):
        proof_path = tmp_path / f"rank{rank}.proof.json"
        assert main(
            [
                "proof-build",
                "--spec",
                str(spec_path),
                "--plan",
                str(plan_path),
                "--target-root",
                str(target_root),
                "--target-rank",
                str(rank),
                "--out",
                str(proof_path),
            ]
        ) == 0
        proofs.append(proof_path)
    merged = tmp_path / "merged.proof.json"
    assert main(
        [
            "proof-merge",
            "--proof",
            str(proofs[0]),
            "--proof",
            str(proofs[1]),
            "--proof",
            str(proofs[2]),
            "--out",
            str(merged),
        ]
    ) == 0
    payload = json.loads(merged.read_text(encoding="utf-8"))
    assert payload["target_files"]


def test_proof_rank_build_and_merge_cli(tmp_path) -> None:
    spec, source_arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    spec_path = tmp_path / "spec.yaml"
    plan_path = tmp_path / "plan.json"
    target_root = tmp_path / "target"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    assert main(["plan", "--spec", str(spec_path), "--out", str(plan_path)]) == 0
    assert main(
        [
            "apply",
            "--spec",
            str(spec_path),
            "--plan",
            str(plan_path),
            "--target-root",
            str(target_root),
        ]
    ) == 0

    proofs = []
    for rank in range(3):
        proof_path = tmp_path / f"rank{rank}.proof.json"
        assert main(
            [
                "proof-build",
                "--spec",
                str(spec_path),
                "--plan",
                str(plan_path),
                "--target-root",
                str(target_root),
                "--target-rank",
                str(rank),
                "--out",
                str(proof_path),
            ]
        ) == 0
        proofs.append(proof_path)
    merged = tmp_path / "merged.proof.json"
    assert main(
        [
            "proof-merge",
            "--proof",
            str(proofs[0]),
            "--proof",
            str(proofs[1]),
            "--proof",
            str(proofs[2]),
            "--out",
            str(merged),
        ]
    ) == 0
    payload = json.loads(merged.read_text(encoding="utf-8"))
    assert payload["target_files"]


def test_summarize_cli_requires_plan_and_spec_together(tmp_path, capsys) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    assert main(
        [
            "summarize",
            "--reports-dir",
            str(reports),
            "--plan",
            str(tmp_path / "plan.json"),
            "--out",
            str(tmp_path / "summary.json"),
        ]
    ) == 2
    assert "--plan and --spec" in capsys.readouterr().err


def test_capabilities_cli_is_machine_readable(capsys) -> None:
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apiVersion"] == "aevrel.dev/v1"
    assert "dcp" in payload["features"]
    assert "checkpoint-readiness" in payload["features"]
    assert "restore-drill" in payload["features"]
    assert "state-bundle" in payload["features"]
    assert "target-ready" in payload["features"]
    assert "local-tui" in payload["features"]
