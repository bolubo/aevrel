"""M3 HTTP scenario: W=8, F in-flight, bounded lag, real HTTP transport."""

from __future__ import annotations

import hashlib
from typing import Any

from .sim import LRSSimulator, Migration
from .toy import ToyConfig
from .transport import HttpDeltaSink


def _digest(values) -> str:
    return hashlib.sha256(values.tobytes()).hexdigest()


def run_m3_scenario(
    *,
    server_url: str,
    world_size: int = 8,
    steps: int = 24,
    begin_step: int = 4,
    cutover_step: int = 12,
    in_flight: int = 2,
    max_lag: int = 2,
    step_delay: float = 0.0,
    seed: int = 7,
) -> dict[str, Any]:
    """Run a baseline and an HTTP-backed migration, returning proof data."""
    if world_size < 2:
        raise ValueError("world_size must be at least 2")
    if in_flight < 1 or in_flight >= world_size:
        raise ValueError("in_flight must be between 1 and world_size-1")
    if cutover_step <= begin_step:
        raise ValueError("cutover_step must be after begin_step")
    if steps <= cutover_step:
        raise ValueError("steps must be after cutover_step")
    config = ToyConfig(
        world_size=world_size,
        shard_size=1,
        param_count=world_size,
        batch_per_rank=4,
        dataset_size=(
            128 if world_size == 8 else max(256, world_size * 16)
        ),
        seed=seed,
    )
    baseline = LRSSimulator(config).run(steps)
    sinks: dict[int, HttpDeltaSink] = {}

    def factory(spec: Migration) -> HttpDeltaSink:
        channel = (
            f"rank-{spec.rank}-{spec.begin_step}-{spec.cutover_step}"
        )
        sink = HttpDeltaSink(
            server_url,
            channel,
            timeout=5.0,
            wait_timeout=30.0,
        )
        sinks[spec.rank] = sink
        return sink

    migrated = LRSSimulator(config, sink_factory=factory).run(
        steps,
        [
            Migration(
                rank=rank,
                begin_step=begin_step,
                cutover_step=cutover_step,
            )
            for rank in range(in_flight)
        ],
        max_in_flight=in_flight,
        max_lag=max_lag,
        step_delay=step_delay,
    )
    checks: dict[str, bool] = {
        "theta bitwise": (
            migrated.theta.tobytes() == baseline.theta.tobytes()
        ),
        "momentum bitwise": (
            migrated.momentum.tobytes() == baseline.momentum.tobytes()
        ),
        "loss bitwise": migrated.loss_history == baseline.loss_history,
        "batch bitwise": migrated.batch_history == baseline.batch_history,
        "step count": migrated.step_index == baseline.step_index == steps,
        "all targets joined": migrated.active_migrations == (),
        "bounded lag": (
            not migrated.lag_history
            or max(migrated.lag_history) <= max_lag
        ),
        "proof digests": all(
            proof.live_cutover_digest == proof.target_cutover_digest
            for proof in migrated.proofs
        ),
        "proof count": len(migrated.proofs) == in_flight,
        "in-flight at cutover": [
            proof.in_flight_at_cutover for proof in migrated.proofs
        ] == list(range(in_flight, 0, -1)),
    }
    sink_info: dict[str, dict[str, object]] = {}
    for rank, sink in sorted(sinks.items()):
        sink_info[str(rank)] = {
            "channel": sink.channel,
            "last_acked_step": sink.last_acked_step,
            "last_acked_digest": sink.last_acked_digest,
        }
        checks[f"rank {rank} acked cutover"] = (
            sink.last_acked_step >= cutover_step
        )
    return {
        "schema": "shardferry.lrs.m3.scenario.v1",
        "ok": all(checks.values()),
        "checks": checks,
        "world_size": world_size,
        "steps": steps,
        "begin_step": begin_step,
        "cutover_step": cutover_step,
        "in_flight": in_flight,
        "max_lag": max_lag,
        "step_delay": step_delay,
        "lag_history": list(migrated.lag_history),
        "lag_history_max": max(migrated.lag_history, default=0),
        "theta_digest_baseline": _digest(baseline.theta),
        "theta_digest_migrated": _digest(migrated.theta),
        "momentum_digest_baseline": _digest(baseline.momentum),
        "momentum_digest_migrated": _digest(migrated.momentum),
        "proofs": [proof.to_dict() for proof in migrated.proofs],
        "sinks": sink_info,
        "cluster_layout": {
            str(rank): cluster.value
            for rank, cluster in migrated.cluster_layout.items()
        },
    }
