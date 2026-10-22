#!/usr/bin/env python3
"""LRS RANK state demo: per-rank RNG survives migration, wrong RNG fails."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.lrs import (  # noqa: E402
    DigestMismatchError,
    LRSSimulator,
    Migration,
    ShardBlob,
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


def _state_digest(sampler) -> str:
    return hashlib.sha256(sampler.capture()).hexdigest()


def main() -> int:
    config = _config()
    baseline = LRSSimulator(config).run(16)
    sim = LRSSimulator(config).run(
        16,
        [Migration(rank=1, begin_step=4, cutover_step=10)],
    )
    proof = sim.proofs[0]
    checks = {
        "theta bitwise": sim.theta.tobytes() == baseline.theta.tobytes(),
        "momentum bitwise": (
            sim.momentum.tobytes() == baseline.momentum.tobytes()
        ),
        "loss bitwise": sim.loss_history == baseline.loss_history,
        "batch bitwise": sim.batch_history == baseline.batch_history,
        "rank1 rng restored": (
            sim.samplers[1].capture()
            == baseline.samplers[1].capture()
        ),
        "rank0 rng isolated": (
            sim.samplers[0].capture()
            == baseline.samplers[0].capture()
        ),
        "rng in proof digest": (
            proof.live_cutover_digest
            == proof.target_cutover_digest
        ),
    }

    negative = LRSSimulator(config)
    negative.run(4)
    spec = Migration(rank=1, begin_step=4, cutover_step=10)
    negative._begin_migration(spec)
    for step in range(4, 11):
        negative._run_step(step)
    runtime = negative._runtimes[1]
    original = runtime.transfer.acked_blob(10)
    runtime.transfer._blobs[10] = ShardBlob.capture(
        step=10,
        shard_id=1,
        epoch=original.epoch,
        theta=original.theta,
        momentum=original.momentum,
        aux=b"wrong-rng-state",
    )
    fail_closed = False
    error = ""
    try:
        negative._cutover(spec)
    except DigestMismatchError as exc:
        fail_closed = True
        error = str(exc)
    checks["wrong rng fail closed"] = (
        fail_closed and not negative.proofs
    )

    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-rank"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / "proof.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "shardferry.lrs.rank-state.v1",
                "checks": checks,
                "sampler_digests": {
                    "baseline_rank1": _state_digest(
                        baseline.samplers[1]
                    ),
                    "migrated_rank1": _state_digest(
                        sim.samplers[1]
                    ),
                    "baseline_rank0": _state_digest(
                        baseline.samplers[0]
                    ),
                    "migrated_rank0": _state_digest(
                        sim.samplers[0]
                    ),
                },
                "migration_proof": proof.to_dict(),
                "wrong_rng": {
                    "fail_closed": fail_closed,
                    "error": error,
                    "proofs": len(negative.proofs),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("LRS RANK state · per-rank sampler/RNG")
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    print(f"evidence {evidence}")
    if not all(checks.values()):
        print("RANK STATE FAILED")
        return 1
    print("RANK STATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
