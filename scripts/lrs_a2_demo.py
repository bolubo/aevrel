#!/usr/bin/env python3
"""A2.2 demo: ETC pairing, DynaTrain model plan, and interleaved ZeRO."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from shardferry.lrs import (  # noqa: E402
    CostMatrix,
    FlatBufferLayout,
    FlatBufferSpec,
    ModelVPS,
    ParallelStrategy,
    ParamSpec,
    infer_tp_dim,
)


def _etc_model() -> ModelVPS:
    return ModelVPS(
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


def _dynatrain_model() -> ModelVPS:
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


def main() -> int:
    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-a2"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / "proof.json"

    etc = _etc_model()
    etc_plan = etc.plan(
        ParallelStrategy(tp=1, pp=4, dp=1),
        ParallelStrategy(tp=1, pp=2, dp=1),
    )
    etc_matrix = CostMatrix.from_model_plan(etc_plan, item_size=8)
    etc_pairing = etc_matrix.pair()

    dyna = _dynatrain_model()
    dyna_plan = dyna.plan(
        ParallelStrategy(tp=2, pp=2, dp=1),
        ParallelStrategy(tp=4, pp=1, dp=1),
    )

    interleaved = FlatBufferLayout(
        FlatBufferSpec(
            params=(("a", 10), ("b", 20), ("c", 30)),
            interleaved=True,
        )
    )
    flat = np.arange(60, dtype=np.float64)
    flat_plan = interleaved.plan_transfer(2, 4)
    flat_result = interleaved.execute(flat_plan, flat)
    flat_expected = interleaved.local_arrays(flat, 4)
    flat_ok = all(
        np.array_equal(flat_result[rank], flat_expected[rank])
        for rank in flat_expected
    )

    meta = {
        "w1": infer_tp_dim((4, 4), (4, 2), 2),
        "w2": infer_tp_dim((4, 1), (2, 1), 2),
        "ln": infer_tp_dim((4,), (4,), 2),
    }
    checks = {
        "etc pairing total": etc_pairing.total_cost == 512,
        "etc costs": etc_matrix.costs
        == (
            (256, 256, 512, 512),
            (512, 512, 256, 256),
        ),
        "dyna plan bytes": (
            dyna_plan.network_bytes(8) + dyna_plan.retain_bytes(8)
            == dyna_plan.total_bytes(8)
        ),
        "interleaved execute": flat_ok,
        "interleaved bytes": (
            flat_plan.network_bytes(8) + flat_plan.retain_bytes(8)
            == interleaved.target_bytes(4, 8)
            == 60 * 8
        ),
        "meta tp": meta == {"w1": 1, "w2": 0, "ln": None},
    }

    payload = {
        "schema": "shardferry.lrs.a2.proof.v1",
        "checks": checks,
        "etc_pairing": {
            "costs": [list(row) for row in etc_matrix.costs],
            "pairing": asdict(etc_pairing),
        },
        "dynatrain": {
            "network_bytes": dyna_plan.network_bytes(8),
            "retain_bytes": dyna_plan.retain_bytes(8),
            "total_bytes": dyna_plan.total_bytes(8),
        },
        "interleaved": {
            "dp2_sizes": [
                item.size
                for item in interleaved.distribution(2)[0]
            ],
            "dp4_sizes": [
                item.size
                for item in interleaved.distribution(4)[0]
            ],
            "network_bytes": flat_plan.network_bytes(8),
            "retain_bytes": flat_plan.retain_bytes(8),
        },
        "meta_tp": meta,
    }
    evidence.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    print("LRS A2.2 · ETC pairing + DynaTrain plan + interleaved ZeRO")
    print(
        f"etc costs={etc_matrix.costs} "
        f"pairs={[(p.source_rank, p.target_rank) for p in etc_pairing.pairs]} "
        f"total={etc_pairing.total_cost}"
    )
    print(
        f"dyna network={dyna_plan.network_bytes(8)} "
        f"retain={dyna_plan.retain_bytes(8)} "
        f"total={dyna_plan.total_bytes(8)}"
    )
    print(
        f"interleaved dp2={payload['interleaved']['dp2_sizes']} "
        f"dp4={payload['interleaved']['dp4_sizes']} "
        f"network={flat_plan.network_bytes(8)} "
        f"retain={flat_plan.retain_bytes(8)}"
    )
    print(f"meta_tp={meta}")
    print(f"evidence {evidence}")
    if not all(checks.values()):
        print("A2.2 FAILED")
        return 1
    print("A2.2 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
