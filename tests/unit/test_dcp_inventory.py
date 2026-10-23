from __future__ import annotations

import pytest

from shardferry.dcp import export_dcp, inventory_dcp
from shardferry.executor import apply
from shardferry.planning import plan
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def test_dcp_inventory_reads_local_rank_without_raw_workdir(tmp_path) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    dcp_dir = tmp_path / "source.dcp"
    export_dcp(spec, dcp_dir)

    inventory, shards = inventory_dcp(
        dcp_dir,
        rank=0,
        file_prefix="http://source-agent/v1/shards",
    )

    assert inventory["rank"] == 0
    assert inventory["format"] == "dcp"
    assert inventory["tensors"][0]["name"] == "w"
    assert inventory["chunks"]
    assert shards
    first = inventory["chunks"][0]
    assert first["file"].startswith("http://source-agent/v1/shards/")
    assert first["checksum"].startswith("sha256:")
    assert any(first["file"].endswith(key) for key in shards)
