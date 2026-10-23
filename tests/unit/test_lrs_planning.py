from __future__ import annotations

import numpy as np
import pytest

from aevrel.lrs.layout import LayoutError
from aevrel.lrs.model_vps import (
    FlatBufferLayout,
    FlatBufferSpec,
    FlatCopy,
    ModelCopy,
    ModelVPS,
    ParallelStrategy,
    ParamSpec,
)
from aevrel.lrs.planning import (
    CostMatrix,
    coalesce_flat_copies,
    coalesce_model_copies,
    hungarian,
    infer_param_specs,
    infer_tp_dim,
    promote_copies,
    xor_schedule,
)


def _copy(
    param: str,
    source: int,
    target: int,
    *,
    size: int = 8,
    reduce: bool = False,
    retain: bool = False,
) -> ModelCopy:
    return ModelCopy(
        param=param,
        source_global=source,
        target_global=target,
        source_offsets=(0,),
        target_offsets=(0,),
        sizes=(size,),
        reduce=reduce,
        retain=retain,
    )


def test_hungarian_rectangular() -> None:
    pairs, total = hungarian(
        np.array([[1.0, 2.0, 3.0], [3.0, 2.0, 1.0]])
    )
    assert pairs == [(0, 0), (1, 2)]
    assert total == 2.0

    pairs, total = hungarian(
        np.array([[1.0, 2.0], [2.0, 1.0], [3.0, 3.0]])
    )
    assert pairs == [(0, 0), (1, 1)]
    assert total == 2.0


def test_etc_cost_matrix_pairing() -> None:
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
    plan = vps.plan(
        ParallelStrategy(tp=1, pp=4, dp=1),
        ParallelStrategy(tp=1, pp=2, dp=1),
    )
    matrix = CostMatrix.from_model_plan(plan, item_size=8)
    assert matrix.target_ranks == (0, 1)
    assert matrix.source_ranks == (0, 1, 2, 3)
    assert matrix.costs == (
        (256, 256, 512, 512),
        (512, 512, 256, 256),
    )
    pairing = matrix.pair()
    assert pairing.total_cost == 512
    assert {pair.target_rank for pair in pairing.pairs} == {0, 1}
    assert all(pair.cost == 256 for pair in pairing.pairs)
    assert len(pairing.unpaired_sources) == 2


def test_infer_tp_dim() -> None:
    assert infer_tp_dim((4, 4), (4, 2), 2) == 1
    assert infer_tp_dim((4, 1), (2, 1), 2) == 0
    assert infer_tp_dim((4,), (4,), 2) is None
    with pytest.raises(LayoutError):
        infer_tp_dim((4, 4), (2, 2), 2)
    with pytest.raises(LayoutError):
        infer_tp_dim((4,), (3,), 2)

    specs = infer_param_specs(
        {"w1": (4, 4), "w2": (4, 1), "ln": (4,)},
        {"w1": (4, 2), "w2": (2, 1), "ln": (4,)},
        2,
        {
            "w1": {"layer_id": 0},
            "w2": {"layer_id": 0},
            "ln": {"stage": "last"},
        },
    )
    assert [spec.tp_dim for spec in specs] == [1, 0, None]
    assert specs[0].layer_id == 0
    assert specs[2].stage == "last"


def test_coalesce_flat_copies() -> None:
    copies = (
        FlatCopy("a", 0, 0, 0, 0, 4, False),
        FlatCopy("a", 0, 0, 4, 4, 4, False),
        FlatCopy("a", 0, 0, 9, 9, 4, False),
    )
    merged = coalesce_flat_copies(copies)
    assert sorted(copy.size for copy in merged) == [4, 8]


def test_coalesce_model_copies() -> None:
    copies = (
        ModelCopy("a", 0, 1, (0,), (0,), (4,), False, False),
        ModelCopy("a", 0, 1, (4,), (4,), (4,), False, False),
        ModelCopy("a", 0, 1, (0, 0), (0, 0), (2, 2), False, False),
    )
    merged = coalesce_model_copies(copies)
    sizes = sorted(copy.sizes for copy in merged)
    assert (8,) in sizes
    assert (2, 2) in sizes


def test_xor_schedule() -> None:
    copies = [
        _copy("p", 0, 1),
        _copy("p", 0, 2),
        _copy("p", 1, 3),
        _copy("p", 2, 3),
    ]
    rounds = xor_schedule(copies, 4)
    assert [round_.step for round_ in rounds] == [1, 2]
    for round_ in rounds:
        ranks = [
            rank
            for pair in round_.pairs
            for rank in (pair.source_rank, pair.target_rank)
        ]
        assert len(ranks) == len(set(ranks))
    scheduled = {
        (pair.source_rank, pair.target_rank)
        for round_ in rounds
        for pair in round_.pairs
    }
    assert scheduled == {(0, 1), (0, 2), (1, 3), (2, 3)}


def test_promote_copies() -> None:
    broadcast = [
        _copy("broadcast", 0, target)
        for target in (0, 1, 2)
    ]
    gather = [
        _copy("gather", source, 3)
        for source in (0, 1)
    ]
    reduce = [
        _copy("reduce", source, 4, reduce=True)
        for source in (0, 1)
    ]
    p2p = [_copy("p2p", 5, 6)]
    ops = promote_copies(broadcast + gather + reduce + p2p)
    kinds = {op.kind for op in ops}
    assert {"broadcast", "gather", "reduce", "p2p"} <= kinds
    broadcast_op = next(
        op for op in ops if op.kind == "broadcast"
    )
    assert broadcast_op.source_ranks == (0,)
    assert broadcast_op.target_ranks == (0, 1, 2)
    gather_op = next(op for op in ops if op.kind == "gather")
    assert gather_op.source_ranks == (0, 1)
    assert gather_op.target_ranks == (3,)


def test_interleaved_flat_buffer() -> None:
    layout = FlatBufferLayout(
        FlatBufferSpec(
            params=(("a", 10), ("b", 20), ("c", 30)),
            interleaved=True,
        )
    )
    flat = np.arange(60, dtype=np.float64)
    source = layout.distribution(2)
    assert [item.size for item in source[0]] == [5, 10, 15]
    assert [item.size for item in source[1]] == [5, 10, 15]
    assert [item.offset for item in source[1]] == [5, 10, 15]
    target = layout.distribution(4)
    assert [item.size for item in target[0]] == [3, 5, 8]
    assert [item.size for item in target[1]] == [3, 5, 8]
    assert [item.size for item in target[2]] == [2, 5, 7]
    assert [item.size for item in target[3]] == [2, 5, 7]

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
    for rank, values in layout.local_arrays(flat, 2).items():
        np.testing.assert_array_equal(noop_result[rank], values)
