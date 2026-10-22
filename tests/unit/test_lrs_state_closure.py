from __future__ import annotations

import random

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from shardferry.lrs.state_closure import (
    TrainingState,
    capture_state,
    restore_state,
)
from shardferry.lrs.sampler import RankSampler


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


def _params(model) -> list[torch.Tensor]:
    return [parameter.detach().clone() for parameter in model.parameters()]


def test_state_closure_restores_exact_trajectory() -> None:
    uninterrupted_model, uninterrupted_opt, uninterrupted_gen = _make()
    uninterrupted_losses = _train(
        uninterrupted_model,
        uninterrupted_opt,
        uninterrupted_gen,
        5,
    )

    model, optimizer, generator = _make()
    _train(model, optimizer, generator, 3)
    state = capture_state(
        model,
        optimizer,
        generator=generator,
        step=3,
        extra={"grad_accum": 2},
    )
    restored = TrainingState.from_bytes(state.to_bytes())
    fresh_model, fresh_opt, fresh_gen = _make()
    restore_state(
        restored,
        fresh_model,
        fresh_opt,
        generator=fresh_gen,
    )
    continued_losses = _train(
        fresh_model,
        fresh_opt,
        fresh_gen,
        2,
    )

    for expected, actual in zip(
        _params(uninterrupted_model),
        _params(fresh_model),
    ):
        assert torch.allclose(expected, actual, atol=1e-6, rtol=0)
    assert np.allclose(
        continued_losses,
        uninterrupted_losses[3:],
        atol=1e-6,
        rtol=0,
    )
    assert restored.step == 3
    assert restored.extra["grad_accum"] == 2


def test_capture_is_a_snapshot() -> None:
    model, optimizer, generator = _make()
    _train(model, optimizer, generator, 1)
    before = _params(model)
    state = capture_state(model, optimizer, step=1)
    _train(model, optimizer, generator, 1)
    captured_weight = state.model["0.weight"]
    assert torch.allclose(captured_weight, before[0], atol=0, rtol=0)
    assert not torch.allclose(
        captured_weight,
        model[0].weight,
        atol=0,
        rtol=0,
    )


def test_rng_and_generator_roundtrip() -> None:
    model, optimizer, generator = _make()
    random.seed(11)
    np.random.seed(11)
    torch.manual_seed(11)
    generator.manual_seed(17)
    first = (
        random.random(),
        float(np.random.rand()),
        torch.rand(3),
        torch.randn(3, generator=generator),
    )
    state = capture_state(model, optimizer, generator=generator)
    second = (
        random.random(),
        float(np.random.rand()),
        torch.rand(3),
        torch.randn(3, generator=generator),
    )
    restore_state(state, model, optimizer, generator=generator)
    third = (
        random.random(),
        float(np.random.rand()),
        torch.rand(3),
        torch.randn(3, generator=generator),
    )
    assert second[0] == third[0]
    assert second[1] == third[1]
    assert torch.equal(second[2], third[2])
    assert torch.equal(second[3], third[3])


def test_optimizer_state_roundtrip() -> None:
    model, optimizer, generator = _make()
    _train(model, optimizer, generator, 3)
    state = capture_state(model, optimizer, generator=generator)
    fresh_model, fresh_opt, fresh_gen = _make()
    restore_state(
        state,
        fresh_model,
        fresh_opt,
        generator=fresh_gen,
    )
    for key, expected in state.optimizer["state"].items():
        actual = fresh_opt.state_dict()["state"][key]
        for name, value in expected.items():
            if torch.is_tensor(value):
                assert torch.equal(actual[name], value)


def test_rank_sampler_and_dataloader_fields() -> None:
    model, optimizer, generator = _make()
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
    expected_next = sampler.draw()
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
    assert fresh_sampler.draw() == expected_next
    assert restored.consumed_samples == 12
    assert restored.micro_batch_size == 4
    assert restored.num_microbatches == 2
    assert restored.grad_accum_steps == 2
    assert restored.data_loader == {"epoch": 1, "offset": 12}
