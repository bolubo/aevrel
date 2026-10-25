"""End-to-end resume-state closure golden path (RFC #30).

Save at world 4, resume at world 3, keep training: the whole chain
(manifest closure -> persisted components -> restore -> continuation ->
readiness level) is exercised in one deterministic CPU harness.

Definitions that make the claim checkable:

* **stream** - one :class:`RankSampler` owns the global sample stream
  (``start=0, end=DATA_SIZE, batch_size=GLOBAL_BATCH``). Its RNG state is a
  resume component, so losing it loses samples.
* **world size** - only decides how the ``GLOBAL_BATCH`` samples are sliced
  across replicas and how many gradient replicas are averaged. The stream
  itself is world-size independent, which is exactly the invariant Line 1
  claims: what a reshard must not lose does not depend on the shard count.
* **equivalence** - the resharded resume must replay the reference (never
  saved, never resharded) trajectory. Batching is bitwise identical; the
  only allowed drift is float summation order inside the gradient
  (``~1e-17``), never a lost or duplicated sample.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import torch
from torch import nn

from aevrel.local.errors import LocalError
from aevrel.local.models import CheckpointRecord, DrillOutcome, iso_utc
from aevrel.local.readiness import assess_readiness
from aevrel.lrs.resume_adapter import TrainingResumeAdapter
from aevrel.lrs.sampler import RankSampler
from aevrel.lrs.state_closure import (
    capture_state as capture_training,
    restore_state as restore_training,
)
from aevrel.state import (
    RESUME_COMPONENT_MISSING,
    RESUME_STATE_COMPLETE,
    StateBundle,
    capture_state as capture_bundle,
    classify_resume_state,
)

SOURCE_WORLD = 4
TARGET_WORLD = 3
GLOBAL_BATCH = 12
PARAM_DIM = 4
DATA_SIZE = 48
MODEL_SEED = 4242
DATA_SEED = 99
STEP_SEED = 77
STEPS_BEFORE = 3
STEPS_AFTER = 2
LR = 0.01
CHECKPOINT_UID = "ckpt-golden-0001"
CHECKPOINT_VERSION = "step-3"
METADATA_DIGEST = "sha256:" + "e" * 64
EQUIVALENCE_TOLERANCE = 1e-12
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)


def _dataset() -> tuple[torch.Tensor, torch.Tensor]:
    generator = torch.Generator().manual_seed(DATA_SEED)
    features = torch.randn(DATA_SIZE, PARAM_DIM, generator=generator).double()
    labels = torch.randn(DATA_SIZE, 1, generator=generator).double()
    return features, labels


def _model() -> nn.Module:
    torch.manual_seed(MODEL_SEED)
    return nn.Linear(PARAM_DIM, 1).double()


def _optimizer(model: nn.Module) -> torch.optim.Optimizer:
    return torch.optim.SGD(model.parameters(), lr=LR)


def _world(size: int) -> tuple[list[nn.Module], list[torch.optim.Optimizer]]:
    models = [_model() for _ in range(size)]
    return models, [_optimizer(model) for model in models]


def _max_drift(left: nn.Module, right: nn.Module) -> float:
    drift = 0.0
    for first, second in zip(left.parameters(), right.parameters(), strict=True):
        drift = max(drift, float((first.detach() - second.detach()).abs().max()))
    return drift


def _train_step(
    models: list[nn.Module],
    optimizers: list[torch.optim.Optimizer],
    features: torch.Tensor,
    labels: torch.Tensor,
    sampler: RankSampler,
) -> tuple[int, ...]:
    """One data-parallel step on a topology-free global batch."""
    batch = sampler.draw()
    batch_per_rank = GLOBAL_BATCH // len(models)
    gradients: list[list[torch.Tensor]] = []
    for index, model in enumerate(models):
        subset = list(batch[index * batch_per_rank:(index + 1) * batch_per_rank])
        optimizers[index].zero_grad()
        loss = ((model(features[subset]) - labels[subset]) ** 2).sum()
        loss.backward()
        gradients.append(
            [parameter.grad.detach().clone() for parameter in model.parameters()]
        )
    for position in range(len(gradients[0])):
        stacked = torch.stack(
            [rank_gradients[position] for rank_gradients in gradients]
        )
        # mean over replicas, then 1/batch_per_rank, is sum/GLOBAL_BATCH for
        # every world size: same real number, different summation order.
        averaged = stacked.mean(dim=0) / batch_per_rank
        for model in models:
            list(model.parameters())[position].grad = averaged.clone()
    for optimizer in optimizers:
        optimizer.step()
    return batch


@dataclass
class Rig:
    """One rank's model/optimizer/sampler wired to the resume adapter."""

    model: nn.Module
    optimizer: torch.optim.Optimizer
    sampler: RankSampler
    step: list[int]
    loader: dict | None
    adapter: TrainingResumeAdapter


