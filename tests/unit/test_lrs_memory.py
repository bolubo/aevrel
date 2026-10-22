from __future__ import annotations

import torch
from torch import nn

from shardferry.lrs.memory import (
    offload_model_and_optimizer,
    onload_model_and_optimizer,
)


def test_offload_onload_cpu_roundtrip() -> None:
    model = nn.Linear(4, 2)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    inputs = torch.randn(3, 4)
    loss = model(inputs).sum()
    loss.backward()
    optimizer.step()

    before = {
        name: parameter.detach().clone()
        for name, parameter in model.named_parameters()
    }
    before_optimizer = {
        parameter: {
            key: value.detach().clone()
            if torch.is_tensor(value)
            else value
            for key, value in state.items()
        }
        for parameter, state in optimizer.state.items()
    }
    device = offload_model_and_optimizer(model, optimizer)
    assert all(
        parameter.device.type == "cpu"
        for parameter in model.parameters()
    )
    assert all(
        value.device.type == "cpu"
        for state in optimizer.state.values()
        for value in state.values()
        if torch.is_tensor(value)
    )
    onload_model_and_optimizer(model, optimizer, device)
    for name, parameter in model.named_parameters():
        assert torch.equal(parameter, before[name])
    for parameter, state in optimizer.state.items():
        for key, value in state.items():
            if torch.is_tensor(value):
                assert torch.equal(
                    value,
                    before_optimizer[parameter][key],
                )
