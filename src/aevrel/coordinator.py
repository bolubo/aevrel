from __future__ import annotations

from math import prod
from typing import Any

from .planning.layout import DTYPE_BYTES
from .planning.mesh import parse_mesh
from .planning import validate_plan


def expected_rank_work(
    plan: dict[str, Any],
    spec: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    validate_plan(plan, spec)
    target_mesh = parse_mesh(spec["target"]["mesh"])
    tensor_names = [
        tensor["name"] for tensor in spec["checkpoint"]["tensors"]
    ]
    item_sizes = {
        tensor["name"]: DTYPE_BYTES[tensor["dtype"]]
        for tensor in spec["checkpoint"]["tensors"]
    }
    expected = {
        rank: {
            "chunks": 0,
            "bytes_planned": 0,
            "tensors": {name: 0 for name in tensor_names},
        }
        for rank in range(target_mesh.world_size)
    }
    for node in plan["nodes"]:
        rank = node["target_rank"]
        row = expected[rank]
        row["chunks"] += 1
        row["bytes_planned"] += (
            prod(node["size"])
            * item_sizes[node["tensor"]]
            * len(node["inputs"])
        )
        row["tensors"][node["tensor"]] += 1
    return expected