def _rig(
    *,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    sampler: RankSampler,
    step: int,
    with_loader: bool = True,
) -> Rig:
    position = [step]
    loader: dict | None = {"epoch": 0, "position": step} if with_loader else None

    def capture():
        return capture_training(
            model,
            optimizer,
            step=position[0],
            consumed_samples=position[0] * GLOBAL_BATCH,
            micro_batch_size=GLOBAL_BATCH,
            data_loader=dict(loader) if loader is not None else None,
            rank_sampler=sampler.capture(),
        )

    def apply(state):
        restore_training(state, model, optimizer)
        sampler.restore(state.rank_sampler)
        position[0] = state.step
        if loader is not None and state.data_loader is not None:
            loader.update(state.data_loader)

    return Rig(
        model=model,
        optimizer=optimizer,
        sampler=sampler,
        step=position,
        loader=loader,
        adapter=TrainingResumeAdapter(capture, apply),
    )


def _stream_sampler() -> RankSampler:
    return RankSampler(
        rank=0,
        seed=STEP_SEED,
        start=0,
        end=DATA_SIZE,
        batch_size=GLOBAL_BATCH,
    )


def _manifest(rig: Rig, *, world: int):
    return rig.adapter.manifest(
        checkpoint_uid=CHECKPOINT_UID,
        checkpoint_version=CHECKPOINT_VERSION,
        metadata_digest=METADATA_DIGEST,
        world_size=world,
        ranks=tuple(range(world)),
    )


def _readiness(report, *, drill_digests: tuple[str, ...]):
    record = CheckpointRecord(
        checkpoint=CHECKPOINT_UID,
        namespace="team-a",
        job_uid=CHECKPOINT_UID,
        version=CHECKPOINT_VERSION,
        step=STEPS_BEFORE,
        provider="lrs",
        format_version="1",
        metadata_digest=METADATA_DIGEST,
        ranks=(),
        observed_at=iso_utc(NOW),
        latest_mtime=iso_utc(NOW),
        created_at=None,
        complete=True,
        reasons=(),
    )
    drill = DrillOutcome(
        phase="Succeeded",
        succeeded=True,
        steps_ran=STEPS_AFTER,
        proof_ref="proof-golden-0001",
        resume_state_digests=drill_digests,
    )
    return assess_readiness(
        record,
        now=NOW,
        max_age_seconds=3600,
        target_world_size=TARGET_WORLD,
        drill=drill,
        resume_state=report,
        require_state_complete=True,
    )


