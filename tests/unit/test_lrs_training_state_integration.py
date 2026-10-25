from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from aevrel.lrs.resume_adapter import TrainingResumeAdapter
from aevrel.lrs.sampler import RankSampler
from aevrel.lrs.state_closure import (
    capture_state as capture_training,
    restore_state as restore_training,
)
from aevrel.state import (
    RESUME_STATE_COMPLETE,
    StateBundle,
    capture_state as capture_bundle,
    classify_resume_state,
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
    sampler = RankSampler(
        rank=0,
        seed=99,
        start=0,
        end=64,
        batch_size=8,
    )
    return model, optimizer, generator, sampler


def _train(model, optimizer, sampler, x, y, steps):
    losses = []
    model.train()
    for _ in range(steps):
        ids = sampler.draw()
        inputs = x[list(ids)]
        targets = y[list(ids)]
        optimizer.zero_grad()
        loss = F.mse_loss(model(inputs), targets)
        loss.backward()
        optimizer.step()
        losses.append(loss.detach().item())
    return losses


def _adapter(model, optimizer, generator, sampler, step):
    def capture():
        return capture_training(
            model,
            optimizer,
            generator=generator,
            step=step[0],
            consumed_samples=step[0] * 8,
            data_loader={"position": step[0]},
            rank_sampler=sampler.capture(),
        )

    def apply(state):
        restore_training(
            state,
            model,
            optimizer,
            generator=generator,
        )
        sampler.restore(state.rank_sampler)
        step[0] = state.step

    return TrainingResumeAdapter(capture, apply)


def test_component_bundle_restores_trajectory_and_sampler(
    tmp_path: Path,
) -> None:
    torch.manual_seed(5)
    x = torch.randn(64, 4)
    y = torch.randn(64, 1)

    uninterrupted_model, uninterrupted_opt, _, uninterrupted_sampler = _make()
    uninterrupted_losses = _train(
        uninterrupted_model,
        uninterrupted_opt,
        uninterrupted_sampler,
        x,
        y,
        5,
    )
    uninterrupted_next = uninterrupted_sampler.draw()
    uninterrupted_params = [
        parameter.detach().clone()
        for parameter in uninterrupted_model.parameters()
    ]

    source_model, source_opt, source_gen, source_sampler = _make()
    _train(source_model, source_opt, source_sampler, x, y, 3)
    source_step = [3]
    source = _adapter(
        source_model, source_opt, source_gen, source_sampler, source_step
    )
    root = tmp_path / "state"
    bundle = capture_bundle(
        root=root,
        checkpoint_id="job-1",
        version="step-3",
        rank=0,
        source_world_size=1,
        providers=list(source.providers()),
    )
    report = classify_resume_state(
        source.manifest(
            checkpoint_uid="job-1",
            checkpoint_version="step-3",
            metadata_digest="sha256:" + "e" * 64,
            world_size=1,
            placements=("rank-0",),
        ).to_dict()
    )
    assert report.status == RESUME_STATE_COMPLETE

    target_model, target_opt, target_gen, target_sampler = _make()
    target_step = [3]
    target = _adapter(
        target_model, target_opt, target_gen, target_sampler, target_step
    )
    restored = StateBundle.read(root)
    target.restore_exact(restored)
    target.verify_restored(restored)

    continued_losses = _train(
        target_model,
        target_opt,
        target_sampler,
        x,
        y,
        2,
    )
    target_params = [
        parameter.detach().clone()
        for parameter in target_model.parameters()
    ]

    for expected, actual in zip(
        uninterrupted_params,
        target_params,
    ):
        assert torch.allclose(expected, actual, atol=1e-6, rtol=0)
    assert np.allclose(
        continued_losses,
        uninterrupted_losses[3:],
        atol=1e-6,
        rtol=0,
    )
    assert target_sampler.draw() == uninterrupted_next
    assert target_step[0] == 3
    assert bundle.manifest.complete is True
