"""Training-state closure for a stable-step handoff.

This is a CPU/GPU-agnostic snapshot of the parts a training loop must
preserve to continue the exact same trajectory: model parameters, optimizer
state, RNG states, generator state, optional AMP scaler, and extra counters.
"""

from __future__ import annotations

import io
import pickle
import random
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch


@dataclass(frozen=True)
class RNGState:
    python: bytes
    numpy: bytes
    torch: bytes
    cuda: tuple[bytes, ...] | None = None


@dataclass(frozen=True)
class TrainingState:
    step: int
    model: dict[str, Any]
    optimizer: dict[str, Any]
    rng: RNGState
    generator: bytes | None
    scaler: dict[str, Any] | None
    extra: dict[str, Any]
    consumed_samples: int = 0
    micro_batch_size: int | None = None
    num_microbatches: int | None = None
    grad_accum_steps: int = 1
    data_loader: dict[str, Any] | None = None
    rank_sampler: bytes | None = None

    def to_bytes(self) -> bytes:
        buffer = io.BytesIO()
        torch.save(
            {
                "step": self.step,
                "model": self.model,
                "optimizer": self.optimizer,
                "rng": {
                    "python": self.rng.python,
                    "numpy": self.rng.numpy,
                    "torch": self.rng.torch,
                    "cuda": self.rng.cuda,
                },
                "generator": self.generator,
                "scaler": self.scaler,
                "consumed_samples": self.consumed_samples,
                "micro_batch_size": self.micro_batch_size,
                "num_microbatches": self.num_microbatches,
                "grad_accum_steps": self.grad_accum_steps,
                "data_loader": self.data_loader,
                "rank_sampler": self.rank_sampler,
                "extra": self.extra,
            },
            buffer,
        )
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, payload: bytes) -> "TrainingState":
        data = torch.load(
            io.BytesIO(payload),
            map_location="cpu",
            weights_only=False,
        )
        rng = data["rng"]
        cuda = rng.get("cuda")
        return cls(
            step=int(data["step"]),
            model=data["model"],
            optimizer=data["optimizer"],
            rng=RNGState(
                python=bytes(rng["python"]),
                numpy=bytes(rng["numpy"]),
                torch=bytes(rng["torch"]),
                cuda=(
                    tuple(bytes(item) for item in cuda)
                    if cuda is not None
                    else None
                ),
            ),
            generator=data.get("generator"),
            scaler=data.get("scaler"),
            consumed_samples=int(data.get("consumed_samples", 0)),
            micro_batch_size=data.get("micro_batch_size"),
            num_microbatches=data.get("num_microbatches"),
            grad_accum_steps=int(data.get("grad_accum_steps", 1)),
            data_loader=data.get("data_loader"),
            rank_sampler=data.get("rank_sampler"),
            extra=dict(data.get("extra", {})),
        )


def _clone(value: Any) -> Any:
    if isinstance(value, torch.Tensor):
        return value.detach().clone()
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone(item) for item in value)
    if isinstance(value, np.ndarray):
        return value.copy()
    return value


def _tensor_bytes(value: torch.Tensor) -> bytes:
    return value.detach().cpu().numpy().tobytes()


def _capture_rng() -> RNGState:
    cuda = None
    if torch.cuda.is_available():
        cuda = tuple(
            _tensor_bytes(state)
            for state in torch.cuda.get_rng_state_all()
        )
    return RNGState(
        python=pickle.dumps(random.getstate()),
        numpy=pickle.dumps(np.random.get_state()),
        torch=_tensor_bytes(torch.get_rng_state()),
        cuda=cuda,
    )


def _capture_generator(
    generator: torch.Generator | None,
) -> bytes | None:
    if generator is None:
        return None
    return _tensor_bytes(generator.get_state())


def capture_state(
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    *,
    scaler: Any | None = None,
    generator: torch.Generator | None = None,
    step: int = 0,
    extra: dict[str, Any] | None = None,
    consumed_samples: int = 0,
    micro_batch_size: int | None = None,
    num_microbatches: int | None = None,
    grad_accum_steps: int = 1,
    data_loader: dict[str, Any] | None = None,
    rank_sampler: bytes | None = None,
) -> TrainingState:
    return TrainingState(
        step=int(step),
        model=_clone(model.state_dict()),
        optimizer=_clone(optimizer.state_dict()),
        rng=_capture_rng(),
        generator=_capture_generator(generator),
        scaler=(
            _clone(scaler.state_dict())
            if scaler is not None
            else None
        ),
        consumed_samples=int(consumed_samples),
        micro_batch_size=micro_batch_size,
        num_microbatches=num_microbatches,
        grad_accum_steps=int(grad_accum_steps),
        data_loader=dict(data_loader) if data_loader is not None else None,
        rank_sampler=(
            bytes(rank_sampler)
            if rank_sampler is not None
            else None
        ),
        extra=dict(extra or {}),
    )


def restore_state(
    state: TrainingState,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    *,
    scaler: Any | None = None,
    generator: torch.Generator | None = None,
    rank_sampler: Any | None = None,
    strict: bool = True,
) -> TrainingState:
    model.load_state_dict(state.model, strict=strict)
    optimizer.load_state_dict(state.optimizer)
    if scaler is not None and state.scaler is not None:
        scaler.load_state_dict(state.scaler)
    random.setstate(pickle.loads(state.rng.python))
    np.random.set_state(pickle.loads(state.rng.numpy))
    torch.set_rng_state(
        torch.frombuffer(
            bytearray(state.rng.torch),
            dtype=torch.uint8,
        )
    )
    if state.rng.cuda is not None and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(
            [
                torch.frombuffer(
                    bytearray(item),
                    dtype=torch.uint8,
                )
                for item in state.rng.cuda
            ]
        )
    if generator is not None and state.generator is not None:
        generator.set_state(
            torch.frombuffer(
                bytearray(state.generator),
                dtype=torch.uint8,
            )
        )
    if (
        rank_sampler is not None
        and state.rank_sampler is not None
    ):
        rank_sampler.restore(state.rank_sampler)
    return state
