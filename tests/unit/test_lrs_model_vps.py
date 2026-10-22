from __future__ import annotations

import math

import numpy as np
import pytest

from shardferry.lrs.layout import LayoutError, iter_shards, volume
from shardferry.lrs.model_vps import (
    FlatBufferLayout,
    FlatBufferSpec,
    ModelVPS,
    ParallelStrategy,
    ParamSpec,
)


def _dynatrain_vps() -> ModelVPS:
    params = [
        ParamSpec("embedding", (8, 4), stage="first"),
    ]
    params += [
        ParamSpec(
            f"layer{layer}",
            (4, 4),
            tp_dim=1,
            layer_id=layer,
        )
        for layer in range(4)
    ]
    params += [
        ParamSpec("final_norm", (4,), stage="last"),
        ParamSpec("output", (4, 4), stage="last"),
    ]
    return ModelVPS(params, num_layers=4)


def _state(vps: ModelVPS) -> dict[str, np.ndarray]:
    result = {}
    for index, spec in enumerate(vps.params):
        numel = math.prod(spec.shape)
        result[spec.name] = (
            np.arange(numel, dtype=np.float64) + index * 1000
        ).reshape(spec.shape)
    return result


def _assert_target(plan, vps: ModelVPS, state) -> None:
    result = vps.execute(plan, state)
    expected = vps.local_shards(state, plan.target)
    assert set(result) == set(expected)
    for rank in expected:
        assert set(result[rank]) == set(expected[rank])
        for name in expected[rank]:
            np.testing.assert_array_equal(
                result[rank][name],
                expected[rank][name],
            )


def test_parallel_rank_mapping() -> None:
    strategy = ParallelStrategy(tp=2, pp=2, dp=2)
    assert strategy.world_size == 8
    ranks = {
        strategy.global_rank(
            tp_rank=tp,
            pp_rank=pp,
            dp_rank=dp,
        )
        for tp in range(2)
        for pp in range(2)
        for dp in range(2)
    }
    assert ranks == set(range(8))
    for rank in range(8):
        tp, pp, dp, ep = strategy.coords(rank)
        assert (
            strategy.global_rank(
                tp_rank=tp,
                pp_rank=pp,
                dp_rank=dp,
                ep_rank=ep,
            )
            == rank
        )
    assert strategy.coords(0) == (0, 0, 0, 0)
    assert strategy.coords(1) == (1, 0, 0, 0)
    assert strategy.coords(2) == (0, 0, 1, 0)
    assert strategy.coords(4) == (0, 1, 0, 0)


def test_dynatrain_tp_pp_plan() -> None:
    vps = _dynatrain_vps()
    state = _state(vps)
    source = ParallelStrategy(tp=2, pp=2, dp=1)
    target = ParallelStrategy(tp=4, pp=1, dp=1)
    plan = vps.plan(source, target)
    _assert_target(plan, vps, state)

    assert {
        spec.name for spec in vps.params_on_stage(source, 0)
    } == {"embedding", "layer0", "layer1"}
    assert {
        spec.name for spec in vps.params_on_stage(source, 1)
    } == {"layer2", "layer3", "final_norm", "output"}
    assert len(vps.params_on_stage(target, 0)) == len(vps.params)

    target_bytes = (
        sum(
            volume(shard.sizes)
            for spec in vps.params
            for shard in iter_shards(
                vps.param_layout(spec, target)
            ).values()
        )
        * 8
    )
    assert plan.total_bytes(8) == target_bytes
    assert plan.network_bytes(8) > 0
    assert plan.retain_bytes(8) > 0
    assert (
        plan.network_bytes(8) + plan.retain_bytes(8)
        == plan.total_bytes(8)
    )
    for source_rank, targets in plan.send_info.items():
        for target_rank, copies in targets.items():
            assert (
                len(plan.recv_info[target_rank][source_rank])
                == len(copies)
            )
    assert all(
        copy.source_global == copy.target_global
        for copies in plan.retain_info.values()
        for copy in copies
    )


