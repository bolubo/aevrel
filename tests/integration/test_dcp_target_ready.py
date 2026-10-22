from __future__ import annotations

from pathlib import Path

import pytest

from shardferry.dcp import export_dcp
from shardferry.executor import apply
from shardferry.planner import plan
from shardferry.target_ready import DcpTargetReady, TargetReadyError
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


def test_dcp_target_ready_fails_closed_on_missing_checkpoint(
    tmp_path: Path,
) -> None:
    pytest.importorskip("torch")
    with pytest.raises(TargetReadyError, match="DCP load failed"):
        DcpTargetReady(tmp_path / "missing.dcp")("step-100")
