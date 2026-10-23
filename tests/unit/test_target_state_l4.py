from __future__ import annotations

import random
from pathlib import Path

import pytest

from aevrel.commands.target_load import _restore_state
from aevrel.state import (
    JsonStateProvider,
    PythonRandomProvider,
    StateBundle,
    StateBundleError,
    capture_state,
)


def test_target_load_restores_rank_rng(tmp_path: Path) -> None:
    root = tmp_path / "state"
    source = random.Random(1234)
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=1,
        providers=[PythonRandomProvider(source)],
    )
    expected = source.random()

    random.seed(9999)
    result = _restore_state(
        root=root,
        target_rank=0,
        provider_names=["python.random"],
        require_complete=True,
    )

    assert result["state_complete"] is True
    assert result["state_root"] == StateBundle.read(root).manifest.root
    assert result["state_providers"] == ["python.random"]
    assert random.random() == expected


def test_target_load_rejects_incomplete_state(tmp_path: Path) -> None:
    root = tmp_path / "state"
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=2,
        providers=[PythonRandomProvider(random.Random(1))],
    )
    with pytest.raises(StateBundleError, match="not closure-complete"):
        _restore_state(
            root=root,
            target_rank=0,
            provider_names=["python.random"],
            require_complete=True,
        )


def test_target_load_rejects_missing_provider(tmp_path: Path) -> None:
    root = tmp_path / "state"
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=1,
        providers=[
            PythonRandomProvider(random.Random(1)),
            JsonStateProvider(
                "scheduler",
                lambda: {"last_epoch": 3},
                lambda _: None,
            ),
        ],
    )
    with pytest.raises(StateBundleError, match="do not cover"):
        _restore_state(
            root=root,
            target_rank=0,
            provider_names=["python.random"],
            require_complete=True,
        )


def test_target_load_rejects_corrupt_state(tmp_path: Path) -> None:
    root = tmp_path / "state"
    capture_state(
        root=root,
        checkpoint_id="job-1",
        version="step-100",
        rank=0,
        source_world_size=1,
        providers=[PythonRandomProvider(random.Random(1))],
    )
    bundle = StateBundle.read(root)
    digest = bundle.manifest.entries[0].digest
    StateBundle.blob_path(root, digest).write_bytes(b"corrupted")
    with pytest.raises(StateBundleError, match="blob mismatch"):
        _restore_state(
            root=root,
            target_rank=0,
            provider_names=["python.random"],
            require_complete=True,
        )
