from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator

from shardferry.manifest import load_spec
from shardferry.planning import PlannerError, plan, validate_plan
from tests.fixtures.generated import (
    checksum,
    generate_partial_source_spec,
    generate_sharded_spec,
    mesh_payload,
)
from tests.fixtures.toy import make_tensor
from tests.oracle.reference import (
    assert_bitwise_equal,
    materialize_plan,
    reference_reshard,
)

ROOT = Path(__file__).resolve().parents[2]
PLAN_SCHEMA = ROOT / "spec" / "plan.schema.json"


def test_example_4to3_plan() -> None:
    spec = load_spec(ROOT / "examples" / "toy-4to3" / "spec.yaml")
    result = plan(spec)
    assert len(result["nodes"]) == 8
    assert result["total_bytes"] == 128
    assert result["theoretical_min_bytes"] == 128
    assert result["cost"]["bytes"] == 128
    assert result["cost"]["range_reads"] == 8
    assert result["cost"]["max_fan_in"] == 1
    assert result["cost"]["nodes"] == 8
    assert result["cost"]["local_reads"] + result["cost"]["remote_reads"] == 8
    assert {node["target_rank"] for node in result["nodes"]} == {0, 1, 2}
    Draft202012Validator(
        json.loads(PLAN_SCHEMA.read_text(encoding="utf-8"))
    ).validate(result)


def test_full_cover_source_avoids_irrelevant_boundaries() -> None:
    checksum_value = "sha256:" + "a" * 64
    shards = [
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/full.bin",
            "offset": [0, 0],
            "size": [256, 1],
            "checksum": checksum_value,
        }
    ]
    for rank in range(1, 128):
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": f"file:///source/rank{rank}.bin",
                "offset": [rank * 2, 0],
                "size": [2, 1],
                "checksum": checksum_value,
            }
        )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [256, 1], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((128,), ("Shard(0)",)),
            "storage": {"uri": "file:///source"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert result["full_gather"] is False
    assert len(result["nodes"]) == 2
    assert result["cost"]["nodes"] == 2
    assert result["cost"]["range_reads"] == 2
    assert result["cost"]["local_reads"] == 1
    assert result["cost"]["remote_reads"] == 1
    assert result["total_bytes"] == 1024
    assert result["theoretical_min_bytes"] == 1024


def test_partial_full_group_uses_one_node_per_target_region() -> None:
    spec, _, _ = generate_partial_source_spec(
        (4, 2), 2, (2,), ("Shard(0)",)
    )
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 2
    assert all(node["op"] == "sum" for node in result["nodes"])
    assert all(len(node["inputs"]) == 2 for node in result["nodes"])


def test_multiple_source_chunks_per_rank_have_stable_ids() -> None:
    tensor = np.arange(8, dtype=np.float32)
    left = tensor[:4].copy()
    right = tensor[4:].copy()
    shards = [
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/rank0-left.bin",
            "offset": [0],
            "size": [4],
            "checksum": checksum(left),
        },
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/rank0-right.bin",
            "offset": [4],
            "size": [4],
            "checksum": checksum(right),
        },
    ]
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [8], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///source"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 2
    source_ids = {
        node["inputs"][0]["source_id"]
        for node in result["nodes"]
    }
    assert len(source_ids) == 2
    assert all(source_id.startswith("sha256:") for source_id in source_ids)


