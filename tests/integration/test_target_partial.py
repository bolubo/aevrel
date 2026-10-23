from __future__ import annotations

import numpy as np
import pytest

from aevrel.dcp import export_dcp
from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.targets import target_file_path
from aevrel.verification import verify
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from tests.oracle.reference import assert_bitwise_equal


def _run(tmp_path, target_placements: tuple[str, ...]):
    spec, arrays, tensor = generate_sharded_spec(
        (4, 2),
        (4,),
        ("Shard(0)",),
        (2,),
        target_placements,
    )
    materialize_source_files(spec, arrays, tmp_path)
    result = plan(spec)
    apply(result, spec)
    verify(spec, result)
    return spec, tensor, result


def test_target_partial_sum_uses_owner_and_zero_peers(tmp_path) -> None:
    spec, tensor, result = _run(tmp_path, ("Partial(sum)",))
    assert any(node["op"] == "zeros" for node in result["nodes"])
    owner = np.frombuffer(
        target_file_path(spec, "w", 0).read_bytes(), dtype=np.float32
    ).reshape(tensor.shape)
    peer = np.frombuffer(
        target_file_path(spec, "w", 1).read_bytes(), dtype=np.float32
    ).reshape(tensor.shape)
    assert_bitwise_equal(owner, tensor)
    assert not np.any(peer)


def test_target_partial_avg_replicates_the_global_shard(tmp_path) -> None:
    spec, tensor, result = _run(tmp_path, ("Partial(avg)",))
    assert all(node["op"] != "zeros" for node in result["nodes"])
    for rank in range(2):
        actual = np.frombuffer(
            target_file_path(spec, "w", rank).read_bytes(), dtype=np.float32
        ).reshape(tensor.shape)
        assert_bitwise_equal(actual, tensor)


def test_target_partial_can_be_exported_as_dcp(tmp_path) -> None:
    pytest.importorskip("torch")
    spec, _, _ = _run(tmp_path, ("Partial(sum)",))
    export_dcp(spec, tmp_path / "partial.dcp")
    assert len(list((tmp_path / "partial.dcp").glob("*.distcp"))) == 2
