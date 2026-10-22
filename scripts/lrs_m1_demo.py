#!/usr/bin/env python3
"""Run the LRS M1 baseline and 2+2 migration, then print the proof."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.lrs import LRSSimulator, Migration, ToyConfig  # noqa: E402


def _digest(values) -> str:
    return hashlib.sha256(values.tobytes()).hexdigest()[:16]


def main() -> int:
    config = ToyConfig()
    baseline = LRSSimulator(config).run(24)
    migrated = LRSSimulator(config).run(
        24,
        [
            Migration(rank=0, begin_step=4, cutover_step=10),
            Migration(rank=1, begin_step=12, cutover_step=18),
        ],
    )

    checks = {
        "theta bitwise": migrated.theta.tobytes() == baseline.theta.tobytes(),
        "momentum bitwise": (
            migrated.momentum.tobytes() == baseline.momentum.tobytes()
        ),
        "loss history": migrated.loss_history == baseline.loss_history,
        "batch history": migrated.batch_history == baseline.batch_history,
        "step count": migrated.step_index == baseline.step_index == 24,
        "cluster layout 2+2": (
            {rank: cluster.value for rank, cluster in migrated.cluster_layout.items()}
            == {0: "B", 1: "B", 2: "A", 3: "A"}
        ),
    }

    print("LRS M1 · W=4 · F=1 · CPU")
    print(f"theta   baseline={_digest(baseline.theta)} "
          f"migrated={_digest(migrated.theta)}")
    print(f"momentum baseline={_digest(baseline.momentum)} "
          f"migrated={_digest(migrated.momentum)}")
    print(f"layout  {migrated.cluster_layout}")
    print()
    print("checks")
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    print()
    print("proofs")
    for proof in migrated.proofs:
        print(
            f"  rank={proof.rank} shard={proof.shard_id} "
            f"steps={proof.begin_step}..{proof.cutover_step} "
            f"temp_owner={proof.temp_owner} "
            f"epoch={proof.source_epoch}->{proof.cutover_epoch}->{proof.target_epoch} "
            f"steps_lost={proof.steps_lost} global_stop={proof.global_stop} "
            f"digest_match={proof.live_cutover_digest == proof.target_cutover_digest}"
        )

    evidence = ROOT / "evidence" / "lrs" / "2026-10-22-m1" / "proof.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "schema": "shardferry.lrs.m1.proof.v1",
                "config": {
                    "world_size": config.world_size,
                    "shard_size": config.shard_size,
                    "batch_per_rank": config.batch_per_rank,
                    "dtype": config.dtype,
                    "reduction": config.reduction,
                    "seed": config.seed,
                },
                "checks": checks,
                "theta_digest_baseline": _digest(baseline.theta),
                "theta_digest_migrated": _digest(migrated.theta),
                "momentum_digest_baseline": _digest(baseline.momentum),
                "momentum_digest_migrated": _digest(migrated.momentum),
                "proofs": [proof.to_dict() for proof in migrated.proofs],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nevidence {evidence}")

    if not all(checks.values()):
        print("\nM1 FAILED")
        return 1
    print("\nM1 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
