"""Model/optimizer offload and onload helpers for state handoff."""

from __future__ import annotations

from typing import Any

import torch


def _move_optimizer_state(
    optimizer: torch.optim.Optimizer,
    device: torch.device,
) -> None:
    for state in optimizer.state.values():
        for key, value in list(state.items()):
            if isinstance(value, torch.Tensor):
                state[key] = value.to(device)


def offload_model_and_optimizer(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
) -> torch.device:
    """Move model/optimizer state to host memory and return the old device."""
    devices = {
        parameter.device
        for parameter in model.parameters()
    } | {
        buffer.device
        for buffer in model.buffers()
    }
    device = next(iter(devices), torch.device("cpu"))
    model.to("cpu")
    _move_optimizer_state(optimizer, torch.device("cpu"))
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    return device


def onload_model_and_optimizer(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    device: torch.device,
) -> None:
    model.to(device)
    _move_optimizer_state(optimizer, device)
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
