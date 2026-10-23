from __future__ import annotations

import numpy as np

from aevrel.lrs.layout import Shard, TensorLayout, iter_shards
from aevrel.lrs.tp_pp import TPPPConfig, TPPPSimulator


def test_a2_layout_matches_m5_tp_pp_shards() -> None:
    config = TPPPConfig()
    sim = TPPPSimulator(config)
    hidden_per_tp = config.hidden // config.tp

    w1_layout = TensorLayout(
        (config.d_in, config.hidden),
        (config.tp,),
        (Shard(1),),
        ("tp",),
    )
    w2_layout = TensorLayout(
        (config.hidden, config.d_out),
        (config.tp,),
        (Shard(0),),
        ("tp",),
    )
    assert w1_layout.rank_count == config.tp
    assert w2_layout.rank_count == config.tp
    assert w1_layout.rank_count * config.pp == 4

    w1_shards = iter_shards(w1_layout)
    w2_shards = iter_shards(w2_layout)
    for tp in range(config.tp):
        stage0 = w1_shards[(tp,)]
        assert stage0.sizes == (config.d_in, hidden_per_tp)
        local_w1 = sim.shards[tp].reshape(stage0.sizes)
        expected_w1 = sim.w1_init[
            :,
            stage0.offsets[1] : stage0.offsets[1] + stage0.sizes[1],
        ]
        np.testing.assert_array_equal(local_w1, expected_w1)

        stage1 = w2_shards[(tp,)]
        assert stage1.sizes == (hidden_per_tp, config.d_out)
        rank = config.tp + tp
        local_w2 = sim.shards[rank].reshape(stage1.sizes)
        expected_w2 = sim.w2_init[
            stage1.offsets[0] : stage1.offsets[0] + stage1.sizes[0],
            :,
        ]
        np.testing.assert_array_equal(local_w2, expected_w2)
