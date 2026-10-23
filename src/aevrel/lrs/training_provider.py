"""StateProvider adapter for the full TrainingState closure."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import torch

from ..state import StateProviderError
from .state_closure import TrainingState


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


def training_states_equal(left: TrainingState, right: TrainingState) -> bool:
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
    )


class TrainingStateProvider:
    """Capture/restore a TrainingState into the generic state bundle."""

    name = "training.state"
    format = "torch-training-state-v1"

    def __init__(
        self,
        capture: Callable[[], TrainingState],
        restore: Callable[[TrainingState], None],
    ) -> None:
        self._capture = capture
        self._restore = restore

    def capture(self) -> bytes:
        return self._capture().to_bytes()

    def restore(self, data: bytes) -> None:
        self._restore(TrainingState.from_bytes(bytes(data)))

    def verify(self, data: bytes) -> None:
        expected = TrainingState.from_bytes(bytes(data))
        actual = self._capture()
        if not training_states_equal(expected, actual):
            raise StateProviderError(
                "restored training state does not match its entry"
            )
