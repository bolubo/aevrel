from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import prod

from .mesh import Mesh, Placement


DTYPE_BYTES = {
    "float32": 4,
    "float16": 2,
    "bfloat16": 2,
    "int64": 8,
    "int32": 4,
    "uint8": 1,
}


@dataclass(frozen=True)
class Region:
    offset: tuple[int, ...]
    size: tuple[int, ...]

    @property
    def end(self) -> tuple[int, ...]:
        return tuple(start + length for start, length in zip(self.offset, self.size))

    @property
    def element_count(self) -> int:
        return prod(self.size)


@lru_cache(maxsize=65536)
def rank_coord(rank: int, shape: tuple[int, ...]) -> tuple[int, ...]:
    if rank < 0 or rank >= prod(shape):
        raise ValueError(f"rank {rank} is out of range for mesh shape {shape}")
    coord = [0] * len(shape)
    remaining = rank
    for axis in range(len(shape) - 1, -1, -1):
        coord[axis] = remaining % shape[axis]
        remaining //= shape[axis]
    return tuple(coord)


@lru_cache(maxsize=65536)
def split_lengths(length: int, parts: int) -> tuple[int, ...]:
    if length < 0:
        raise ValueError("length must be non-negative")
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, extra = divmod(length, parts)
    return tuple(base + (1 if index < extra else 0) for index in range(parts))


@lru_cache(maxsize=65536)
def local_region(
    mesh: Mesh,
    placements: tuple[Placement, ...],
    tensor_shape: tuple[int, ...],
    rank: int,
    *,
    allow_partial: bool = False,
) -> Region:
    if len(placements) != len(mesh.shape):
        raise ValueError("placement count must match mesh rank")
    if (
        any(placement.kind == "partial" for placement in placements)
        and not allow_partial
    ):
        raise ValueError("Partial placement has no direct local region")
    if len(tensor_shape) == 0:
        raise ValueError("tensor shape must not be empty")

    coord = rank_coord(rank, mesh.shape)
    offset = [0] * len(tensor_shape)
    size = list(tensor_shape)
    for dim, dim_length in enumerate(tensor_shape):
        start = 0
        length = dim_length
        for axis, placement in enumerate(placements):
            if placement.kind != "shard" or placement.dim != dim:
                continue
            lengths = split_lengths(length, mesh.shape[axis])
            axis_rank = coord[axis]
            start += sum(lengths[:axis_rank])
            length = lengths[axis_rank]
        offset[dim] = start
        size[dim] = length
    return Region(tuple(offset), tuple(size))


def contains(region: Region, offset: tuple[int, ...], size: tuple[int, ...]) -> bool:
    if len(offset) != len(region.offset) or len(size) != len(region.size):
        return False
    if any(length <= 0 for length in size):
        return False
    return all(
        start >= outer_start and start + length <= outer_start + outer_length
        for start, length, outer_start, outer_length in zip(
            offset, size, region.offset, region.size
        )
    )