def test_partial_source_allows_multiple_chunks_per_rank() -> None:
    values = np.arange(8, dtype=np.float32)
    shards = []
    for rank in range(2):
        for offset, part in ((0, values[:4]), (4, values[4:])):
            shards.append(
                {
                    "tensor": "w",
                    "rank": rank,
                    "uri": f"file:///partial/rank{rank}-{offset}.bin",
                    "offset": [offset],
                    "size": [4],
                    "checksum": checksum(part),
                }
            )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [8], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((2,), ("Partial(sum)",)),
            "storage": {"uri": "file:///partial"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 2
    assert all(node["op"] == "sum" for node in result["nodes"])
    assert {
        tuple(sorted(source["source_rank"] for source in node["inputs"]))
        for node in result["nodes"]
    } == {(0, 1)}


def test_adjacent_same_source_nodes_are_coalesced() -> None:
    values = np.arange(16, dtype=np.float32).reshape(4, 4)
    shards = [
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/left.bin",
            "offset": [0, 0],
            "size": [4, 2],
            "checksum": checksum(values[:, :2].copy()),
        },
        {
            "tensor": "w",
            "rank": 1,
            "uri": "file:///source/right-bottom.bin",
            "offset": [2, 2],
            "size": [2, 2],
            "checksum": checksum(values[2:, 2:].copy()),
        },
        {
            "tensor": "w",
            "rank": 2,
            "uri": "file:///source/right-top.bin",
            "offset": [0, 2],
            "size": [2, 2],
            "checksum": checksum(values[:2, 2:].copy()),
        },
    ]
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [4, 4], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((3,), ("Shard(0)",)),
            "storage": {"uri": "file:///source"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((1,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 3
    assert any(
        node["size"] == [4, 2] and node["inputs"][0]["source_rank"] == 0
        for node in result["nodes"]
    )
    assert result["cost"]["nodes"] == 3
    assert result["cost"]["range_reads"] == 3


def test_multiple_source_chunks_per_rank_have_stable_ids() -> None:
    tensor = np.arange(8, dtype=np.float32)
    left = tensor[:4].copy()
    right = tensor[4:].copy()
    shards = [
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/rank0-left.bin",
            "offset": [0],
            "size": [4],
            "checksum": checksum(left),
        },
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/rank0-right.bin",
            "offset": [4],
            "size": [4],
            "checksum": checksum(right),
        },
    ]
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [8], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///source"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 2
    source_ids = {
        node["inputs"][0]["source_id"]
        for node in result["nodes"]
    }
    assert len(source_ids) == 2
    assert all(source_id.startswith("sha256:") for source_id in source_ids)


def test_partial_source_allows_multiple_chunks_per_rank() -> None:
    values = np.arange(8, dtype=np.float32)
    shards = []
    for rank in range(2):
        for offset, part in ((0, values[:4]), (4, values[4:])):
            shards.append(
                {
                    "tensor": "w",
                    "rank": rank,
                    "uri": f"file:///partial/rank{rank}-{offset}.bin",
                    "offset": [offset],
                    "size": [4],
                    "checksum": checksum(part),
                }
            )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [8], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((2,), ("Partial(sum)",)),
            "storage": {"uri": "file:///partial"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 2
    assert all(node["op"] == "sum" for node in result["nodes"])
    assert {
        tuple(sorted(source["source_rank"] for source in node["inputs"]))
        for node in result["nodes"]
    } == {(0, 1)}


def test_adjacent_same_source_nodes_are_coalesced() -> None:
    values = np.arange(16, dtype=np.float32).reshape(4, 4)
    shards = [
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///source/left.bin",
            "offset": [0, 0],
            "size": [4, 2],
            "checksum": checksum(values[:, :2].copy()),
        },
        {
            "tensor": "w",
            "rank": 1,
            "uri": "file:///source/right-bottom.bin",
            "offset": [2, 2],
            "size": [2, 2],
            "checksum": checksum(values[2:, 2:].copy()),
        },
        {
            "tensor": "w",
            "rank": 2,
            "uri": "file:///source/right-top.bin",
            "offset": [0, 2],
            "size": [2, 2],
            "checksum": checksum(values[:2, 2:].copy()),
        },
    ]
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [4, 4], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((3,), ("Shard(0)",)),
            "storage": {"uri": "file:///source"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((1,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    validate_plan(result, spec)
    assert len(result["nodes"]) == 3
    assert any(
        node["size"] == [4, 2] and node["inputs"][0]["source_rank"] == 0
        for node in result["nodes"]
    )
    assert result["cost"]["nodes"] == 3
    assert result["cost"]["range_reads"] == 3


def test_plan_is_deterministic() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    assert plan(spec) == plan(deepcopy(spec))


def test_migration_id_ignores_storage_locations_and_rank_endpoints() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    first = plan(spec)["migration_id"]
    relocated = deepcopy(spec)
    relocated["target"]["storage"]["uri"] = "file:///different/target"
    relocated["source"]["mesh"]["rank_map"] = [
        {"rank": rank, "endpoint": f"http://rank{rank}:9000"}
        for rank in range(4)
    ]
    for shard in relocated["source"]["shards"]:
        shard["uri"] = f"http://agent{shard['rank']}:9000/shard/w/{shard['rank']}"
    relocated["source"]["shards"].reverse()
    second = plan(relocated)["migration_id"]
    assert first == second


def test_plan_handles_multiple_tensors() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    spec["checkpoint"]["tensors"].append(
        {"name": "b", "shape": [8, 4], "dtype": "float32"}
    )
    original_shards = list(spec["source"]["shards"])
    spec["source"]["shards"].extend(
        {
            **deepcopy(shard),
            "tensor": "b",
            "uri": shard["uri"].replace("rank", "b-rank"),
        }
        for shard in original_shards
    )
    result = plan(spec)
    validate_plan(result, spec)
    assert result["total_bytes"] == 256
    assert {node["tensor"] for node in result["nodes"]} == {"w", "b"}


def test_plan_rejects_source_gap() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    spec["source"]["shards"].pop()
    with pytest.raises(PlannerError, match="coverage gap"):
        plan(spec)


def test_plan_rejects_unsupported_target_partial_op() -> None:
    spec, _, _ = generate_sharded_spec(
        (4, 2), (2,), ("Shard(0)",), (1,), ("Partial(max)",)
    )
    with pytest.raises(PlannerError, match="target Partial"):
        plan(spec)


def test_plan_rejects_incomplete_partial_group() -> None:
    spec, _, _ = generate_partial_source_spec(
        (4, 2), 2, (2,), ("Shard(0)",)
    )
    spec["source"]["shards"].pop()
    with pytest.raises(PlannerError, match="incomplete Partial"):
        plan(spec)


def test_plan_rejects_mixed_partial_ops() -> None:
    spec, _, _ = generate_partial_source_spec(
        (4, 2), 4, (1,), ("Replicate()",)
    )
    spec["source"]["mesh"]["shape"] = [2, 2]
    spec["source"]["mesh"]["world_size"] = 4
    spec["source"]["mesh"]["placements"] = ["Partial(sum)", "Partial(max)"]
    with pytest.raises(PlannerError, match="multiple Partial"):
        plan(spec)


def test_partial_source_is_reduced() -> None:
    spec, source_arrays, tensor = generate_partial_source_spec(
        (4, 2), 2, (2,), ("Shard(0)",)
    )
    result = plan(spec)
    assert all(node["op"] == "sum" for node in result["nodes"])
    assert all(len(node["inputs"]) == 2 for node in result["nodes"])

    produced = materialize_plan(result, spec, source_arrays)
    expected = reference_reshard(tensor, (2,), ("Shard(0)",))
    for rank, (_, expected_array) in expected.items():
        assert_bitwise_equal(produced[("w", rank)], expected_array)


def test_partial_group_with_sharded_source_axis() -> None:
    tensor = make_tensor((4, 2))
    source_arrays = {
        ("w", 0): tensor[0:2].copy(),
        ("w", 1): np.zeros((2, 2), dtype=tensor.dtype),
        ("w", 2): tensor[2:4].copy(),
        ("w", 3): np.zeros((2, 2), dtype=tensor.dtype),
    }
    shards = [
        {
            "tensor": "w",
            "rank": 0,
            "uri": "file:///partial/rank0.bin",
            "offset": [0, 0],
            "size": [2, 2],
            "checksum": checksum(source_arrays[("w", 0)]),
        },
        {
            "tensor": "w",
            "rank": 1,
            "uri": "file:///partial/rank1.bin",
            "offset": [0, 0],
            "size": [2, 2],
            "checksum": checksum(source_arrays[("w", 1)]),
        },
        {
            "tensor": "w",
            "rank": 2,
            "uri": "file:///partial/rank2.bin",
            "offset": [2, 0],
            "size": [2, 2],
            "checksum": checksum(source_arrays[("w", 2)]),
        },
        {
            "tensor": "w",
            "rank": 3,
            "uri": "file:///partial/rank3.bin",
            "offset": [2, 0],
            "size": [2, 2],
            "checksum": checksum(source_arrays[("w", 3)]),
        },
    ]
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [4, 2], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload(
                (2, 2), ("Shard(0)", "Partial(sum)")
            ),
            "storage": {"uri": "file:///partial"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": "file:///target"},
        },
    }
    result = plan(spec)
    assert all(node["op"] == "sum" for node in result["nodes"])
    assert {
        tuple(source["source_rank"] for source in node["inputs"])
        for node in result["nodes"]
    } == {(0, 1), (2, 3)}

    produced = materialize_plan(result, spec, source_arrays)
    expected = reference_reshard(tensor, (2,), ("Shard(0)",))
    for rank, (_, expected_array) in expected.items():
        assert_bitwise_equal(produced[("w", rank)], expected_array)


def test_validate_plan_rejects_overlap() -> None:
    spec, _, _ = generate_sharded_spec(
        (4, 2), (2,), ("Shard(0)",), (2,), ("Shard(0)",)
    )
    result = plan(spec)
    result["nodes"].append(deepcopy(result["nodes"][0]))
    with pytest.raises(PlannerError):
        validate_plan(result, spec)


def test_validate_plan_rejects_bad_source_offset() -> None:
    spec, _, _ = generate_sharded_spec(
        (4, 2), (2,), ("Shard(0)",), (2,), ("Shard(0)",)
    )
    result = plan(spec)
    result["nodes"][0]["inputs"][0]["source_offset"] = [3, 0]
    result["nodes"][0]["size"] = [2, 2]
    with pytest.raises(PlannerError):
        validate_plan(result, spec)
