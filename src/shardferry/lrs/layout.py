"""VPS-lite layout algebra for TP/DP/ZeRO tensor sharding.

The goal is to plan source-shard -> target-shard transfers at range
granularity, without materializing the full tensor. PP is a stage assignment,
not a tensor split; it is handled separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from typing import TypeAlias

import numpy as np


class LayoutError(ValueError):
    """Invalid layout or transfer plan."""


@dataclass(frozen=True)
class Replicate:
    def to_dict(self) -> dict[str, str]:
        return {"type": "replicate"}


@dataclass(frozen=True)
class Shard:
    dim: int

    def __post_init__(self) -> None:
        if self.dim < 0:
            raise LayoutError("shard dim must be non-negative")

    def to_dict(self) -> dict[str, int | str]:
        return {"type": "shard", "dim": self.dim}


@dataclass(frozen=True)
class Partial:
    reduce: str = "sum"

    def __post_init__(self) -> None:
        if self.reduce != "sum":
            raise LayoutError("only sum partials are supported")

    def to_dict(self) -> dict[str, str]:
        return {"type": "partial", "reduce": self.reduce}


Placement: TypeAlias = Replicate | Shard | Partial


def _split(total: int, parts: int) -> tuple[int, ...]:
    if parts < 1:
        raise LayoutError("parts must be positive")
    base, remainder = divmod(total, parts)
    return tuple(
        base + (1 if index < remainder else 0)
        for index in range(parts)
    )


def volume(sizes: tuple[int, ...]) -> int:
    if not sizes:
        return 1
    return math.prod(sizes)


@dataclass(frozen=True)
class TensorLayout:
    shape: tuple[int, ...]
    mesh_shape: tuple[int, ...]
    placements: tuple[Placement, ...]
    mesh_dim_names: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.shape:
            raise LayoutError("tensor shape must not be empty")
        if any(dim <= 0 for dim in self.shape):
            raise LayoutError("tensor dimensions must be positive")
        if not self.mesh_shape:
            raise LayoutError("mesh shape must not be empty")
        if any(dim <= 0 for dim in self.mesh_shape):
            raise LayoutError("mesh dimensions must be positive")
        if len(self.placements) != len(self.mesh_shape):
            raise LayoutError("placements must match mesh rank")
        if self.mesh_dim_names is not None:
            if len(self.mesh_dim_names) != len(self.mesh_shape):
                raise LayoutError("mesh_dim_names must match mesh rank")
        for placement in self.placements:
            if isinstance(placement, Shard):
                if placement.dim >= len(self.shape):
                    raise LayoutError("shard dim is outside the tensor")

    @property
    def rank_count(self) -> int:
        return math.prod(self.mesh_shape)

    def coords(self) -> tuple[tuple[int, ...], ...]:
        return tuple(
            product(*[range(dim) for dim in self.mesh_shape])
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "shape": list(self.shape),
            "mesh_shape": list(self.mesh_shape),
            "mesh_dim_names": (
                list(self.mesh_dim_names)
                if self.mesh_dim_names is not None
                else None
            ),
            "placements": [
                placement.to_dict()
                for placement in self.placements
            ],
            "rank_count": self.rank_count,
        }


@dataclass(frozen=True)
class ShardSlice:
    coord: tuple[int, ...]
    offsets: tuple[int, ...]
    sizes: tuple[int, ...]
    partial: bool = False
    replicated: bool = False

    @property
    def empty(self) -> bool:
        return any(size == 0 for size in self.sizes)


def iter_shards(layout: TensorLayout) -> dict[tuple[int, ...], ShardSlice]:
    axes_by_dim: dict[int, list[tuple[int, int]]] = {}
    for axis, placement in enumerate(layout.placements):
        if isinstance(placement, Shard):
            axes_by_dim.setdefault(placement.dim, []).append(
                (axis, layout.mesh_shape[axis])
            )
    partial = any(
        isinstance(placement, Partial)
        for placement in layout.placements
    )
    replicated = not axes_by_dim and not partial
    result: dict[tuple[int, ...], ShardSlice] = {}
    for coord in layout.coords():
        offsets = [0] * len(layout.shape)
        sizes = list(layout.shape)
        for dim, axes in axes_by_dim.items():
            offset = 0
            size = layout.shape[dim]
            for axis, mesh_size in axes:
                chunks = _split(size, mesh_size)
                offset += sum(chunks[: coord[axis]])
                size = chunks[coord[axis]]
            offsets[dim] = offset
            sizes[dim] = size
        result[coord] = ShardSlice(
            coord=coord,
            offsets=tuple(offsets),
            sizes=tuple(sizes),
            partial=partial,
            replicated=replicated,
        )
    return result


@dataclass(frozen=True)
class Copy:
    source_coord: tuple[int, ...]
    target_coord: tuple[int, ...]
    source_offsets: tuple[int, ...]
    target_offsets: tuple[int, ...]
    sizes: tuple[int, ...]
    reduce: bool = False

    @property
    def elements(self) -> int:
        return volume(self.sizes)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_coord": list(self.source_coord),
            "target_coord": list(self.target_coord),
            "source_offsets": list(self.source_offsets),
            "target_offsets": list(self.target_offsets),
            "sizes": list(self.sizes),
            "reduce": self.reduce,
        }


@dataclass(frozen=True)
class TransferPlan:
    source: TensorLayout
    target: TensorLayout
    copies: tuple[Copy, ...]
    max_validate_cells: int = 1_000_000

    def validate(self) -> "TransferPlan":
        if self.source.shape != self.target.shape:
            raise LayoutError("source and target shapes differ")
        target_shards = iter_shards(self.target)
        for target_coord, target_slice in target_shards.items():
            copies = [
                copy
                for copy in self.copies
                if copy.target_coord == target_coord
            ]
            cells = volume(target_slice.sizes)
            if cells == 0:
                continue
            if not copies:
                raise LayoutError(
                    f"target {target_coord} has no source copies"
                )
            reduce_flags = {copy.reduce for copy in copies}
            if len(reduce_flags) > 1:
                raise LayoutError(
                    f"target {target_coord} mixes reduce and assign copies"
                )
            for copy in copies:
                for offset, size, limit in zip(
                    copy.target_offsets,
                    copy.sizes,
                    target_slice.sizes,
                ):
                    if offset < 0 or offset + size > limit:
                        raise LayoutError(
                            f"copy target range is outside {target_coord}"
                        )
            if cells > self.max_validate_cells:
                continue
            cover = np.zeros(target_slice.sizes, dtype=np.int8)
            for copy in copies:
                destination = tuple(
                    slice(offset, offset + size)
                    for offset, size in zip(
                        copy.target_offsets,
                        copy.sizes,
                    )
                )
                cover[destination] += 1
            if np.any(cover == 0):
                raise LayoutError(
                    f"target {target_coord} is not fully covered"
                )
            if not reduce_flags and np.any(cover > 1):
                raise LayoutError(
                    f"target {target_coord} has overlapping copies"
                )
        return self

    def total_bytes(self, item_size: int) -> int:
        if item_size <= 0:
            raise LayoutError("item_size must be positive")
        return sum(
            copy.elements * item_size
            for copy in self.copies
        )

    def target_bytes(self, item_size: int) -> int:
        return sum(
            volume(slice_.sizes) * item_size
            for slice_ in iter_shards(self.target).values()
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.to_dict(),
            "target": self.target.to_dict(),
            "copies": [copy.to_dict() for copy in self.copies],
        }


def _intersection(
    target: ShardSlice,
    source: ShardSlice,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
] | None:
    source_offsets: list[int] = []
    target_offsets: list[int] = []
    sizes: list[int] = []
    for target_offset, target_size, source_offset, source_size in zip(
        target.offsets,
        target.sizes,
        source.offsets,
        source.sizes,
    ):
        start = max(target_offset, source_offset)
        end = min(
            target_offset + target_size,
            source_offset + source_size,
        )
        if start >= end:
            return None
        source_offsets.append(start - source_offset)
        target_offsets.append(start - target_offset)
        sizes.append(end - start)
    return (
        tuple(source_offsets),
        tuple(target_offsets),
        tuple(sizes),
    )


def plan_transfer(
    source: TensorLayout,
    target: TensorLayout,
) -> TransferPlan:
    if source.shape != target.shape:
        raise LayoutError("source and target shapes differ")
    source_shards = iter_shards(source)
    target_shards = iter_shards(target)
    copies: list[Copy] = []
    for target_coord, target_slice in target_shards.items():
        assigned: dict[
            tuple[tuple[int, ...], tuple[int, ...]],
            Copy,
        ] = {}
        reduced: list[Copy] = []
        for source_coord, source_slice in source_shards.items():
            overlap = _intersection(target_slice, source_slice)
            if overlap is None:
                continue
            source_offsets, target_offsets, sizes = overlap
            copy = Copy(
                source_coord=source_coord,
                target_coord=target_coord,
                source_offsets=source_offsets,
                target_offsets=target_offsets,
                sizes=sizes,
                reduce=source_slice.partial,
            )
            if copy.reduce:
                reduced.append(copy)
            else:
                key = (copy.target_offsets, copy.sizes)
                if key not in assigned or copy.source_coord == target_coord:
                    assigned[key] = copy
        copies.extend(assigned.values())
        copies.extend(reduced)
    plan = TransferPlan(
        source=source,
        target=target,
        copies=tuple(copies),
    )
    plan.validate()
    return plan


def execute_plan(
    plan: TransferPlan,
    source_locals: dict[tuple[int, ...], np.ndarray],
) -> dict[tuple[int, ...], np.ndarray]:
    dtype = next(iter(source_locals.values())).dtype
    result: dict[tuple[int, ...], np.ndarray] = {}
    for target_coord, target_slice in iter_shards(plan.target).items():
        output = np.zeros(target_slice.sizes, dtype=dtype)
        reduced = np.zeros_like(output)
        has_reduce = False
        has_assign = False
        for copy in plan.copies:
            if copy.target_coord != target_coord:
                continue
            source = source_locals[copy.source_coord]
            source_region = tuple(
                slice(offset, offset + size)
                for offset, size in zip(
                    copy.source_offsets,
                    copy.sizes,
                )
            )
            target_region = tuple(
                slice(offset, offset + size)
                for offset, size in zip(
                    copy.target_offsets,
                    copy.sizes,
                )
            )
            data = source[source_region]
            if copy.reduce:
                reduced[target_region] += data
                has_reduce = True
            else:
                output[target_region] = data
                has_assign = True
        if has_reduce and has_assign:
            raise LayoutError("cannot mix reduce and assign copies")
        result[target_coord] = reduced if has_reduce else output
    return result


def byte_ranges(
    shape: tuple[int, ...],
    offsets: tuple[int, ...],
    sizes: tuple[int, ...],
    item_size: int,
) -> tuple[tuple[int, int], ...]:
    if item_size <= 0:
        raise LayoutError("item_size must be positive")
    if len(shape) != len(offsets) or len(shape) != len(sizes):
        raise LayoutError("shape, offsets, and sizes must match")
    if not shape:
        return ((0, item_size),)
    if any(size == 0 for size in sizes):
        return ()
    for offset, size, limit in zip(offsets, sizes, shape):
        if offset < 0 or size < 0 or offset + size > limit:
            raise LayoutError("range is outside the tensor")
    strides = [1] * len(shape)
    for index in range(len(shape) - 2, -1, -1):
        strides[index] = strides[index + 1] * shape[index + 1]
    if len(shape) == 1:
        return (
            (offsets[0] * item_size, sizes[0] * item_size),
        )
    ranges: list[tuple[int, int]] = []
    outer = [
        range(offsets[index], offsets[index] + sizes[index])
        for index in range(len(shape) - 1)
    ]
    for outer_index in product(*outer):
        start = sum(
            outer_index[index] * strides[index]
            for index in range(len(shape) - 1)
        ) + offsets[-1]
        start_byte = start * item_size
        length_byte = sizes[-1] * item_size
        if ranges and ranges[-1][0] + ranges[-1][1] == start_byte:
            ranges[-1] = (
                ranges[-1][0],
                ranges[-1][1] + length_byte,
            )
        else:
            ranges.append((start_byte, length_byte))
    return tuple(ranges)
