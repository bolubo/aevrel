from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from aevrel.dcp import (
    export_dcp,
    export_dcp_local,
    inspect_dcp,
    local_dcp_ranks,
)
from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.storage.file import FileStorage
from aevrel.verification import DcpTargetReady
from aevrel.verification import verify
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from tests.oracle.reference import assert_bitwise_equal, reference_reshard


def test_dcp_roundtrip_4to3(tmp_path) -> None:
    pytest.importorskip("torch")
    spec, source_arrays, tensor = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, source_arrays, tmp_path)
    source_plan = plan(spec)
    apply(source_plan, spec)
    dcp_dir = tmp_path / "source.dcp"
    export_dcp(spec, dcp_dir)
    assert local_dcp_ranks(dcp_dir) == [0, 1, 2, 3]
    subset = tmp_path / "node-rank1"
    subset.mkdir()
    shutil.copy(dcp_dir / "__1_0.distcp", subset / "__1_0.distcp")
    assert local_dcp_ranks(subset, metadata_dir=dcp_dir) == [1]

    work_dir = tmp_path / "dcp-work"
    migrated_spec = inspect_dcp(
        dcp_dir,
        work_dir,
        target_world_size=3,
    )
    migrated_plan = plan(migrated_spec)
    apply(migrated_plan, migrated_spec)
    verify(migrated_spec, migrated_plan)

    expected = reference_reshard(tensor, (3,), ("Shard(0)",))
    for rank, (_, expected_array) in expected.items():
        raw = (
            FileStorage()
            .path(migrated_spec["target"]["storage"]["uri"])
            / "w"
            / f"rank{rank}.bin"
        )
        actual = np.frombuffer(raw.read_bytes(), dtype=np.float32).reshape(
            expected_array.shape
        )
        assert_bitwise_equal(actual, expected_array)

    output_dcp = tmp_path / "migrated.dcp"
    export_dcp(migrated_spec, output_dcp)
    assert (output_dcp / ".metadata").is_file()
    assert len(list(output_dcp.glob("*.distcp"))) == 3
    rank_dcp = tmp_path / "target-rank0.dcp"
    migrated_raw = (
        FileStorage().path(migrated_spec["target"]["storage"]["uri"])
    )
    export_dcp_local(
        migrated_spec,
        rank_dcp,
        raw_root=migrated_raw,
        ranks=(0,),
    )
    assert (rank_dcp / ".metadata").is_file()
    assert len(list(rank_dcp.glob("*.distcp"))) == 1
    DcpTargetReady(rank_dcp, rank=0)("local-rank")
