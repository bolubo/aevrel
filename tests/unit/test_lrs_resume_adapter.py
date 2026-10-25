"""Component-granular resume adapter tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import torch
from torch import nn

from aevrel.local.errors import LocalError
from aevrel.lrs.resume_adapter import (
    COMPONENT_KINDS,
    ComponentHook,
    TrainingResumeAdapter,
    assemble_state,
    slice_state,
)
from aevrel.lrs.sampler import RankSampler
from aevrel.lrs.state_closure import (
    capture_state as capture_training,
    restore_state as restore_training,
)
from aevrel.lrs.training_provider import training_states_equal
from aevrel.state import (
    RESUME_COMPONENT_DIGEST_MISMATCH,
    RESUME_COMPONENT_MISSING,
    RESUME_RANK_COVERAGE_INCOMPLETE,
    RESUME_STATE_COMPLETE,
    RESUME_STATE_PARTIAL,
    StateBundle,
    capture_state as capture_bundle,
    classify_resume_state,
)


@dataclass
class Rig:
    """A model/optimizer/sampler triple wired to a component adapter."""

    model: nn.Module
    optimizer: torch.optim.Optimizer
    generator: torch.Generator
    sampler: RankSampler
    step: list[int]
    adapter: TrainingResumeAdapter
    extras: dict = field(default_factory=dict)


def _rig(
    *,
    step: int = 3,
    with_loader: bool = True,
    with_scheduler: bool = False,
    hooks: dict | None = None,
) -> Rig:
    torch.manual_seed(11)
    model = nn.Sequential(nn.Linear(4, 8), nn.ReLU(), nn.Linear(8, 1))
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
    generator = torch.Generator().manual_seed(3)
    sampler = RankSampler(rank=0, seed=5, start=0, end=32, batch_size=4)
    step_holder = [step]

    def capture():
        extra = {}
        if with_scheduler:
            extra["lr_scheduler"] = {"last_epoch": step_holder[0], "base_lrs": [0.01]}
        return capture_training(
            model,
            optimizer,
            generator=generator,
            step=step_holder[0],
            extra=extra,
            consumed_samples=step_holder[0] * 4,
            data_loader={"position": step_holder[0]} if with_loader else None,
            rank_sampler=sampler.capture(),
        )

    def apply(state):
        restore_training(state, model, optimizer, generator=generator)
        sampler.restore(state.rank_sampler)
        step_holder[0] = state.step

    adapter = TrainingResumeAdapter(capture, apply, hooks=hooks or {})
    return Rig(
        model=model,
        optimizer=optimizer,
        generator=generator,
        sampler=sampler,
        step=step_holder,
        adapter=adapter,
    )


def _bundle(tmp_path: Path, rig: Rig, rank: int = 0) -> StateBundle:
    return capture_bundle(
        root=tmp_path / "state",
        checkpoint_id="ckpt-1",
        version="step-3",
        rank=rank,
        source_world_size=1,
        providers=list(rig.adapter.providers()),
    )


def _manifest(rig: Rig, *, world_size: int = 1, ranks: tuple[int, ...] | None = None):
    return rig.adapter.manifest(
        checkpoint_uid="ckpt-1",
        checkpoint_version="step-3",
        metadata_digest="sha256:" + "a" * 64,
        world_size=world_size,
        ranks=ranks,
    )


def test_snapshot_is_captured_once() -> None:
    rig = _rig()
    rig.adapter.freeze()
    rig.adapter.providers()
    _manifest(rig)
    rig.adapter.freeze()
    assert rig.adapter.capture_count == 1


def test_components_are_separate_entries(tmp_path: Path) -> None:
    rig = _rig()
    bundle = _bundle(tmp_path, rig)
    names = {entry.name for entry in bundle.manifest.entries}
    kinds = rig.adapter.freeze().kinds()
    assert "training.state" not in names
    assert names == {f"training.{kind}" for kind in kinds}
    assert bundle.manifest.entries[0].rank == 0
    assert len(bundle.manifest.entries) == len(names)


def test_slice_and_assemble_roundtrip_is_lossless() -> None:
    torch.manual_seed(21)
    model = nn.Linear(3, 2)
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    generator = torch.Generator().manual_seed(9)
    sampler = RankSampler(rank=0, seed=1, start=0, end=8, batch_size=2)
    state = capture_training(
        model,
        optimizer,
        generator=generator,
        step=7,
        extra={"lr_scheduler": {"last_epoch": 7}, "note": "keep"},
        consumed_samples=28,
        micro_batch_size=4,
        num_microbatches=1,
        grad_accum_steps=2,
        data_loader={"position": 7},
        rank_sampler=sampler.capture(),
    )
    payloads = slice_state(state)
    assert training_states_equal(state, assemble_state(payloads))
    assert set(payloads) == {
        "model",
        "optimizer",
        "global_step",
        "rng",
        "dataloader",
        "lr_scheduler",
        "sampler",
        "rank_local",
    }


def test_manifest_complete_for_single_rank() -> None:
    rig = _rig()
    report = classify_resume_state(_manifest(rig).to_dict())
    assert report.status == RESUME_STATE_COMPLETE
    assert report.closed is True


def test_manifest_reports_partial_coverage() -> None:
    rig = _rig()
    report = classify_resume_state(_manifest(rig, world_size=2, ranks=(0,)).to_dict())
    assert report.status == RESUME_STATE_PARTIAL
    assert RESUME_RANK_COVERAGE_INCOMPLETE in {
        reason.code for reason in report.reasons
    }


def test_missing_dataloader_fails_closed(tmp_path: Path) -> None:
    rig = _rig(with_loader=False)
    report = classify_resume_state(_manifest(rig).to_dict())
    assert report.status == RESUME_STATE_PARTIAL
    assert any(
        reason.code == RESUME_COMPONENT_MISSING
        and reason.details.get("kind") == "dataloader"
        for reason in report.reasons
    )
    bundle = _bundle(tmp_path, rig)
    target = _rig()
    with pytest.raises(LocalError) as error:
        target.adapter.restore_exact(bundle)
    assert error.value.code == RESUME_COMPONENT_MISSING
    assert "dataloader" in error.value.details["missing"]


def test_assemble_requires_every_required_component(tmp_path: Path) -> None:
    rig = _rig()
    bundle = _bundle(tmp_path, rig)
    payloads = {
        entry.name.split(".", 1)[1]: bundle.blobs[entry.digest]
        for entry in bundle.manifest.entries
    }
    assert set(payloads) >= set(COMPONENT_KINDS[:5])
    incomplete = {key: value for key, value in payloads.items() if key != "optimizer"}
    with pytest.raises(LocalError) as error:
        assemble_state(incomplete)
    assert error.value.code == RESUME_COMPONENT_MISSING
    assert error.value.details["missing"] == ["optimizer"]


def test_verify_restored_detects_tampering(tmp_path: Path) -> None:
    source = _rig()
    bundle = _bundle(tmp_path, source)
    target = _rig(step=0)
    target.adapter.restore_exact(bundle)
    target.adapter.verify_restored(bundle)
    with torch.no_grad():
        next(target.model.parameters()).mul_(2.0)
    with pytest.raises(LocalError) as error:
        target.adapter.verify_restored(bundle)
    assert error.value.code == RESUME_COMPONENT_DIGEST_MISMATCH


def test_optional_scheduler_is_declared_when_present() -> None:
    rig = _rig(with_scheduler=True)
    payloads = rig.adapter.freeze().payloads
    assert "lr_scheduler" in payloads
    assert assemble_state(payloads).extra["lr_scheduler"] == {
        "last_epoch": 3,
        "base_lrs": [0.01],
    }


def test_rank_local_absent_when_nothing_is_local() -> None:
    rig = _rig()
    payloads = rig.adapter.freeze().payloads
    assert "rank_local" not in payloads
    assert assemble_state(payloads).extra == {}


def test_optional_components_absent_are_declared_absent() -> None:
    rig = _rig()
    report = classify_resume_state(_manifest(rig).to_dict())
    lr_scheduler = report.manifest.component("lr_scheduler")
    assert lr_scheduler is not None
    assert lr_scheduler.present is False
    assert lr_scheduler.digest is None


def test_hooks_cover_out_of_kernel_state(tmp_path: Path) -> None:
    observed = {"restored": b""}
    hook = ComponentHook(
        capture=lambda: b"hook-state",
        restore=lambda data: observed.__setitem__("restored", data),
    )
    source = _rig(hooks={"lr_scheduler": hook})
    bundle = _bundle(tmp_path, source)
    names = {entry.name for entry in bundle.manifest.entries}
    assert "training.lr_scheduler" in names
    target = _rig(step=0, hooks={"lr_scheduler": hook})
    target.adapter.restore_exact(bundle)
    assert observed["restored"] == b"hook-state"
