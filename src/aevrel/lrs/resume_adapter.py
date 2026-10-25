"""Component-granular resume adapter for the LRS training-state kernel.

The kernel (:mod:`aevrel.lrs.state_closure`) captures a whole
``TrainingState``. This adapter slices one frozen snapshot into named
resume components (``model``, ``optimizer``, ``global_step``, ``rng``,
``dataloader`` plus declared optional ones), serves them as independent
bundle entries, refuses to restore an incomplete required set, and emits a
checkpoint-level :class:`~aevrel.state.ResumeStateManifest`.

Two invariants matter:

* **one snapshot** — the source state is captured exactly once per freeze,
  so every component belongs to the same instant;
* **one restore** — components are reassembled into a ``TrainingState`` and
  applied through the kernel's ``restore_state`` in a single call, so a
  partial component set can never be applied halfway.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import torch

from ..local.errors import LocalError
from ..state import (
    RESUME_COMPONENT_DIGEST_MISMATCH,
    RESUME_COMPONENT_MISSING,
    RESUME_OPTIONAL_COMPONENTS,
    RESUME_REQUIRED_COMPONENTS,
    ResumeCheckpoint,
    ResumeComponent,
    ResumeComponentCoverage,
    ResumeStateManifest,
    ResumeTopology,
    StateBundle,
    blob_digest,
)
from .state_closure import RNGState, TrainingState
from .training_provider import TrainingComponentProvider, training_states_equal

COMPONENT_KINDS = (
    "model",
    "optimizer",
    "global_step",
    "rng",
    "dataloader",
    "lr_scheduler",
    "sampler",
    "rank_local",
)


@dataclass(frozen=True)
class FrozenComponents:
    """One captured training state, sliced into component payloads."""

    state: TrainingState
    payloads: dict[str, bytes]

    def kinds(self) -> tuple[str, ...]:
        return tuple(
            kind for kind in COMPONENT_KINDS if kind in self.payloads
        )

    def missing_required(self) -> tuple[str, ...]:
        return tuple(
            kind
            for kind in RESUME_REQUIRED_COMPONENTS
            if kind not in self.payloads
        )


@dataclass(frozen=True)
class ComponentHook:
    """Capture/restore for state that lives outside the training kernel."""

    capture: Callable[[], bytes]
    restore: Callable[[bytes], None]


def _dump(payload: Any) -> bytes:
    buffer = io.BytesIO()
    torch.save(payload, buffer)
    return buffer.getvalue()


def _load(payload: bytes) -> Any:
    return torch.load(
        io.BytesIO(bytes(payload)),
        map_location="cpu",
        weights_only=False,
    )


def slice_state(
    state: TrainingState,
    hooks: Mapping[str, ComponentHook] | None = None,
) -> dict[str, bytes]:
    """Split a training state into component payloads.

    Optional components are omitted when the state does not carry them, so
    absence is recorded rather than guessed.
    """
    payloads: dict[str, bytes] = {
        "model": _dump(state.model),
        "optimizer": _dump(state.optimizer),
        "global_step": json.dumps(
            {
                "step": state.step,
                "consumed_samples": state.consumed_samples,
            },
            sort_keys=True,
        ).encode("utf-8"),
        "rng": _dump(
            {
                "python": state.rng.python,
                "numpy": state.rng.numpy,
                "torch": state.rng.torch,
                "cuda": state.rng.cuda,
                "generator": state.generator,
            }
        ),
    }
    if state.data_loader is not None:
        payloads["dataloader"] = _dump(
            {
                "data_loader": state.data_loader,
                "micro_batch_size": state.micro_batch_size,
                "num_microbatches": state.num_microbatches,
                "grad_accum_steps": state.grad_accum_steps,
            }
        )

    extra = dict(state.extra or {})
    scheduler = extra.pop("lr_scheduler", None)
    if scheduler is not None:
        payloads["lr_scheduler"] = _dump(scheduler)
    if state.rank_sampler is not None:
        payloads["sampler"] = bytes(state.rank_sampler)
    if state.scaler is not None or extra:
        payloads["rank_local"] = _dump({"scaler": state.scaler, "extra": extra})

    for kind, hook in (hooks or {}).items():
        if kind in RESUME_REQUIRED_COMPONENTS or kind in RESUME_OPTIONAL_COMPONENTS:
            payloads[kind] = bytes(hook.capture())
    return payloads


def assemble_state(
    payloads: Mapping[str, bytes],
    *,
    external_kinds: tuple[str, ...] = (),
) -> TrainingState:
    """Rebuild a training state from component payloads.

    ``external_kinds`` are components owned by hooks; their payloads are
    opaque bytes and are restored by the hook, not by the kernel.

    Raises :class:`LocalError` with ``RESUME_COMPONENT_MISSING`` when a
    required component is absent, so an incomplete set never restores.
    """
    missing = [
        kind for kind in RESUME_REQUIRED_COMPONENTS if kind not in payloads
    ]
    if missing:
        raise LocalError(
            RESUME_COMPONENT_MISSING,
            "cannot restore an incomplete resume component set: "
            + ", ".join(missing),
            details={"missing": missing},
        )
    step_payload = json.loads(bytes(payloads["global_step"]).decode("utf-8"))
    rng_payload = _load(payloads["rng"])
    data_payload = _load(payloads["dataloader"])
    rank_local = (
        _load(payloads["rank_local"]) if "rank_local" in payloads else None
    )
    extra = dict(rank_local.get("extra") or {}) if rank_local else {}
    if "lr_scheduler" in payloads and "lr_scheduler" not in external_kinds:
        extra["lr_scheduler"] = _load(payloads["lr_scheduler"])
    cuda = rng_payload.get("cuda")
    return TrainingState(
        step=int(step_payload["step"]),
        model=_load(payloads["model"]),
        optimizer=_load(payloads["optimizer"]),
        rng=RNGState(
            python=bytes(rng_payload["python"]),
            numpy=bytes(rng_payload["numpy"]),
            torch=bytes(rng_payload["torch"]),
            cuda=(
                tuple(bytes(item) for item in cuda)
                if cuda is not None
                else None
            ),
        ),
        generator=rng_payload.get("generator"),
        scaler=(rank_local or {}).get("scaler"),
        extra=extra,
        consumed_samples=int(step_payload.get("consumed_samples", 0)),
        micro_batch_size=data_payload.get("micro_batch_size"),
        num_microbatches=data_payload.get("num_microbatches"),
        grad_accum_steps=int(data_payload.get("grad_accum_steps", 1)),
        data_loader=data_payload.get("data_loader"),
        rank_sampler=(
            bytes(payloads["sampler"]) if "sampler" in payloads else None
        ),
    )


@dataclass
class TrainingResumeAdapter:
    """Freeze, serve, restore and describe one training state."""

    capture: Callable[[], TrainingState]
    apply: Callable[[TrainingState], None]
    hooks: Mapping[str, ComponentHook] = field(default_factory=dict)
    provider: str = "lrs.training"
    prefix: str = "training"
    _frozen: FrozenComponents | None = field(default=None, init=False)
    _captures: int = field(default=0, init=False)

    def freeze(self) -> FrozenComponents:
        """Capture the source state exactly once and slice it."""
        if self._frozen is None:
            self._frozen = self._snapshot()
        return self._frozen

    def refreeze(self) -> FrozenComponents:
        """Capture a new snapshot, discarding the cached one."""
        self._frozen = self._snapshot()
        return self._frozen

    def _snapshot(self) -> FrozenComponents:
        state = self.capture()
        self._captures += 1
        return FrozenComponents(state=state, payloads=slice_state(state, self.hooks))

    @property
    def capture_count(self) -> int:
        return self._captures

    def providers(self) -> tuple[TrainingComponentProvider, ...]:
        frozen = self.freeze()
        return tuple(
            TrainingComponentProvider(kind, frozen.payloads[kind], prefix=self.prefix)
            for kind in frozen.kinds()
        )

    def restore_exact(
        self,
        bundle: StateBundle,
        *,
        target_rank: int = 0,
    ) -> TrainingState:
        """Restore the exact component set of one source rank.

        Fails closed when a required component is missing from the bundle.
        """
        entries = bundle.entries_for_target(target_rank)
        payloads: dict[str, bytes] = {}
        for kind in COMPONENT_KINDS:
            entry = entries.get(f"{self.prefix}.{kind}")
            if entry is None:
                continue
            payload = bundle.blobs.get(entry.digest)
            if payload is None:
                raise LocalError(
                    RESUME_COMPONENT_MISSING,
                    f"bundle is missing the payload for {entry.name}",
                    details={"kind": kind},
                )
            payloads[kind] = payload
        state = assemble_state(
            payloads, external_kinds=tuple(self.hooks)
        )
        self.apply(state)
        for kind, hook in self.hooks.items():
            if kind in payloads:
                hook.restore(payloads[kind])
        return state

    def verify_restored(
        self,
        bundle: StateBundle,
        *,
        target_rank: int = 0,
    ) -> None:
        """Recapture after restore and compare every component digest."""
        entries = bundle.entries_for_target(target_rank)
        current = self._snapshot()
        for kind in COMPONENT_KINDS:
            entry = entries.get(f"{self.prefix}.{kind}")
            payload = current.payloads.get(kind)
            if entry is None and payload is None:
                continue
            if entry is None or payload is None:
                raise LocalError(
                    RESUME_COMPONENT_MISSING,
                    f"component {kind} does not match the captured set",
                    details={"kind": kind},
                )
            if blob_digest(payload) != entry.digest:
                raise LocalError(
                    RESUME_COMPONENT_DIGEST_MISMATCH,
                    f"restored component {kind} does not match its entry",
                    details={"kind": kind, "expected": entry.digest},
                )

    def manifest(
        self,
        *,
        checkpoint_uid: str,
        checkpoint_version: str,
        metadata_digest: str,
        world_size: int,
        ranks: tuple[int, ...] | None = None,
        mesh: str = "1d",
        placements: tuple[str, ...] = (),
    ) -> ResumeStateManifest:
        """Describe the frozen snapshot as a resume-state manifest."""
        frozen = self.freeze()
        coverage_ranks = (0,) if ranks is None else tuple(ranks)
        components = []
        for kind in COMPONENT_KINDS:
            payload = frozen.payloads.get(kind)
            components.append(
                ResumeComponent(
                    kind=kind,
                    name=f"{self.prefix}.{kind}",
                    provider=self.provider,
                    digest=blob_digest(payload) if payload is not None else None,
                    coverage=ResumeComponentCoverage(
                        world_size=world_size,
                        ranks=coverage_ranks,
                    ),
                    required=kind in RESUME_REQUIRED_COMPONENTS,
                    present=payload is not None,
                )
            )
        return ResumeStateManifest(
            schema_version="1",
            checkpoint=ResumeCheckpoint(
                uid=checkpoint_uid,
                version=checkpoint_version,
                metadata_digest=metadata_digest,
            ),
            topology=ResumeTopology(
                world_size=world_size,
                mesh=mesh,
                placements=placements,
            ),
            components=tuple(components),
        )

    def states_equal(self, left: TrainingState, right: TrainingState) -> bool:
        return training_states_equal(left, right)
