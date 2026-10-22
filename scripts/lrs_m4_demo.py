#!/usr/bin/env python3
"""LRS M4 demo: source-death takeover with shadows and fail-closed paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.lrs import (  # noqa: E402
    CoverageError,
    LRSSimulator,
    Migration,
    QuorumError,
    RankPhase,
    Takeover,
    ToyConfig,
)


def _config() -> ToyConfig:
    return ToyConfig(
        world_size=8,
        shard_size=1,
        param_count=8,
        batch_per_rank=4,
        dataset_size=128,
        seed=7,
    )


def _migrations() -> list[Migration]:
    return [
        Migration(rank=rank, begin_step=4, cutover_step=10)
        for rank in range(6)
    ]


def _run_positive(
    owner_6: int,
    owner_7: int,
    baseline: LRSSimulator,
) -> tuple[LRSSimulator, dict[str, bool]]:
    sim = LRSSimulator(_config())
    sim.run(12, _migrations(), max_in_flight=6)
    sim.replicate_shadow(6, owner_6)
    sim.replicate_shadow(7, owner_7)
    sim.run(24, takeovers=[Takeover(12)])
    proof = sim.takeover_proofs[0]
    active = {
        rank
        for rank, state in sim.ranks.items()
        if state.phase is RankPhase.ACTIVE
    }
    checks = {
        "theta bitwise": sim.theta.tobytes() == baseline.theta.tobytes(),
        "momentum bitwise": (
            sim.momentum.tobytes() == baseline.momentum.tobytes()
        ),
        "loss bitwise": sim.loss_history == baseline.loss_history,
        "batch bitwise": sim.batch_history == baseline.batch_history,
        "step count": sim.step_index == baseline.step_index == 24,
        "one takeover proof": len(sim.takeover_proofs) == 1,
        "coverage complete": proof.coverage == tuple(range(8)),
        "quorum met": len(proof.quorum_votes) >= proof.quorum_required,
        "steps_lost zero": proof.steps_lost == 0,
        "global_stop zero": proof.global_stop == 0,
        "active set": active
        == (
            {0, 1, 2, 3, 4, 5}
            if proof.degraded_world_size == 6
            else {0, 1, 2, 3, 4, 5, 8, 9}
        ),
    }
    return sim, checks


def main() -> int:
    baseline = LRSSimulator(_config()).run(24)
    full_sim, full_checks = _run_positive(8, 9, baseline)
    degraded_sim, degraded_checks = _run_positive(0, 1, baseline)

    no_coverage = LRSSimulator(_config())
    no_coverage.run(12, _migrations(), max_in_flight=6)
    coverage_closed = False
    coverage_error = ""
    try:
        no_coverage.run(24, takeovers=[Takeover(12)])
    except CoverageError as exc:
        coverage_closed = True
        coverage_error = str(exc)

    no_quorum_sim = LRSSimulator(_config())
    no_quorum_sim.run(12, _migrations(), max_in_flight=6)
    no_quorum_sim.replicate_shadow(6, 8)
    no_quorum_sim.replicate_shadow(7, 9)
    quorum_closed = False
    quorum_error = ""
    try:
        no_quorum_sim.run(24, takeovers=[Takeover(12, quorum=9)])
    except QuorumError as exc:
        quorum_closed = True
        quorum_error = str(exc)

    full_proof = full_sim.takeover_proofs[0]
    degraded_proof = degraded_sim.takeover_proofs[0]
    checks = {
        "full takeover": all(full_checks.values()),
        "degraded takeover": all(degraded_checks.values()),
        "no coverage fail closed": (
            coverage_closed and not no_coverage.takeover_proofs
        ),
        "no quorum fail closed": (
            quorum_closed and not no_quorum_sim.takeover_proofs
        ),
    }

    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-m4"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / "proof.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "shardferry.lrs.m4.proof.v1",
                "checks": checks,
                "full_takeover": {
                    "checks": full_checks,
                    "proof": full_proof.to_dict(),
                },
                "degraded_takeover": {
                    "checks": degraded_checks,
                    "proof": degraded_proof.to_dict(),
                },
                "no_coverage": {
                    "fail_closed": coverage_closed,
                    "error": coverage_error,
                    "takeover_proofs": len(no_coverage.takeover_proofs),
                },
                "no_quorum": {
                    "fail_closed": quorum_closed,
                    "error": quorum_error,
                    "takeover_proofs": len(no_quorum_sim.takeover_proofs),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("LRS M4 · W=8 · source death takeover")
    print(
        f"full:     ok={checks['full takeover']} "
        f"degraded={full_proof.degraded_world_size} "
        f"quorum={len(full_proof.quorum_votes)}/{full_proof.quorum_required} "
        f"shadow={full_proof.shadow_shards}"
    )
    print(
        f"degraded: ok={checks['degraded takeover']} "
        f"degraded={degraded_proof.degraded_world_size} "
        f"quorum={len(degraded_proof.quorum_votes)}/"
        f"{degraded_proof.quorum_required} "
        f"shadow={degraded_proof.shadow_shards}"
    )
    print(
        f"no coverage: fail_closed={coverage_closed} "
        f"error={coverage_error}"
    )
    print(
        f"no quorum:   fail_closed={quorum_closed} "
        f"error={quorum_error}"
    )
    print(f"evidence {evidence}")
    if not all(checks.values()):
        print("M4 FAILED")
        return 1
    print("M4 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