def test_etc_pp4_to_pp2_plan() -> None:
    vps = ModelVPS(
        [
            ParamSpec(
                f"layer{layer}",
                (8,),
                layer_id=layer,
            )
            for layer in range(16)
        ],
        num_layers=16,
    )
    state = _state(vps)
    source = ParallelStrategy(tp=1, pp=4, dp=1)
    target = ParallelStrategy(tp=1, pp=2, dp=1)
    plan = vps.plan(source, target)
    _assert_target(plan, vps, state)

    assert plan.network_bytes(8) == 12 * 8 * 8
    assert plan.retain_bytes(8) == 4 * 8 * 8
    assert plan.total_bytes(8) == 16 * 8 * 8
    assert {
        copy.param
        for copy in plan.retain_info[0]
    } == {"layer0", "layer1", "layer2", "layer3"}
    assert {
        copy.param
        for copies in plan.send_info[1].values()
        for copy in copies
    } == {"layer4", "layer5", "layer6", "layer7"}
    assert {
        spec.name for spec in vps.params_on_stage(target, 0)
    } == {f"layer{layer}" for layer in range(8)}
    assert {
        spec.name for spec in vps.params_on_stage(target, 1)
    } == {f"layer{layer}" for layer in range(8, 16)}


def test_noop_all_retain() -> None:
    vps = _dynatrain_vps()
    state = _state(vps)
    strategy = ParallelStrategy(tp=2, pp=2, dp=1)
    plan = vps.plan(strategy, strategy)
    _assert_target(plan, vps, state)
    assert plan.network_bytes(8) == 0
    assert plan.retain_bytes(8) == plan.total_bytes(8)
    assert plan.send_info == {}
    assert plan.recv_info == {}
    assert plan.retain_info


def test_flat_buffer_dp_2_to_4() -> None:
    spec = FlatBufferSpec(
        params=(("a", 10), ("b", 20), ("c", 30)),
        bucket_size=32,
    )
    layout = FlatBufferLayout(spec)
    flat = np.arange(60, dtype=np.float64)
    source = layout.distribution(2)
    target = layout.distribution(4)
    assert sum(item.size for item in source[0]) == 30
    assert sum(item.size for item in source[1]) == 30
    assert all(
        sum(item.size for item in ranges) == 15
        for ranges in target.values()
    )

    plan = layout.plan_transfer(2, 4)
    result = layout.execute(plan, flat)
    expected = layout.local_arrays(flat, 4)
    for rank in expected:
        np.testing.assert_array_equal(result[rank], expected[rank])
    assert (
        plan.network_bytes(8) + plan.retain_bytes(8)
        == layout.target_bytes(4, 8)
        == 60 * 8
    )

    noop = layout.plan_transfer(2, 2)
    assert noop.network_bytes(8) == 0
    assert noop.retain_bytes(8) == 60 * 8
    noop_result = layout.execute(noop, flat)
    noop_expected = layout.local_arrays(flat, 2)
    for rank in noop_expected:
        np.testing.assert_array_equal(
            noop_result[rank],
            noop_expected[rank],
        )


def test_param_stage_uneven() -> None:
    vps = ModelVPS(
        [
            ParamSpec("l0", (4,), layer_id=0),
            ParamSpec("l2", (4,), layer_id=2),
            ParamSpec("l3", (4,), layer_id=3),
        ],
        num_layers=5,
    )
    strategy = ParallelStrategy(tp=1, pp=2, dp=1)
    assert {
        spec.name for spec in vps.params_on_stage(strategy, 0)
    } == {"l0", "l2"}
    assert {
        spec.name for spec in vps.params_on_stage(strategy, 1)
    } == {"l3"}


def test_invalid_inputs() -> None:
    with pytest.raises(LayoutError):
        ParamSpec("bad", (4,), tp_dim=2)
    with pytest.raises(LayoutError):
        ParamSpec("bad", (4,), layer_id=0, stage="first")
    with pytest.raises(LayoutError):
        ParallelStrategy(tp=0, pp=1, dp=1)
    with pytest.raises(LayoutError):
        FlatBufferSpec(params=(("a", 1), ("a", 2)))
