"""Component-granular providers for the LRS training-state closure.

Each provider serves exactly one resume component (``training.model``,
``training.optimizer``, ...) captured from a single frozen snapshot. They
are capture-only: restoring a component set must go through
:class:`aevrel.lrs.resume_adapter.TrainingResumeAdapter`, which rebuilds the
training state and restores it once, so components can never be applied
out of order or partially.
"""

from __future__ import annotations

import numpy as np
import torch

from ..state import StateProviderError


COMPONENT_FORMAT = "torch-training-component-v1"


def _equal(left, right) -> bool:
    if torch.is_tensor(left) or torch.is_tensor(right):
        return (
            torch.is_tensor(left)
            and torch.is_tensor(right)
            and torch.equal(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return (
            left.keys() == right.keys()
            and all(_equal(left[key], right[key]) for key in left)
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (
            len(left) == len(right)
            and all(_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        return (
            isinstance(left, np.ndarray)
            and isinstance(right, np.ndarray)
            and np.array_equal(left, right)
        )
    return left == right


def training_states_equal(left, right) -> bool:
    """Bitwise comparison of two training states."""
    return (
        _equal(left.step, right.step)
        and _equal(left.model, right.model)
        and _equal(left.optimizer, right.optimizer)
        and _equal(left.rng.python, right.rng.python)
        and _equal(left.rng.numpy, right.rng.numpy)
        and _equal(left.rng.torch, right.rng.torch)
        and _equal(left.rng.cuda, right.rng.cuda)
        and _equal(left.generator, right.generator)
        and _equal(left.scaler, right.scaler)
        and _equal(left.extra, right.extra)
        and _equal(left.consumed_samples, right.consumed_samples)
        and _equal(left.micro_batch_size, right.micro_batch_size)
        and _equal(left.num_microbatches, right.num_microbatches)
        and _equal(left.grad_accum_steps, right.grad_accum_steps)
        and _equal(left.data_loader, right.data_loader)
        and _equal(left.rank_sampler, right.rank_sampler)
    )


class TrainingComponentProvider:
    """Serve one component of a frozen snapshot as a bundle entry."""

    def __init__(self, kind: str, payload: bytes, *, prefix: str = "training") -> None:
        if not kind:
            raise ValueError("component kind is required")
        self.kind = kind
        self.name = f"{prefix}.{kind}"
        self.format = COMPONENT_FORMAT
        self._payload = bytes(payload)

    def capture(self) -> bytes:
        return self._payload

    def restore(self, data: bytes) -> None:
        raise StateProviderError(
            f"component provider {self.name} is capture-only; "
            "restore through TrainingResumeAdapter.restore_exact"
        )

    def __repr__(self) -> str:
        return f"TrainingComponentProvider({self.name!r}, {len(self._payload)} bytes)"
