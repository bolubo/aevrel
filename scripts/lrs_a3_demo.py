#!/usr/bin/env python3
"""A3.2 demo: state closure, RANK sampler, RNG reshard, ring, offload."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from torch import nn  # noqa: E402

from shardferry.lrs.memory import (  # noqa: E402
    offload_model_and_optimizer,
    onload_model_and_optimizer,
)
from shardferry.lrs.rng_reshard import (  # noqa: E402
    RNGStream,
    RNGStreamKey,
    RNGStreamRegistry,
)
from shardferry.lrs.sampler import RankSampler  # noqa: E402
from shardferry.lrs.snapshot_ring import (  # noqa: E402
    NeighborBackup,
    SnapshotRing,
)
from shardferry.lrs.state_closure import (  # noqa: E402
    TrainingState,
    capture_state,
    restore_state,
)


def _make():
    torch.manual_seed(123)
    model = nn.Sequential(
        nn.Linear(4, 8),
        nn.ReLU(),
        nn.Dropout(0.1),
        nn.Linear(8, 1),
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    generator = torch.Generator().manual_seed(7)
    return model, optimizer, generator


def _train(model, optimizer, generator, steps):
    losses = []
    model.train()
    for _ in range(steps):
        inputs = torch.randn(8, 4, generator=generator)
        targets = torch.randn(8, 1, generator=generator)
        optimizer.zero_grad()
        loss = F.mse_loss(model(inputs), targets)
        loss.backward()
        optimizer.step()
        losses.append(loss.detach().item())
    return losses


def main() -> int:
    baseline_model, baseline_opt, baseline_gen = _make()
    baseline_losses = _train(
        baseline_model,
        baseline_opt,
        baseline_gen,
        5,
    )
    model, optimizer, generator = _make()
    _train(model, optimizer, generator, 3)
    sampler = RankSampler(
        rank=1,
        seed=17,
        start=8,
        end=16,
        batch_size=4,
    )
    sampler.draw()
    state = capture_state(
        model,
        optimizer,
        generator=generator,
        rank_sampler=sampler.capture(),
        consumed_samples=12,
        micro_batch_size=4,
        num_microbatches=2,
        grad_accum_steps=2,
        data_loader={"epoch": 1, "offset": 12},
    )
    expected_sampler = sampler.draw()
    restored = TrainingState.from_bytes(state.to_bytes())
    fresh_model, fresh_opt, fresh_gen = _make()
    fresh_sampler = RankSampler(
        rank=1,
        seed=0,
        start=8,
        end=16,
        batch_size=4,
    )
    restore_state(
        restored,
        fresh_model,
        fresh_opt,
        generator=fresh_gen,
        rank_sampler=fresh_sampler,
    )
    sampler_ok = fresh_sampler.draw() == expected_sampler
    continued_losses = _train(
        fresh_model,
        fresh_opt,
        fresh_gen,
        2,
    )
    param_diff = max(
        (
            left.detach() - right.detach()
        ).abs().max().item()
        for left, right in zip(
            baseline_model.parameters(),
            fresh_model.parameters(),
        )
    )
    loss_diff = max(
        abs(left - right)
        for left, right in zip(
            continued_losses,
            baseline_losses[3:],
        )
    )

    ring = SnapshotRing(capacity=2)
    for step in range(3):
        ring.push(step, state)
    ring_ok = ring.steps() == (1, 2) and ring.latest().step == 2
    backup = NeighborBackup()
    backup.backup(source_rank=1, target_rank=2, state=state)
    _, backup_state = backup.restore(2)
    backup_ok = backup_state.step == state.step

    registry = RNGStreamRegistry()
    data = RNGStream(RNGStreamKey("data", 0), seed=1)
    registry.add(data)
    data.draw(2)
    moved = registry.reshard(
        {
            RNGStreamKey("data", 0): RNGStreamKey("data", 2),
        }
    )
    rng_ok = (
        moved.get(RNGStreamKey("data", 2)).draw(2)
        == data.draw(2)
    )

    device = offload_model_and_optimizer(fresh_model, fresh_opt)
    offload_ok = all(
        parameter.device.type == "cpu"
        for parameter in fresh_model.parameters()
    )
    onload_model_and_optimizer(fresh_model, fresh_opt, device)

    checks = {
        "sampler continues": sampler_ok,
        "parameter trajectory": param_diff <= 1e-6,
        "loss trajectory": loss_diff <= 1e-6,
        "snapshot ring": ring_ok,
        "neighbor backup": backup_ok,
        "rng reshard": rng_ok,
        "offload": offload_ok,
        "dataloader fields": (
            restored.consumed_samples == 12
            and restored.data_loader == {"epoch": 1, "offset": 12}
        ),
    }
    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-a3"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / "proof.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "shardferry.lrs.a3.proof.v1",
                "checks": checks,
                "parameter_max_abs_delta": param_diff,
                "loss_max_abs_delta": loss_diff,
                "ring_steps": list(ring.steps()),
                "consumed_samples": restored.consumed_samples,
                "data_loader": restored.data_loader,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("LRS A3.2 · state closure + RANK sampler + RNG reshard + ring")
    print(
        f"sampler={sampler_ok} param_diff={param_diff:.3e} "
        f"loss_diff={loss_diff:.3e}"
    )
    print(
        f"ring={list(ring.steps())} neighbor={backup_ok} "
        f"rng_reshard={rng_ok} offload={offload_ok}"
    )
    print(f"evidence {evidence}")
    if not all(checks.values()):
        print("A3.2 FAILED")
        return 1
    print("A3.2 PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
