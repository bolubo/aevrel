from __future__ import annotations

import pytest

from shardferry.planning.mesh import parse_mesh, parse_placement, validate_placement_dims


def test_parse_placements() -> None:
    assert parse_placement("Shard(1)").dim == 1
    assert parse_placement("Replicate()").kind == "replicate"
    assert parse_placement("Partial(sum)").reduce_op == "sum"


def test_invalid_placement_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_placement("Shard(x)")


def test_unknown_partial_reduce_op_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_placement("Partial(multiply)")


def test_parse_2d_mesh() -> None:
    mesh = parse_mesh(
        {
            "shape": [2, 2],
            "world_size": 4,
            "placements": ["Shard(0)", "Shard(1)"],
        }
    )
    assert mesh.shape == (2, 2)
    assert mesh.world_size == 4
    assert len(mesh.placements) == 2


def test_world_size_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_mesh(
            {
                "shape": [2, 2],
                "world_size": 3,
                "placements": ["Shard(0)", "Shard(1)"],
            }
        )


def test_shard_dim_is_tensor_dimension_not_mesh_dimension() -> None:
    mesh = parse_mesh(
        {
            "shape": [2, 2],
            "world_size": 4,
            "placements": ["Shard(2)", "Shard(3)"],
        }
    )
    validate_placement_dims(mesh, tensor_rank=4)
    with pytest.raises(ValueError):
        validate_placement_dims(mesh, tensor_rank=3)
