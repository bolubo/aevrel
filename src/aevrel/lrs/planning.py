"""A2.2 planning: cost matrix, meta TP inference, coalescing, scheduling."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .layout import LayoutError, volume
from .model_vps import (
    FlatCopy,
    FlatTransferPlan,
    ModelCopy,
    ModelTransferPlan,
    ModelVPS,
    ParallelStrategy,
    ParamSpec,
)


def hungarian(cost: np.ndarray) -> tuple[list[tuple[int, int]], float]:
    """Minimum-cost assignment for a rectangular matrix.

    The result covers every row when rows <= columns, otherwise every column.
    This is the standard O(n^3) potentials implementation.
    """
    matrix = np.asarray(cost, dtype=np.float64)
    if matrix.ndim != 2:
        raise LayoutError("cost matrix must be two-dimensional")
    rows, cols = matrix.shape
    if rows == 0 or cols == 0:
        return [], 0.0
    if rows > cols:
        pairs, total = hungarian(matrix.T)
        return [(column, row) for row, column in pairs], total

    u = np.zeros(rows + 1)
    v = np.zeros(cols + 1)
    p = np.zeros(cols + 1, dtype=int)
    way = np.zeros(cols + 1, dtype=int)
    for row in range(1, rows + 1):
        p[0] = row
        column0 = 0
        minv = np.full(cols + 1, np.inf)
        used = np.zeros(cols + 1, dtype=bool)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = np.inf
            column1 = 0
            for column in range(1, cols + 1):
                if used[column]:
                    continue
                current = (
                    matrix[row0 - 1, column - 1]
                    - u[row0]
                    - v[column]
                )
                if current < minv[column]:
                    minv[column] = current
                    way[column] = column0
                if minv[column] < delta:
                    delta = minv[column]
                    column1 = column
            for column in range(cols + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    minv[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break
    pairs = [
        (int(p[column]) - 1, column - 1)
        for column in range(1, cols + 1)
        if p[column] != 0
    ]
    total = sum(matrix[row, column] for row, column in pairs)
    return sorted(pairs), float(total)


@dataclass(frozen=True)
class RankPair:
    source_rank: int
    target_rank: int
    cost: int


@dataclass(frozen=True)
class RankPairing:
    pairs: tuple[RankPair, ...]
    unpaired_sources: tuple[int, ...]
    unpaired_targets: tuple[int, ...]
    total_cost: int


@dataclass(frozen=True)
class CostMatrix:
    source_ranks: tuple[int, ...]
    target_ranks: tuple[int, ...]
    costs: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if len(self.costs) != len(self.target_ranks):
            raise LayoutError("cost matrix rows must match targets")
        if any(len(row) != len(self.source_ranks) for row in self.costs):
            raise LayoutError("cost matrix cols must match sources")
        if any(cost < 0 for row in self.costs for cost in row):
            raise LayoutError("costs must be non-negative")

    @classmethod
    def from_model_plan(
        cls,
        plan: ModelTransferPlan,
        item_size: int,
    ) -> "CostMatrix":
        source_ranks = tuple(range(plan.source.world_size))
        target_ranks = tuple(range(plan.target.world_size))
        target_bytes = {rank: 0 for rank in target_ranks}
        overlap: dict[tuple[int, int], int] = {}
        for param in plan.params:
            for copy in param.copies:
                amount = copy.elements * item_size
                target_bytes[copy.target_global] += amount
                key = (copy.target_global, copy.source_global)
                overlap[key] = overlap.get(key, 0) + amount
        costs = tuple(
            tuple(
                max(
                    0,
                    target_bytes[target]
                    - overlap.get((target, source), 0),
                )
                for source in source_ranks
            )
            for target in target_ranks
        )
        return cls(
            source_ranks=source_ranks,
            target_ranks=target_ranks,
            costs=costs,
        )

    def pair(self) -> RankPairing:
        pairs, total = hungarian(
            np.asarray(self.costs, dtype=np.float64)
        )
        result = [
            RankPair(
                source_rank=self.source_ranks[source],
                target_rank=self.target_ranks[target],
                cost=int(self.costs[target][source]),
            )
            for target, source in pairs
        ]
        paired_sources = {pair.source_rank for pair in result}
        paired_targets = {pair.target_rank for pair in result}
        return RankPairing(
            pairs=tuple(result),
            unpaired_sources=tuple(
                rank
                for rank in self.source_ranks
                if rank not in paired_sources
            ),
            unpaired_targets=tuple(
                rank
                for rank in self.target_ranks
                if rank not in paired_targets
            ),
            total_cost=int(total),
        )


def infer_tp_dim(
    full_shape: tuple[int, ...],
    local_shape: tuple[int, ...],
    tp_size: int,
) -> int | None:
    if len(full_shape) != len(local_shape):
        raise LayoutError("full/local shapes must have the same rank")
    if tp_size < 1:
        raise LayoutError("tp_size must be positive")
    if all(
        full == local
        for full, local in zip(full_shape, local_shape)
    ):
        return None
    matches = [
        dim
        for dim, (full, local) in enumerate(
            zip(full_shape, local_shape)
        )
        if full == local * tp_size and full % tp_size == 0
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LayoutError("cannot infer the TP dimension")
    raise LayoutError("ambiguous TP dimension")


def infer_param_specs(
    full_shapes: dict[str, tuple[int, ...]],
    local_shapes: dict[str, tuple[int, ...]],
    tp_size: int,
    metadata: dict[str, dict[str, int | str | None]],
) -> tuple[ParamSpec, ...]:
    specs = []
    for name, full_shape in full_shapes.items():
        if name not in local_shapes:
            raise LayoutError(f"missing local shape for {name}")
        if name not in metadata:
            raise LayoutError(f"missing metadata for {name}")
        meta = metadata[name]
        specs.append(
            ParamSpec(
                name=name,
                shape=full_shape,
                tp_dim=infer_tp_dim(
                    full_shape,
                    local_shapes[name],
                    tp_size,
                ),
                layer_id=meta.get("layer_id"),
                stage=meta.get("stage"),
            )
        )
    return tuple(specs)


def coalesce_flat_copies(
    copies: Iterable[FlatCopy],
) -> tuple[FlatCopy, ...]:
    groups: dict[
        tuple[str, int, int, bool],
        list[FlatCopy],
    ] = defaultdict(list)
    for copy in copies:
        groups[
            (
                copy.param,
                copy.source_dp,
                copy.target_dp,
                copy.retain,
            )
        ].append(copy)
    result: list[FlatCopy] = []
    for group in groups.values():
        group.sort(key=lambda copy: copy.target_local_offset)
        current = group[0]
        for copy in group[1:]:
            if (
                current.source_local_offset + current.size
                == copy.source_local_offset
                and current.target_local_offset + current.size
                == copy.target_local_offset
            ):
                current = FlatCopy(
                    param=current.param,
                    source_dp=current.source_dp,
                    target_dp=current.target_dp,
                    source_local_offset=current.source_local_offset,
                    target_local_offset=current.target_local_offset,
                    size=current.size + copy.size,
                    retain=current.retain,
                )
            else:
                result.append(current)
                current = copy
        result.append(current)
    return tuple(result)


def coalesce_model_copies(
    copies: Iterable[ModelCopy],
) -> tuple[ModelCopy, ...]:
    groups: dict[
        tuple[str, int, int, bool, bool],
        list[ModelCopy],
    ] = defaultdict(list)
    result: list[ModelCopy] = []
    for copy in copies:
        if len(copy.sizes) != 1:
            result.append(copy)
            continue
        groups[
            (
                copy.param,
                copy.source_global,
                copy.target_global,
                copy.reduce,
                copy.retain,
            )
        ].append(copy)
    for group in groups.values():
        group.sort(key=lambda copy: copy.target_offsets[0])
        current = group[0]
        for copy in group[1:]:
            if (
                current.source_offsets[0] + current.sizes[0]
                == copy.source_offsets[0]
                and current.target_offsets[0] + current.sizes[0]
                == copy.target_offsets[0]
            ):
                current = ModelCopy(
                    param=current.param,
                    source_global=current.source_global,
                    target_global=current.target_global,
                    source_offsets=(current.source_offsets[0],),
                    target_offsets=(current.target_offsets[0],),
                    sizes=(current.sizes[0] + copy.sizes[0],),
                    reduce=current.reduce,
                    retain=current.retain,
                )
            else:
                result.append(current)
                current = copy
        result.append(current)
    return tuple(result)


@dataclass(frozen=True)
class SchedulePair:
    source_rank: int
    target_rank: int
    copies: tuple[object, ...]


@dataclass(frozen=True)
class ScheduleRound:
    step: int
    pairs: tuple[SchedulePair, ...]


def xor_schedule(
    copies: Iterable[ModelCopy | FlatCopy],
    num_ranks: int,
) -> tuple[ScheduleRound, ...]:
    """XOR-based deadlock-free round schedule.

    Rank i talks to i^step at logical step ``step``; every rank has at
    most one peer per round. Retained copies are ignored.
    """
    if num_ranks < 1:
        raise LayoutError("num_ranks must be positive")
    grouped: dict[tuple[int, int], list[object]] = defaultdict(list)
    for copy in copies:
        if getattr(copy, "retain", False):
            continue
        source = copy.source_global if hasattr(
            copy, "source_global"
        ) else copy.source_dp
        target = copy.target_global if hasattr(
            copy, "target_global"
        ) else copy.target_dp
        grouped[(min(source, target), max(source, target))].append(copy)
    limit = 1
    while limit < num_ranks:
        limit <<= 1
    rounds: list[ScheduleRound] = []
    for step in range(1, limit):
        pairs = []
        for source in range(limit):
            target = source ^ step
            if target <= source:
                continue
            items = grouped.get((source, target))
            if items:
                pairs.append(
                    SchedulePair(
                        source_rank=source,
                        target_rank=target,
                        copies=tuple(items),
                    )
                )
        if pairs:
            rounds.append(
                ScheduleRound(step=step, pairs=tuple(pairs))
            )
    return tuple(rounds)


@dataclass(frozen=True)
class CollectiveOp:
    kind: str
    param: str
    source_ranks: tuple[int, ...]
    target_ranks: tuple[int, ...]
    source_offsets: tuple[int, ...]
    target_offsets: tuple[int, ...]
    sizes: tuple[int, ...]
    reduce: bool


def promote_copies(
    copies: Iterable[ModelCopy],
) -> tuple[CollectiveOp, ...]:
    """Promote groups of identical P2P copies to broadcast/gather/reduce."""
    remaining = list(copies)
    result: list[CollectiveOp] = []

    broadcast_groups: dict[
        tuple[
            str,
            int,
            tuple[int, ...],
            tuple[int, ...],
            tuple[int, ...],
        ],
        list[ModelCopy],
    ] = defaultdict(list)
    for copy in remaining:
        if copy.retain or copy.reduce:
            continue
        broadcast_groups[
            (
                copy.param,
                copy.source_global,
                copy.source_offsets,
                copy.target_offsets,
                copy.sizes,
            )
        ].append(copy)
    broadcast_ids: set[int] = set()
    for group in broadcast_groups.values():
        targets = {copy.target_global for copy in group}
        if len(targets) <= 1:
            continue
        first = group[0]
        result.append(
            CollectiveOp(
                kind="broadcast",
                param=first.param,
                source_ranks=(first.source_global,),
                target_ranks=tuple(sorted(targets)),
                source_offsets=first.source_offsets,
                target_offsets=first.target_offsets,
                sizes=first.sizes,
                reduce=False,
            )
        )
        broadcast_ids.update(id(copy) for copy in group)
    remaining = [
        copy for copy in remaining if id(copy) not in broadcast_ids
    ]

    gather_groups: dict[
        tuple[
            str,
            int,
            tuple[int, ...],
            tuple[int, ...],
        ],
        list[ModelCopy],
    ] = defaultdict(list)
    for copy in remaining:
        if copy.retain:
            continue
        gather_groups[
            (
                copy.param,
                copy.target_global,
                copy.target_offsets,
                copy.sizes,
            )
        ].append(copy)
    gather_ids: set[int] = set()
    for group in gather_groups.values():
        sources = {copy.source_global for copy in group}
        if len(sources) <= 1:
            continue
        first = group[0]
        result.append(
            CollectiveOp(
                kind="reduce" if first.reduce else "gather",
                param=first.param,
                source_ranks=tuple(sorted(sources)),
                target_ranks=(first.target_global,),
                source_offsets=first.source_offsets,
                target_offsets=first.target_offsets,
                sizes=first.sizes,
                reduce=first.reduce,
            )
        )
        gather_ids.update(id(copy) for copy in group)
    remaining = [
        copy for copy in remaining if id(copy) not in gather_ids
    ]
    for copy in remaining:
        result.append(
            CollectiveOp(
                kind="p2p",
                param=copy.param,
                source_ranks=(copy.source_global,),
                target_ranks=(copy.target_global,),
                source_offsets=copy.source_offsets,
                target_offsets=copy.target_offsets,
                sizes=copy.sizes,
                reduce=copy.reduce,
            )
        )
    return tuple(result)
