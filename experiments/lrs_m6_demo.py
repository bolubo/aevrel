#!/usr/bin/env python3
"""LRS M6 demo: 8+4 / 16+8 scale, takeover, and fail-closed coverage."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aevrel.lrs import (  # noqa: E402
    CoverageError,
    LRSSimulator,
    Migration,
    Takeover,
    ToyConfig,
)


def _config(world_size: int) -> ToyConfig:
    return ToyConfig(
        world_size=world_size,
        shard_size=1,
        param_count=world_size,
        batch_per_rank=4,
        dataset_size=max(256, world_size * 16),
        seed=7,
    )


def _scale(world_size: int, in_flight: int):
    baseline = LRSSimulator(_config(world_size)).run(16)
    sim = LRSSimulator(_config(world_size))
    sim.run(
        16,
        [
            Migration(rank=rank, begin_step=4, cutover_step=10)
            for rank in range(in_flight)
        ],
        max_in_flight=in_flight,
    )
    checks = {
        "theta bitwise": sim.theta.tobytes() == baseline.theta.tobytes(),
        "momentum bitwise": (
            sim.momentum.tobytes() == baseline.momentum.tobytes()
        ),
        "loss bitwise": sim.loss_history == baseline.loss_history,
        "batch bitwise": sim.batch_history == baseline.batch_history,
        "step count": sim.step_index == 16,
        "proofs": len(sim.proofs) == in_flight,
        "steps_lost zero": all(
            proof.steps_lost == 0 for proof in sim.proofs
        ),
        "layout": {
            rank
            for rank, cluster in sim.cluster_layout.items()
            if cluster.value == "B"
        }
        == set(range(in_flight)),
    }
    return sim, checks


def main() -> int:
    w12, w12_checks = _scale(12, 4)
    w24, w24_checks = _scale(24, 8)

    takeover_baseline = LRSSimulator(_config(12)).run(16)
    takeover = LRSSimulator(_config(12))
    takeover.run(
        12,
        [
            Migration(rank=rank, begin_step=4, cutover_step=10)
            for rank in range(8)
        ],
        max_in_flight=8,
    )
    for shard_id, owner in zip(range(8, 12), range(12, 16)):
        takeover.replicate_shadow(shard_id, owner)
    takeover.run(16, takeovers=[Takeover(12)])
    takeover_proof = takeover.takeover_proofs[0]
    takeover_checks = {
        "theta bitwise": (
            takeover.theta.tobytes()
            == takeover_baseline.theta.tobytes()
        ),
        "momentum bitwise": (
            takeover.momentum.tobytes()
            == takeover_baseline.momentum.tobytes()
        ),
        "loss bitwise": (
            takeover.loss_history == takeover_baseline.loss_history
        ),
        "degraded world": takeover_proof.degraded_world_size == 12,
        "coverage": takeover_proof.coverage == tuple(range(12)),
        "shadow shards": takeover_proof.shadow_shards
        == tuple(range(8, 12)),
        "quorum": len(takeover_proof.quorum_votes)
        >= takeover_proof.quorum_required,
    }

    no_coverage = LRSSimulator(_config(12))
    no_coverage.run(
        12,
        [
            Migration(rank=rank, begin_step=4, cutover_step=10)
            for rank in range(8)
        ],
        max_in_flight=8,
    )
    coverage_closed = False
    coverage_error = ""
    try:
        no_coverage.run(16, takeovers=[Takeover(12)])
    except CoverageError as exc:
        coverage_closed = True
        coverage_error = str(exc)

    checks = {
        "8+4 scale": all(w12_checks.values()),
        "16+8 scale": all(w24_checks.values()),
        "8+4 takeover": all(takeover_checks.values()),
        "no coverage fail closed": (
            coverage_closed and not no_coverage.takeover_proofs
        ),
    }
    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-m6"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / "proof.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "aevrel.lrs.m6.proof.v1",
                "checks": checks,
                "scale_8plus4": {
                    "checks": w12_checks,
                    "proofs": [
                        proof.to_dict() for proof in w12.proofs
                    ],
                },
                "scale_16plus8": {
                    "checks": w24_checks,
                    "proofs": [
                        proof.to_dict() for proof in w24.proofs
                    ],
                },
                "takeover_8plus4": {
                    "checks": takeover_checks,
                    "proof": takeover_proof.to_dict(),
                },
                "no_coverage": {
                    "fail_closed": coverage_closed,
                    "error": coverage_error,
                    "takeover_proofs": len(
                        no_coverage.takeover_proofs
                    ),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("LRS M6 · scale and fault matrix · CPU")
    print(
        f"8+4:  ok={checks['8+4 scale']} "
        f"proofs={len(w12.proofs)}"
    )
    print(
        f"16+8: ok={checks['16+8 scale']} "
        f"proofs={len(w24.proofs)}"
    )
    print(
        f"takeover 8+4: ok={checks['8+4 takeover']} "
        f"degraded={takeover_proof.degraded_world_size} "
        f"shadow={takeover_proof.shadow_shards}"
    )
    print(
        f"no coverage: fail_closed={coverage_closed} "
        f"error={coverage_error}"
    )
    print(f"evidence {evidence}")
    if not all(checks.values()):
        print("M6 FAILED")
        return 1
    print("M6 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
