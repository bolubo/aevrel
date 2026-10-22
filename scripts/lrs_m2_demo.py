#!/usr/bin/env python3
"""Run LRS M2 W=8 F=1/2/3 and print the concurrency proof."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from shardferry.lrs import LRSSimulator, Migration, ToyConfig  # noqa: E402


def _digest(values) -> str:
    return hashlib.sha256(values.tobytes()).hexdigest()[:16]


def _config(
    *,
    dtype: str = "float64",
    reduction: str = "canonical",
) -> ToyConfig:
    return ToyConfig(
        world_size=8,
        shard_size=1,
        param_count=8,
        batch_per_rank=4,
        dataset_size=128,
        seed=7,
        dtype=dtype,
        reduction=reduction,
    )


def _concurrent(in_flight: int):
    config = _config()
    baseline = LRSSimulator(config).run(24)
    migrations = [
        Migration(rank=rank, begin_step=4, cutover_step=12)
        for rank in range(in_flight)
    ]
    migrated = LRSSimulator(config).run(
        24,
        migrations,
        max_in_flight=in_flight,
    )
    checks = {
        "theta bitwise": (
            migrated.theta.tobytes() == baseline.theta.tobytes()
        ),
        "momentum bitwise": (
            migrated.momentum.tobytes() == baseline.momentum.tobytes()
        ),
        "loss bitwise": migrated.loss_history == baseline.loss_history,
        "batch bitwise": migrated.batch_history == baseline.batch_history,
        "step count": migrated.step_index == baseline.step_index == 24,
        "all targets joined": migrated.active_migrations == (),
        "temp owners": [
            proof.temp_owner for proof in migrated.proofs
        ] == list(range(in_flight, 2 * in_flight)),
    }
    return baseline, migrated, checks


def main() -> int:
    results = []
    all_checks: dict[str, bool] = {}

    print("LRS M2 · W=8 · F=1/2/3 · CPU")
    for in_flight in (1, 2, 3):
        baseline, migrated, checks = _concurrent(in_flight)
        all_checks.update(
            {f"F={in_flight} {name}": ok for name, ok in checks.items()}
        )
        layout = {
            cluster.value: sum(
                1
                for value in migrated.cluster_layout.values()
                if value is cluster
            )
            for cluster in set(migrated.cluster_layout.values())
        }
        print(
            f"  F={in_flight} "
            f"theta={_digest(migrated.theta)} "
            f"temp_owners={[p.temp_owner for p in migrated.proofs]} "
            f"layout={layout} "
            f"all_ok={all(checks.values())}"
        )
        results.append(
            {
                "in_flight": in_flight,
                "checks": checks,
                "theta_digest": _digest(migrated.theta),
                "baseline_theta_digest": _digest(baseline.theta),
                "temp_owners": [
                    proof.temp_owner for proof in migrated.proofs
                ],
                "layout": layout,
                "proofs": [proof.to_dict() for proof in migrated.proofs],
            }
        )

    fp32_config = _config(dtype="float32", reduction="rank")
    fp32_baseline = LRSSimulator(fp32_config).run(24)
    fp32_migrated = LRSSimulator(fp32_config).run(
        24,
        [
            Migration(rank=rank, begin_step=4, cutover_step=12)
            for rank in range(3)
        ],
        max_in_flight=3,
    )
    fp32 = {
        "theta_max_abs_delta": float(
            np.max(np.abs(fp32_migrated.theta - fp32_baseline.theta))
        ),
        "momentum_max_abs_delta": float(
            np.max(
                np.abs(
                    fp32_migrated.momentum - fp32_baseline.momentum
                )
            )
        ),
        "loss_max_abs_delta": float(
            np.max(
                np.abs(
                    np.array(fp32_migrated.loss_history)
                    - np.array(fp32_baseline.loss_history)
                )
            )
        ),
    }
    all_checks["fp32 theta <= 1e-6"] = (
        fp32["theta_max_abs_delta"] <= 1e-6
    )
    all_checks["fp32 loss <= 1e-5"] = fp32["loss_max_abs_delta"] <= 1e-5
    print(
        f"  fp32 F=3 max_abs_delta "
        f"theta={fp32['theta_max_abs_delta']:.3e} "
        f"momentum={fp32['momentum_max_abs_delta']:.3e} "
        f"loss={fp32['loss_max_abs_delta']:.3e}"
    )

    staggered_config = _config()
    staggered = LRSSimulator(staggered_config).run(
        30,
        [
            Migration(rank=0, begin_step=4, cutover_step=10),
            Migration(rank=1, begin_step=6, cutover_step=12),
            Migration(rank=2, begin_step=8, cutover_step=14),
        ],
        max_in_flight=3,
    )
    active_counts = [
        len(staggered.assignment_history[step])
        for step in (4, 6, 8, 11, 13, 15)
    ]
    all_checks["staggered active counts"] = active_counts == [7, 6, 5, 6, 7, 8]
    print(f"  staggered active_counts={active_counts}")

    evidence = ROOT / "evidence" / "lrs" / "2026-10-22-m2" / "proof.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "schema": "shardferry.lrs.m2.proof.v1",
                "config": {
                    "world_size": 8,
                    "shard_size": 1,
                    "batch_per_rank": 4,
                    "dataset_size": 128,
                    "seed": 7,
                },
                "checks": all_checks,
                "concurrent": results,
                "fp32": fp32,
                "staggered": {
                    "active_counts": active_counts,
                    "proofs": [
                        proof.to_dict() for proof in staggered.proofs
                    ],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nevidence {evidence}")

    if not all(all_checks.values()):
        print("\nM2 FAILED")
        return 1
    print("\nM2 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
