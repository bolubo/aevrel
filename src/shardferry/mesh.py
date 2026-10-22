from __future__ import annotations

import re
from dataclasses import dataclass
from math import prod
from typing import Any


SUPPORTED_REDUCE_OPS = frozenset({"sum", "avg", "max", "min"})


@dataclass(frozen=True)
class Placement:
    kind: str
    dim: int | None = None
    reduce_op: str | None = None


@dataclass(frozen=True)
class Mesh:
    shape: tuple[int, ...]
    world_size: int
    placements: tuple[Placement, ...]


def parse_placement(value: str) -> Placement:
    match = re.fullmatch(r"Shard\((\d+)\)", value)
    if match:
        return Placement(kind="shard", dim=int(match.group(1)))
    if value == "Replicate()":
        return Placement(kind="replicate")
    match = re.fullmatch(r"Partial\(([a-z]+)\)", value)
    if match:
        reduce_op = match.group(1)
        if reduce_op not in SUPPORTED_REDUCE_OPS:
            raise ValueError(f"unsupported reduce op: {reduce_op}")
        return Placement(kind="partial", reduce_op=reduce_op)
    raise ValueError(f"invalid placement: {value}")


def parse_mesh(payload: dict[str, Any]) -> Mesh:
    try:
        shape = tuple(int(value) for value in payload["shape"])
        world_size = int(payload["world_size"])
        placements = tuple(parse_placement(value) for value in payload["placements"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid mesh: {exc}") from exc

    if not shape or any(dim <= 0 for dim in shape):
        raise ValueError("mesh shape must contain positive dimensions")
    if world_size != prod(shape):
        raise ValueError(
            f"world_size {world_size} does not match mesh shape product {prod(shape)}"
        )
    if len(placements) != len(shape):
        raise ValueError(
            f"expected {len(shape)} placements, got {len(placements)}"
        )

    return Mesh(shape=shape, world_size=world_size, placements=placements)


def validate_placement_dims(mesh: Mesh, tensor_rank: int) -> None:
    for placement in mesh.placements:
        if placement.kind == "shard" and placement.dim is not None:
            if placement.dim >= tensor_rank:
                raise ValueError(
                    f"shard dim {placement.dim} is outside tensor rank {tensor_rank}"
                )
