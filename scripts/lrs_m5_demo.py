#!/usr/bin/env python3
"""LRS M5 demo: TP rank migration and PP stage remapping."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from aevrel.lrs import RankPhase  # noqa: E402
from aevrel.lrs.tp_pp import (  # noqa: E402
    TPPPBaseline,
    TPPPConfig,
    TPPPSimulator,
)


def _diff(left, right) -> float:
    return float(np.max(np.abs(np.asarray(left) - np.asarray(right))))


def main() -> int:
    config = TPPPConfig()
    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-m5"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    proof_path = evidence_dir / "proof.json"

    # TP rank migration: rank 1 (TP=1, PP=0) -> B.
    tp_sim = TPPPSimulator(config)
    tp_baseline = TPPPBaseline(
        config,
        tp_sim.x,
        tp_sim.y,
        tp_sim.w1_init,
        tp_sim.w2_init,
    ).run(8)
    tp_sim.run(2)
    tp_sim.begin_tp_migration(1, cutover_step=5)
    tp_sim.run(8)
    tp_w1, tp_w2 = tp_sim.reconstruct()
    tp_checks = {
        "w1 matches": np.allclose(tp_w1, tp_baseline.w1, atol=1e-9),
        "w2 matches": np.allclose(tp_w2, tp_baseline.w2, atol=1e-9),
        "loss matches": np.allclose(
            tp_sim.loss_history,
            tp_baseline.loss_history,
            atol=1e-12,
        ),
        "target active on B": (
            tp_sim.ranks[1].phase is RankPhase.ACTIVE
            and tp_sim.ranks[1].cluster.value == "B"
        ),
        "tp epoch bumped": tp_sim.tp_epoch[0] == 1,
        "post-cutover owners": all(
            record.owners == (0, 1)
            for record in tp_sim.collective_log
            if record.pp == 0 and record.tp_epoch == 1
        ),
    }
    tp_post = [
        asdict(record)
        for record in tp_sim.collective_log
        if record.pp == 0 and record.tp_epoch == 1
    ]

    # PP stage remap: stage 1 (ranks 2/3) -> B standbys 4/5.
    pp_sim = TPPPSimulator(config)
    pp_baseline = TPPPBaseline(
        config,
        pp_sim.x,
        pp_sim.y,
        pp_sim.w1_init,
        pp_sim.w2_init,
    ).run(8)
    pp_sim.run(2)
    pp_sim.shadow_pp_stage(1)
    pp_sim.cutover_pp_stage(1)
    pp_sim.run(8)
    pp_w1, pp_w2 = pp_sim.reconstruct()
    pp_checks = {
        "w1 matches": np.allclose(pp_w1, pp_baseline.w1, atol=1e-9),
        "w2 matches": np.allclose(pp_w2, pp_baseline.w2, atol=1e-9),
        "loss matches": np.allclose(
            pp_sim.loss_history,
            pp_baseline.loss_history,
            atol=1e-12,
        ),
        "stage owners moved": (
            pp_sim.ranks[2].phase is RankPhase.DEAD
            and pp_sim.ranks[3].phase is RankPhase.DEAD
            and pp_sim.ranks[4].phase is RankPhase.ACTIVE
            and pp_sim.ranks[5].phase is RankPhase.ACTIVE
        ),
        "pp/tp epoch bumped": (
            pp_sim.pp_epoch[1] == 1 and pp_sim.tp_epoch[1] == 1
        ),
        "post-cutover owners": all(
            record.owners == (4, 5)
            for record in pp_sim.collective_log
            if record.pp == 1 and record.pp_epoch == 1
        ),
        "pipeline owners": all(
            record.to_owners == (4, 5)
            for record in pp_sim.pipeline_log
            if record.to_pp == 1 and record.pp_epoch == 1
        ),
    }
    pp_post = [
        asdict(record)
        for record in pp_sim.collective_log
        if record.pp == 1 and record.pp_epoch == 1
    ]
    pp_pipeline = [
        asdict(record)
        for record in pp_sim.pipeline_log
        if record.to_pp == 1 and record.pp_epoch == 1
    ]

    checks = {
        "tp rank migration": all(tp_checks.values()),
        "pp stage remap": all(pp_checks.values()),
    }
    proof_path.write_text(
        json.dumps(
            {
                "schema": "aevrel.lrs.m5.proof.v1",
                "checks": checks,
                "note": (
                    "CPU semantic TP/PP toy; real 4-GPU NCCL run "
                    "requires a rental"
                ),
                "tp_rank_migration": {
                    "checks": tp_checks,
                    "w1_max_abs_delta": _diff(tp_w1, tp_baseline.w1),
                    "w2_max_abs_delta": _diff(tp_w2, tp_baseline.w2),
                    "proof": tp_sim.tp_proofs[0].to_dict(),
                    "collective_after": tp_post,
                },
                "pp_stage_remap": {
                    "checks": pp_checks,
                    "w1_max_abs_delta": _diff(pp_w1, pp_baseline.w1),
                    "w2_max_abs_delta": _diff(pp_w2, pp_baseline.w2),
                    "proof": pp_sim.pp_proofs[0].to_dict(),
                    "collective_after": pp_post,
                    "pipeline_after": pp_pipeline,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("LRS M5 · TP=2 / PP=2 · CPU semantic toy")
    print(
        f"tp rank 1: ok={checks['tp rank migration']} "
        f"w1_delta={_diff(tp_w1, tp_baseline.w1):.3e} "
        f"w2_delta={_diff(tp_w2, tp_baseline.w2):.3e} "
        f"tp_epoch={tp_sim.tp_epoch[0]} "
        f"owners={tp_sim.tp_proofs[0].collective_owners_after}"
    )
    print(
        f"pp stage 1: ok={checks['pp stage remap']} "
        f"w1_delta={_diff(pp_w1, pp_baseline.w1):.3e} "
        f"w2_delta={_diff(pp_w2, pp_baseline.w2):.3e} "
        f"owners={pp_sim.pp_proofs[0].new_owners} "
        f"pp_epoch={pp_sim.pp_proofs[0].pp_epoch}"
    )
    print(f"evidence {proof_path}")
    if not all(checks.values()):
        print("M5 FAILED")
        return 1
    print("M5 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
