from __future__ import annotations

import pytest

from shardferry.planning.layout import Region, local_region, split_lengths
from shardferry.planning.mesh import parse_mesh


def test_split_lengths_puts_remainder_first() -> None:
    assert split_lengths(8, 3) == (3, 3, 2)
    assert split_lengths(2, 3) == (1, 1, 0)


def test_local_region_1d_world_size_change() -> None:
    mesh = parse_mesh(
        {"shape": [3], "world_size": 3, "placements": ["Shard(0)"]}
    )
    assert local_region(mesh, mesh.placements, (8, 4), 0) == Region((0, 0), (3, 4))
    assert local_region(mesh, mesh.placements, (8, 4), 1) == Region((3, 0), (3, 4))
    assert local_region(mesh, mesh.placements, (8, 4), 2) == Region((6, 0), (2, 4))


def test_local_region_2d_mesh() -> None:
    mesh = parse_mesh(
        {
            "shape": [2, 2],
            "world_size": 4,
            "placements": ["Shard(0)", "Shard(1)"],
        }
    )
    assert local_region(mesh, mesh.placements, (8, 4), 0) == Region((0, 0), (4, 2))
    assert local_region(mesh, mesh.placements, (8, 4), 1) == Region((0, 2), (4, 2))
    assert local_region(mesh, mesh.placements, (8, 4), 2) == Region((4, 0), (4, 2))
    assert local_region(mesh, mesh.placements, (8, 4), 3) == Region((4, 2), (4, 2))


def test_replicate_keeps_full_tensor() -> None:
    mesh = parse_mesh(
        {"shape": [2], "world_size": 2, "placements": ["Replicate()"]}
    )
    for rank in range(2):
        assert local_region(mesh, mesh.placements, (8, 4), rank) == Region(
            (0, 0), (8, 4)
        )


def test_nested_shard_on_same_tensor_dim() -> None:
    mesh = parse_mesh(
        {
            "shape": [2, 2],
            "world_size": 4,
            "placements": ["Shard(0)", "Shard(0)"],
        }
    )
    assert local_region(mesh, mesh.placements, (7,), 0) == Region((0,), (2,))
    assert local_region(mesh, mesh.placements, (7,), 3) == Region((6,), (1,))


def test_empty_local_region_when_mesh_is_larger_than_tensor_dim() -> None:
    mesh = parse_mesh(
        {"shape": [4], "world_size": 4, "placements": ["Shard(0)"]}
    )
    assert local_region(mesh, mesh.placements, (2,), 3) == Region((2,), (0,))


def test_partial_has_no_direct_local_region() -> None:
    mesh = parse_mesh(
        {"shape": [2], "world_size": 2, "placements": ["Partial(sum)"]}
    )
    with pytest.raises(ValueError):
        local_region(mesh, mesh.placements, (8, 4), 0)
