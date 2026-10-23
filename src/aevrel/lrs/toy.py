"""A deterministic CPU toy model for the LRS M1 semantic proof."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ToyConfig:
    param_count: int = 8
    world_size: int = 4
    shard_size: int = 2
    batch_per_rank: int = 4
    dataset_size: int = 128
    seed: int = 7
    learning_rate: float = 0.02
    momentum: float = 0.9
    dtype: str = "float64"
    reduction: str = "canonical"

    def __post_init__(self) -> None:
        if self.param_count != self.world_size * self.shard_size:
            raise ValueError("param_count must equal world_size * shard_size")
        if self.world_size < 2:
            raise ValueError("world_size must be at least 2")
        if self.shard_size < 1:
            raise ValueError("shard_size must be positive")
        if self.batch_per_rank < 1:
            raise ValueError("batch_per_rank must be positive")
        if self.dataset_size < self.world_size * self.batch_per_rank:
            raise ValueError("dataset is too small for one global batch")
        if (
            self.dataset_size // self.world_size
            < self.batch_per_rank
        ):
            raise ValueError("each rank needs enough samples")
        if self.dtype not in ("float32", "float64"):
            raise ValueError("dtype must be float32 or float64")
        if self.reduction not in ("canonical", "rank"):
            raise ValueError("reduction must be canonical or rank")


class ToyDataset:
    """Fixed linear-regression dataset shared by baseline and migration."""

    def __init__(self, config: ToyConfig) -> None:
        rng = np.random.default_rng(config.seed)
        self.x = rng.normal(size=(config.dataset_size, config.param_count))
        true_theta = rng.normal(size=config.param_count)
        noise = rng.normal(scale=0.01, size=config.dataset_size)
        self.y = self.x @ true_theta + noise

    def loss(self, theta: np.ndarray) -> float:
        residual = self.x @ theta - self.y
        return float(np.mean(residual * residual))


class BatchStream:
    """Deterministic global batches; step -> tuple of sample ids."""

    def __init__(self, config: ToyConfig) -> None:
        self._config = config
        self._rng = np.random.default_rng(config.seed + 1)
        self._batches: dict[int, tuple[int, ...]] = {}

    def batch(self, step: int) -> tuple[int, ...]:
        if step < 0:
            raise ValueError("step must be non-negative")
        if step not in self._batches:
            if step != len(self._batches):
                raise ValueError("batches must be requested in step order")
            size = self._config.world_size * self._config.batch_per_rank
            chosen = self._rng.choice(
                self._config.dataset_size,
                size=size,
                replace=False,
            )
            self._batches[step] = tuple(int(value) for value in chosen)
        return self._batches[step]

    def history(self) -> tuple[tuple[int, ...], ...]:
        return tuple(
            self._batches[step] for step in range(len(self._batches))
        )


def per_sample_gradients(
    dataset: ToyDataset,
    theta: np.ndarray,
    sample_ids: tuple[int, ...],
) -> dict[int, np.ndarray]:
    """MSE gradient for each sample, w.r.t. the full parameter vector."""
    result: dict[int, np.ndarray] = {}
    batch_size = len(sample_ids)
    for sample_id in sample_ids:
        x = dataset.x[sample_id]
        residual = float(x @ theta - dataset.y[sample_id])
        result[sample_id] = (2.0 * residual / batch_size) * x
    return result


def canonical_gradient(
    dataset: ToyDataset,
    theta: np.ndarray,
    sample_ids: tuple[int, ...],
) -> np.ndarray:
    """Order-stable gradient over a fixed global batch.

    M1 isolates protocol semantics from floating-point reduction order by
    reducing per-sample gradients in sample-id order with ``math.fsum``.
    M2/M3 will replace this with real per-rank accumulation and measure the
    resulting tolerance.
    """
    gradients = per_sample_gradients(dataset, theta, sample_ids)
    ordered = sorted(sample_ids)
    return np.array(
        [
            math.fsum(
                float(gradients[sample_id][index])
                for sample_id in ordered
            )
            for index in range(theta.size)
        ],
        dtype=np.float64,
    )
