from __future__ import annotations

from pathlib import Path

import pytest

from shardferry.cdp import VersionMarker, write_marker
from shardferry.cutover_service import CutoverService
from shardferry.dcp import export_dcp, export_dcp_local
from shardferry.executor import apply
from shardferry.planner import plan
from shardferry.target_ready import DcpTargetReady, TargetReadyError
from shardferry.target_probe import main as target_probe_main
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def test_dcp_target_ready_loads_target_checkpoint(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path / "work")
    apply(plan(spec), spec)
    checkpoint = tmp_path / "source.dcp"
    export_dcp(spec, checkpoint)

    DcpTargetReady(checkpoint)("step-100")


def test_target_probe_dcp_cli_mode(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path / "work")
    apply(plan(spec), spec)
    checkpoint = tmp_path / "source.dcp"
    export_dcp(spec, checkpoint)

    assert target_probe_main(
        [
            "--dcp-checkpoint",
            str(checkpoint),
            "--version-id",
            "step-100",
            "--json",
        ]
    ) == 0
    assert target_probe_main(
        [
            "--dcp-checkpoint",
            str(checkpoint),
            "--version-id",
            "step-100",
            "--dcp-rank",
            "0",
        ]
    ) == 0


def test_local_rank_dcp_target_ready(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    local = tmp_path / "rank0.dcp"
    export_dcp_local(
        spec,
        local,
        raw_root=tmp_path / "target",
        ranks=(0,),
    )

    DcpTargetReady(local, rank=0)("step-100")


def test_rank_local_load_does_not_allocate_global_tensor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    torch = pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    local = tmp_path / "rank0.dcp"
    export_dcp_local(
        spec,
        local,
        raw_root=tmp_path / "target",
        ranks=(0,),
    )

    original_empty = torch.empty
    shapes = []

    def record_empty(*args, **kwargs):
        if args:
            shapes.append(tuple(args[0]))
        return original_empty(*args, **kwargs)

    monkeypatch.setattr(torch, "empty", record_empty)
    DcpTargetReady(local, rank=0)("step-100")
    assert (8, 4) not in shapes


def test_dcp_target_ready_fails_closed_on_missing_checkpoint(
    tmp_path: Path,
) -> None:
    pytest.importorskip("torch")
    with pytest.raises(TargetReadyError, match="DCP load failed"):
        DcpTargetReady(tmp_path / "missing.dcp")("step-100")


def test_cutover_includes_real_dcp_load_in_rto(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path / "work")
    apply(plan(spec), spec)
    checkpoint = tmp_path / "target.dcp"
    export_dcp(spec, checkpoint)

    source = tmp_path / "source"
    version = source / "step-100"
    version.mkdir(parents=True)
    (version / "w.bin").write_bytes(b"rto-data")
    write_marker(
        version,
        VersionMarker(
            version="1",
            version_id="step-100",
            job_uid="job-1",
            step=100,
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )
    raw_target = tmp_path / "raw-target"
    service = CutoverService(
        source,
        raw_target,
        target_ready=DcpTargetReady(checkpoint),
    )
    service.follow_once()
    result = service.cutover("step-100")

    assert result["state"] == "promoted"
    assert result["target_ready_seconds"] > 0
