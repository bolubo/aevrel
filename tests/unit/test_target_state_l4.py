from __future__ import annotations

import random
from pathlib import Path

import pytest
import yaml

from shardferry.commands.target_load import _restore_state
from shardferry.state import (
    JsonStateProvider,
    PythonRandomProvider,
    StateBundle,
    StateBundleError,
    capture_state,
)
from shardferry.target_job import (
    KubernetesTargetReady,
    TargetJobConfig,
    evaluate_target_reports,
)
from shardferry.verification.target_ready import TargetReadyError


def _job_template() -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": ""},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "trainer",
                            "image": "example/trainer:latest",
                            "command": ["python", "train.py"],
                        }
                    ]
                }
            }
        },
    }


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


def test_target_job_render_injects_state_env(tmp_path: Path) -> None:
    template = tmp_path / "job.yaml"
    template.write_text(yaml.safe_dump(_job_template()), encoding="utf-8")
    ready = KubernetesTargetReady(
        TargetJobConfig(
            template=template,
            namespace="training",
            world_size=2,
            steps=4,
            job_id="handoff-a",
            checkpoint_template="/targets/{version_id}/rank{rank}",
            state_template="/state/{version_id}",
            require_state=True,
        )
    )
    rendered = ready.render("step-200")
    env = {
        item["name"]: item["value"]
        for item in rendered["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["SHARDFERRY_STATE_TEMPLATE"] == "/state/step-200"
    assert env["SHARDFERRY_REQUIRE_STATE"] == "1"
    assert "python.random" in env["SHARDFERRY_STATE_PROVIDERS"]


def test_target_job_require_state_needs_template(tmp_path: Path) -> None:
    template = tmp_path / "job.yaml"
    template.write_text(yaml.safe_dump(_job_template()), encoding="utf-8")
    with pytest.raises(ValueError, match="state_template"):
        TargetJobConfig(
            template=template,
            namespace="training",
            world_size=1,
            steps=1,
            job_id="handoff-a",
            checkpoint_template="/targets/{version_id}/rank{rank}",
            require_state=True,
        )


def test_evaluate_target_reports_requires_state() -> None:
    report = {
        "schemaVersion": "1",
        "migrationUID": "handoff-a",
        "version": "step-200",
        "rank": 0,
        "role": "worker",
        "modelLoaded": True,
        "optimizerLoaded": True,
        "stepsRan": 2,
        "stateComplete": False,
        "ok": True,
        "error": "",
    }
    with pytest.raises(TargetReadyError, match="rank-local state"):
        evaluate_target_reports(
            [report],
            world_size=1,
            version="step-200",
            job_id="handoff-a",
            steps=2,
            require_state=True,
        )
    evaluate_target_reports(
        [{**report, "stateComplete": True}],
        world_size=1,
        version="step-200",
        job_id="handoff-a",
        steps=2,
        require_state=True,
    )