def _save(tmp_path: Path, rig: Rig, *, world: int, drop: tuple[str, ...] = ()):
    providers = [
        provider for provider in rig.adapter.providers() if provider.kind not in drop
    ]
    return capture_bundle(
        root=tmp_path / "state",
        checkpoint_id=CHECKPOINT_UID,
        version=CHECKPOINT_VERSION,
        rank=0,
        source_world_size=world,
        providers=providers,
    )


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_save_reshard_resume_replays_the_reference(tmp_path: Path) -> None:
    features, labels = _dataset()

    # Reference: world 4 for every step, never saved and never resharded.
    reference = _stream_sampler()
    reference_models, reference_optimizers = _world(SOURCE_WORLD)
    reference_stream = [
        _train_step(
            reference_models,
            reference_optimizers,
            features,
            labels,
            reference,
        )
        for _ in range(STEPS_BEFORE + STEPS_AFTER)
    ]

    # Source run: world 4 up to the save point.
    source = _stream_sampler()
    source_models, source_optimizers = _world(SOURCE_WORLD)
    saved_stream = [
        _train_step(
            source_models,
            source_optimizers,
            features,
            labels,
            source,
        )
        for _ in range(STEPS_BEFORE)
    ]
    assert saved_stream == reference_stream[:STEPS_BEFORE]

    # A data-parallel world keeps every replica bitwise identical.
    for replica in source_models[1:]:
        assert _max_drift(source_models[0], replica) == 0.0

    rig = _rig(
        model=source_models[0],
        optimizer=source_optimizers[0],
        sampler=source,
        step=STEPS_BEFORE,
    )
    _save(tmp_path, rig, world=SOURCE_WORLD)
    manifest = _manifest(rig, world=SOURCE_WORLD)
    report = classify_resume_state(manifest.to_dict())
    assert report.status == RESUME_STATE_COMPLETE
    assert report.reasons == ()
    assert report.manifest_digest is not None

    # ---- restore one world-3 rank from the persisted bytes only
    target_model = _model()
    target = _rig(
        model=target_model,
        optimizer=_optimizer(target_model),
        sampler=_stream_sampler(),
        step=0,
    )

    persisted = StateBundle.read(tmp_path / "state")
    before = _tree_digest(tmp_path / "state")
    restored = target.adapter.restore_exact(persisted, target_rank=0)
    assert restored.step == STEPS_BEFORE
    assert _max_drift(target_model, rig.model) == 0.0
    target.adapter.verify_restored(persisted, target_rank=0)

    # The source bundle is read-only evidence: restoring must not rewrite it.
    assert _tree_digest(tmp_path / "state") == before

    # ---- continue in the new world size
    target_models = [target_model]
    target_optimizers = [target.optimizer]
    for _ in range(TARGET_WORLD - 1):
        replica = _model()
        replica.load_state_dict(target_model.state_dict())
        target_models.append(replica)
        target_optimizers.append(_optimizer(replica))

    resumed_stream = [
        _train_step(
            target_models,
            target_optimizers,
            features,
            labels,
            target.sampler,
        )
        for _ in range(STEPS_AFTER)
    ]

    # Batching is bitwise continuous: no sample lost, none duplicated.
    assert resumed_stream == reference_stream[STEPS_BEFORE:]

    # Weights replay the reference up to float summation order.
    drift = _max_drift(target_models[0], reference_models[0])
    assert drift < EQUIVALENCE_TOLERANCE, drift
    for replica in target_models[1:]:
        assert _max_drift(replica, reference_models[0]) < EQUIVALENCE_TOLERANCE

    # Negative control: continuing the same stream from a *cold* model cannot
    # reproduce the reference, so the equivalence assertion is not vacuous.
    cold_model = _model()
    cold_sampler = _stream_sampler()
    cold_sampler.rng.set_state(source.rng.get_state())
    cold_models: list[nn.Module] = []
    cold_optimizers: list[torch.optim.Optimizer] = []
    for _ in range(TARGET_WORLD):
        replica = _model()
        replica.load_state_dict(cold_model.state_dict())
        cold_models.append(replica)
        cold_optimizers.append(_optimizer(replica))
    for _ in range(STEPS_AFTER):
        _train_step(cold_models, cold_optimizers, features, labels, cold_sampler)
    assert _max_drift(cold_models[0], reference_models[0]) > 1e-3

    # ---- readiness reaches L4 because the drill is anchored to this manifest
    readiness = _readiness(report, drill_digests=(report.manifest_digest,))
    assert readiness.state_complete is True
    assert readiness.status.value == "READY"
    assert readiness.readiness_level == "L4"
    assert readiness.last_drill_digest == report.manifest_digest


def test_missing_component_in_the_bundle_fails_closed(tmp_path: Path) -> None:
    model = _model()
    rig = _rig(
        model=model,
        optimizer=_optimizer(model),
        sampler=_stream_sampler(),
        step=STEPS_BEFORE,
    )
    # The closure advertises a complete set; the persisted bundle does not.
    assert (
        classify_resume_state(_manifest(rig, world=SOURCE_WORLD).to_dict()).status
        == RESUME_STATE_COMPLETE
    )
    incomplete = _save(tmp_path, rig, world=SOURCE_WORLD, drop=("dataloader",))

    target_model = _model()
    target = _rig(
        model=target_model,
        optimizer=_optimizer(target_model),
        sampler=_stream_sampler(),
        step=0,
    )
    with pytest.raises(LocalError) as excinfo:
        target.adapter.restore_exact(incomplete, target_rank=0)
    assert excinfo.value.code == RESUME_COMPONENT_MISSING


def test_incomplete_closure_blocks_the_target(tmp_path: Path) -> None:
    model = _model()
    rig = _rig(
        model=model,
        optimizer=_optimizer(model),
        sampler=_stream_sampler(),
        step=STEPS_BEFORE,
        with_loader=False,
    )
    _save(tmp_path, rig, world=SOURCE_WORLD)

    report = classify_resume_state(_manifest(rig, world=SOURCE_WORLD).to_dict())
    missing = {
        reason.details["kind"]
        for reason in report.reasons
        if reason.code == RESUME_COMPONENT_MISSING
    }
    assert missing == {"dataloader"}
    assert report.status != RESUME_STATE_COMPLETE

    readiness = _readiness(report, drill_digests=(report.manifest_digest,))
    assert readiness.state_complete is False
    assert readiness.status.value == "TARGET_BLOCKED"
    assert readiness.loadable is False
    assert readiness.readiness_level == "L3"
