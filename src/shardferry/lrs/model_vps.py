"""Model-level VPS and flat-buffer plans (DynaTrain/ETC-inspired).

This module composes the tensor-level layout plan into a model-level plan:
TP/PP/DP parameter resharding plus a generic ZeRO flat-buffer distribution.
It is framework-agnostic and uses numpy for the oracle.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .layout import (
    LayoutError,
    Replicate,
    Shard,
    TensorLayout,
    TransferPlan,
    execute_plan,
    iter_shards,
    plan_transfer,
    volume,
)


@dataclass(frozen=True)
class ParallelStrategy:
    tp: int
    pp: int
    dp: int
    ep: int = 1

    def __post_init__(self) -> None:
        if self.tp < 1 or self.pp < 1 or self.dp < 1 or self.ep < 1:
            raise LayoutError("parallel dimensions must be positive")

    @property
    def world_size(self) -> int:
        return self.tp * self.pp * self.dp * self.ep

    def global_rank(
        self,
        *,
        tp_rank: int,
        pp_rank: int,
        dp_rank: int,
        ep_rank: int = 0,
    ) -> int:
        """DynaTrain-compatible order: TP -> EP -> DP -> PP."""
        if not 0 <= tp_rank < self.tp:
            raise LayoutError("tp_rank out of range")
        if not 0 <= pp_rank < self.pp:
            raise LayoutError("pp_rank out of range")
        if not 0 <= dp_rank < self.dp:
            raise LayoutError("dp_rank out of range")
        if not 0 <= ep_rank < self.ep:
            raise LayoutError("ep_rank out of range")
        return (
            ((pp_rank * self.dp + dp_rank) * self.ep + ep_rank)
            * self.tp
            + tp_rank
        )

    def coords(
        self,
        rank: int,
    ) -> tuple[int, int, int, int]:
        if not 0 <= rank < self.world_size:
            raise LayoutError("rank out of range")
        tp_rank = rank % self.tp
        rest = rank // self.tp
        ep_rank = rest % self.ep
        rest //= self.ep
        dp_rank = rest % self.dp
        pp_rank = rest // self.dp
        return tp_rank, pp_rank, dp_rank, ep_rank


@dataclass(frozen=True)
class ParamSpec:
    name: str
    shape: tuple[int, ...]
    tp_dim: int | None = None
    layer_id: int | None = None
    stage: str | None = None

    def __post_init__(self) -> None:
        if not self.shape:
            raise LayoutError("parameter shape must not be empty")
        if any(dim <= 0 for dim in self.shape):
            raise LayoutError("parameter dims must be positive")
        if self.tp_dim is not None:
            if self.tp_dim < 0 or self.tp_dim >= len(self.shape):
                raise LayoutError("tp_dim is outside the parameter")
        if (self.layer_id is None) == (self.stage is None):
            raise LayoutError(
                "exactly one of layer_id and stage must be set"
            )
        if self.stage is not None and self.stage not in {"first", "last"}:
            raise LayoutError("stage must be 'first' or 'last'")


@dataclass(frozen=True)
class ModelCopy:
    param: str
    source_global: int
    target_global: int
    source_offsets: tuple[int, ...]
    target_offsets: tuple[int, ...]
    sizes: tuple[int, ...]
    reduce: bool
    retain: bool

    @property
    def elements(self) -> int:
        return volume(self.sizes)


@dataclass(frozen=True)
class ParamTransferPlan:
    name: str
    source_stage: int
    target_stage: int
    source_layout: TensorLayout
    target_layout: TensorLayout
    layout_plan: TransferPlan
    copies: tuple[ModelCopy, ...]

    def network_bytes(self, item_size: int) -> int:
        return sum(
            copy.elements * item_size
            for copy in self.copies
            if not copy.retain
        )

    def retain_bytes(self, item_size: int) -> int:
        return sum(
            copy.elements * item_size
            for copy in self.copies
            if copy.retain
        )

    def total_bytes(self, item_size: int) -> int:
        return sum(
            copy.elements * item_size
            for copy in self.copies
        )


@dataclass(frozen=True)
class ModelTransferPlan:
    source: ParallelStrategy
    target: ParallelStrategy
    params: tuple[ParamTransferPlan, ...]
    send_info: dict[int, dict[int, tuple[ModelCopy, ...]]]
    recv_info: dict[int, dict[int, tuple[ModelCopy, ...]]]
    retain_info: dict[int, tuple[ModelCopy, ...]]

    def network_bytes(self, item_size: int) -> int:
        return sum(
            param.network_bytes(item_size)
            for param in self.params
        )

    def retain_bytes(self, item_size: int) -> int:
        return sum(
            param.retain_bytes(item_size)
            for param in self.params
        )

    def total_bytes(self, item_size: int) -> int:
        return sum(
            param.total_bytes(item_size)
            for param in self.params
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source": {
                "tp": self.source.tp,
                "pp": self.source.pp,
                "dp": self.source.dp,
                "ep": self.source.ep,
            },
            "target": {
                "tp": self.target.tp,
                "pp": self.target.pp,
                "dp": self.target.dp,
                "ep": self.target.ep,
            },
            "params": [
                {
                    "name": param.name,
                    "source_stage": param.source_stage,
                    "target_stage": param.target_stage,
                    "copies": [
                        {
                            "source_global": copy.source_global,
                            "target_global": copy.target_global,
                            "source_offsets": list(copy.source_offsets),
                            "target_offsets": list(copy.target_offsets),
                            "sizes": list(copy.sizes),
                            "reduce": copy.reduce,
                            "retain": copy.retain,
                        }
                        for copy in param.copies
                    ],
                }
                for param in self.params
            ],
        }


class ModelVPS:
    """A model-wide virtual parameter space."""

    def __init__(
        self,
        params: Iterable[ParamSpec],
        *,
        num_layers: int,
    ) -> None:
        self.params = tuple(params)
        self.num_layers = int(num_layers)
        if self.num_layers < 1:
            raise LayoutError("num_layers must be positive")
        names = [param.name for param in self.params]
        if len(names) != len(set(names)):
            raise LayoutError("parameter names must be unique")
        for param in self.params:
            if (
                param.layer_id is not None
                and param.layer_id >= self.num_layers
            ):
                raise LayoutError(
                    f"layer_id {param.layer_id} is outside num_layers"
                )

    def _stage_ranges(self, pp: int) -> tuple[tuple[int, int], ...]:
        base, remainder = divmod(self.num_layers, pp)
        ranges = []
        start = 0
        for stage in range(pp):
            size = base + (1 if stage < remainder else 0)
            ranges.append((start, start + size))
            start += size
        return tuple(ranges)

    def param_stage(
        self,
        spec: ParamSpec,
        strategy: ParallelStrategy,
    ) -> int:
        if spec.stage == "first":
            return 0
        if spec.stage == "last":
            return strategy.pp - 1
        for stage, (start, end) in enumerate(
            self._stage_ranges(strategy.pp)
        ):
            if start <= spec.layer_id < end:
                return stage
        raise LayoutError(f"could not assign {spec.name} to a stage")

    def params_on_stage(
        self,
        strategy: ParallelStrategy,
        stage: int,
    ) -> tuple[ParamSpec, ...]:
        return tuple(
            spec
            for spec in self.params
            if self.param_stage(spec, strategy) == stage
        )

    def param_layout(
        self,
        spec: ParamSpec,
        strategy: ParallelStrategy,
    ) -> TensorLayout:
        tp_placement = (
            Shard(spec.tp_dim)
            if spec.tp_dim is not None
            else Replicate()
        )
        return TensorLayout(
            shape=spec.shape,
            mesh_shape=(strategy.dp, strategy.tp),
            placements=(Replicate(), tp_placement),
            mesh_dim_names=("dp", "tp"),
        )

    def plan(
        self,
        source: ParallelStrategy,
        target: ParallelStrategy,
    ) -> ModelTransferPlan:
        send_info: dict[int, dict[int, list[ModelCopy]]] = defaultdict(
            lambda: defaultdict(list)
        )
        recv_info: dict[int, dict[int, list[ModelCopy]]] = defaultdict(
            lambda: defaultdict(list)
        )
        retain_info: dict[int, list[ModelCopy]] = defaultdict(list)
        params: list[ParamTransferPlan] = []
        for spec in self.params:
            source_stage = self.param_stage(spec, source)
            target_stage = self.param_stage(spec, target)
            source_layout = self.param_layout(spec, source)
            target_layout = self.param_layout(spec, target)
            layout_plan = plan_transfer(source_layout, target_layout)
            copies: list[ModelCopy] = []
            for copy in layout_plan.copies:
                source_global = source.global_rank(
                    tp_rank=copy.source_coord[1],
                    pp_rank=source_stage,
                    dp_rank=copy.source_coord[0],
                )
                target_global = target.global_rank(
                    tp_rank=copy.target_coord[1],
                    pp_rank=target_stage,
                    dp_rank=copy.target_coord[0],
                )
                retain = source_global == target_global
                model_copy = ModelCopy(
                    param=spec.name,
                    source_global=source_global,
                    target_global=target_global,
                    source_offsets=copy.source_offsets,
                    target_offsets=copy.target_offsets,
                    sizes=copy.sizes,
                    reduce=copy.reduce,
                    retain=retain,
                )
                copies.append(model_copy)
                if retain:
                    retain_info[source_global].append(model_copy)
                else:
                    send_info[source_global][target_global].append(
                        model_copy
                    )
                    recv_info[target_global][source_global].append(
                        model_copy
                    )
            params.append(
                ParamTransferPlan(
                    name=spec.name,
                    source_stage=source_stage,
                    target_stage=target_stage,
                    source_layout=source_layout,
                    target_layout=target_layout,
                    layout_plan=layout_plan,
                    copies=tuple(copies),
                )
            )
        return ModelTransferPlan(
            source=source,
            target=target,
            params=tuple(params),
            send_info={
                source_rank: {
                    target_rank: tuple(copies)
                    for target_rank, copies in targets.items()
                }
                for source_rank, targets in send_info.items()
            },
            recv_info={
                target_rank: {
                    source_rank: tuple(copies)
                    for source_rank, copies in sources.items()
                }
                for target_rank, sources in recv_info.items()
            },
            retain_info={
                rank: tuple(copies)
                for rank, copies in retain_info.items()
            },
        )

    def local_shards(
        self,
        full_state: dict[str, np.ndarray],
        strategy: ParallelStrategy,
    ) -> dict[int, dict[str, np.ndarray]]:
        result: dict[int, dict[str, np.ndarray]] = defaultdict(dict)
        for spec in self.params:
            stage = self.param_stage(spec, strategy)
            layout = self.param_layout(spec, strategy)
            full = full_state[spec.name]
            for (dp_rank, tp_rank), shard in iter_shards(layout).items():
                rank = strategy.global_rank(
                    tp_rank=tp_rank,
                    pp_rank=stage,
                    dp_rank=dp_rank,
                )
                region = tuple(
                    slice(offset, offset + size)
                    for offset, size in zip(shard.offsets, shard.sizes)
                )
                result[rank][spec.name] = full[region]
        return dict(result)

    def execute(
        self,
        plan: ModelTransferPlan,
        full_state: dict[str, np.ndarray],
    ) -> dict[int, dict[str, np.ndarray]]:
        result: dict[int, dict[str, np.ndarray]] = defaultdict(dict)
        for param_plan in plan.params:
            full = full_state[param_plan.name]
            source_locals: dict[
                tuple[int, ...],
                np.ndarray,
            ] = {}
            for coord, shard in iter_shards(
                param_plan.source_layout
            ).items():
                region = tuple(
                    slice(offset, offset + size)
                    for offset, size in zip(shard.offsets, shard.sizes)
                )
                source_locals[coord] = full[region]
            target_locals = execute_plan(
                param_plan.layout_plan,
                source_locals,
            )
            for coord, tensor in target_locals.items():
                rank = plan.target.global_rank(
                    tp_rank=coord[1],
                    pp_rank=param_plan.target_stage,
                    dp_rank=coord[0],
                )
                result[rank][param_plan.name] = tensor
        return dict(result)


@dataclass(frozen=True)
class FlatBufferSpec:
    params: tuple[tuple[str, int], ...]
    bucket_size: int | None = None
    interleaved: bool = False

    def __post_init__(self) -> None:
        if not self.params:
            raise LayoutError("flat buffer must contain parameters")
        names = [name for name, _ in self.params]
        if len(names) != len(set(names)):
            raise LayoutError("flat buffer names must be unique")
        if any(size < 1 for _, size in self.params):
            raise LayoutError("flat buffer sizes must be positive")
        if self.bucket_size is not None and self.bucket_size < 1:
            raise LayoutError("bucket_size must be positive")


@dataclass(frozen=True)
class FlatRange:
    param: str
    offset: int
    size: int


@dataclass(frozen=True)
class FlatCopy:
    param: str
    source_dp: int
    target_dp: int
    source_local_offset: int
    target_local_offset: int
    size: int
    retain: bool


@dataclass(frozen=True)
class FlatTransferPlan:
    source_dp: int
    target_dp: int
    copies: tuple[FlatCopy, ...]

    def network_bytes(self, item_size: int) -> int:
        return sum(
            copy.size * item_size
            for copy in self.copies
            if not copy.retain
        )

    def retain_bytes(self, item_size: int) -> int:
        return sum(
            copy.size * item_size
            for copy in self.copies
            if copy.retain
        )

    def total_bytes(self, item_size: int) -> int:
        return sum(copy.size * item_size for copy in self.copies)


class FlatBufferLayout:
    """Generic ZeRO-style flat buffer distribution.

    Parameters are laid out contiguously in buffer order; the buffer is
    split into buckets, and each bucket is evenly sharded across DP ranks.
    """

    def __init__(self, spec: FlatBufferSpec) -> None:
        self.spec = spec
        self.param_offsets: dict[str, int] = {}
        offset = 0
        for name, size in spec.params:
            self.param_offsets[name] = offset
            offset += size
        self.total = offset
        self.buckets: tuple[tuple[int, int], ...] = self._buckets()

    def _buckets(self) -> tuple[tuple[int, int], ...]:
        if (
            self.spec.bucket_size is None
            or self.spec.bucket_size >= self.total
        ):
            return ((0, self.total),)
        buckets = []
        start = 0
        while start < self.total:
            end = min(start + self.spec.bucket_size, self.total)
            buckets.append((start, end))
            start = end
        return tuple(buckets)

    def _param_ranges(self) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            (
                name,
                self.param_offsets[name],
                self.param_offsets[name] + size,
            )
            for name, size in self.spec.params
        )

    def distribution(
        self,
        dp_size: int,
    ) -> dict[int, tuple[FlatRange, ...]]:
        if dp_size < 1:
            raise LayoutError("dp_size must be positive")
        if self.spec.interleaved:
            result: dict[int, list[FlatRange]] = {
                rank: [] for rank in range(dp_size)
            }
            for rank in range(dp_size):
                for name, size in self.spec.params:
                    base, remainder = divmod(size, dp_size)
                    offset = rank * base + min(rank, remainder)
                    chunk = base + (1 if rank < remainder else 0)
                    if chunk:
                        result[rank].append(
                            FlatRange(
                                param=name,
                                offset=offset,
                                size=chunk,
                            )
                        )
            return {
                rank: tuple(ranges)
                for rank, ranges in result.items()
            }
        result: dict[int, list[FlatRange]] = {
            rank: [] for rank in range(dp_size)
        }
        for bucket_start, bucket_end in self.buckets:
            base, remainder = divmod(
                bucket_end - bucket_start,
                dp_size,
            )
            for rank in range(dp_size):
                shard_start = (
                    bucket_start
                    + rank * base
                    + min(rank, remainder)
                )
                shard_size = base + (1 if rank < remainder else 0)
                shard_end = shard_start + shard_size
                for name, param_start, param_end in self._param_ranges():
                    start = max(shard_start, param_start)
                    end = min(shard_end, param_end)
                    if start < end:
                        result[rank].append(
                            FlatRange(
                                param=name,
                                offset=start - param_start,
                                size=end - start,
                            )
                        )
        return {
            rank: tuple(ranges)
            for rank, ranges in result.items()
        }

    @staticmethod
    def _local_offsets(
        distribution: dict[int, tuple[FlatRange, ...]],
    ) -> dict[tuple[int, str, int], int]:
        result: dict[tuple[int, str, int], int] = {}
        for rank, ranges in distribution.items():
            local_offset = 0
            for item in ranges:
                result[(rank, item.param, item.offset)] = local_offset
                local_offset += item.size
        return result

    def plan_transfer(
        self,
        source_dp: int,
        target_dp: int,
    ) -> FlatTransferPlan:
        source = self.distribution(source_dp)
        target = self.distribution(target_dp)
        source_offsets = self._local_offsets(source)
        target_offsets = self._local_offsets(target)
        copies: list[FlatCopy] = []
        for target_rank, target_ranges in target.items():
            for target_range in target_ranges:
                target_range_end = target_range.offset + target_range.size
                for source_rank, source_ranges in source.items():
                    for source_range in source_ranges:
                        if source_range.param != target_range.param:
                            continue
                        source_range_end = (
                            source_range.offset + source_range.size
                        )
                        start = max(
                            target_range.offset,
                            source_range.offset,
                        )
                        end = min(
                            target_range_end,
                            source_range_end,
                        )
                        if start >= end:
                            continue
                        source_local = (
                            source_offsets[
                                (
                                    source_rank,
                                    source_range.param,
                                    source_range.offset,
                                )
                            ]
                            + (start - source_range.offset)
                        )
                        target_local = (
                            target_offsets[
                                (
                                    target_rank,
                                    target_range.param,
                                    target_range.offset,
                                )
                            ]
                            + (start - target_range.offset)
                        )
                        copies.append(
                            FlatCopy(
                                param=target_range.param,
                                source_dp=source_rank,
                                target_dp=target_rank,
                                source_local_offset=source_local,
                                target_local_offset=target_local,
                                size=end - start,
                                retain=source_rank == target_rank,
                            )
                        )
        return FlatTransferPlan(
            source_dp=source_dp,
            target_dp=target_dp,
            copies=tuple(copies),
        )

    def local_arrays(
        self,
        flat: np.ndarray,
        dp_size: int,
    ) -> dict[int, np.ndarray]:
        if flat.size != self.total:
            raise LayoutError("flat array size does not match the layout")
        result: dict[int, np.ndarray] = {}
        for rank, ranges in self.distribution(dp_size).items():
            arrays = [
                flat[
                    self.param_offsets[item.param]
                    + item.offset : self.param_offsets[item.param]
                    + item.offset
                    + item.size
                ]
                for item in ranges
            ]
            result[rank] = (
                np.concatenate(arrays)
                if arrays
                else np.empty(0, dtype=flat.dtype)
            )
        return result

    def execute(
        self,
        plan: FlatTransferPlan,
        flat: np.ndarray,
    ) -> dict[int, np.ndarray]:
        source = self.local_arrays(flat, plan.source_dp)
        target_arrays = {
            rank: np.zeros(
                sum(item.size for item in ranges),
                dtype=flat.dtype,
            )
            for rank, ranges in self.distribution(
                plan.target_dp
            ).items()
        }
        for copy in plan.copies:
            target = target_arrays[copy.target_dp]
            source_array = source[copy.source_dp]
            target[
                copy.target_local_offset : copy.target_local_offset
                + copy.size
            ] = source_array[
                copy.source_local_offset : copy.source_local_offset
                + copy.size
            ]
        return target_arrays

    def target_bytes(self, dp_size: int, item_size: int) -> int:
        return sum(
            item.size * item_size
            for ranges in self.distribution(dp_size).values()
            for item in ranges
        )
