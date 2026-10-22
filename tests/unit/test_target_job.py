from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from shardferry.target_job import (
    KubernetesTargetReady,
    TargetJobConfig,
    evaluate_target_reports,
)
from shardferry.verification.target_ready import TargetReadyError


def job_template() -> dict:
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


def test_target_job_render_injects_indexed_contract(tmp_path: Path) -> None:
    template = tmp_path / "job.yaml"
    template.write_text(yaml.safe_dump(job_template()), encoding="utf-8")
    ready = KubernetesTargetReady(
        TargetJobConfig(
            template=template,
            namespace="training",
            world_size=4,
            steps=10,
            job_id="handoff-a",
            checkpoint_template="/targets/{version_id}/rank{rank}",
        )
    )

    rendered = ready.render("step-200")

    assert rendered["metadata"]["name"]
    assert rendered["metadata"]["namespace"] == "training"
    assert rendered["spec"]["completionMode"] == "Indexed"
    assert rendered["spec"]["completions"] == 4
    assert rendered["spec"]["parallelism"] == 4
    env = {
        item["name"]: item["value"]
        for item in rendered["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["SHARDFERRY_VERSION"] == "step-200"
    assert env["SHARDFERRY_VALIDATION_STEPS"] == "10"
    assert env["SHARDFERRY_CHECKPOINT_TEMPLATE"] == "/targets/step-200/rank{rank}"


def test_evaluate_target_reports_requires_all_ranks() -> None:
    report = {
        "schemaVersion": "1",
        "migrationUID": "handoff-a",
        "version": "step-200",
        "rank": 0,
        "modelLoaded": True,
        "optimizerLoaded": True,
        "stepsRan": 2,
        "ok": True,
        "error": "",
    }
    evaluate_target_reports(
        [report],
        world_size=1,
        version="step-200",
        job_id="handoff-a",
        steps=2,
    )
    with pytest.raises(TargetReadyError, match="missing"):
        evaluate_target_reports(
            [report],
            world_size=2,
            version="step-200",
            job_id="handoff-a",
            steps=2,
        )
    with pytest.raises(TargetReadyError, match="steps"):
        evaluate_target_reports(
            [{**report, "stepsRan": 1}],
            world_size=1,
            version="step-200",
            job_id="handoff-a",
            steps=2,
        )


def test_target_job_runs_with_kubectl_adapter(tmp_path: Path) -> None:
    template = tmp_path / "job.yaml"
    template.write_text(yaml.safe_dump(job_template()), encoding="utf-8")
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
    pod = {
        "metadata": {
            "name": "worker-0",
            "annotations": {
                "batch.kubernetes.io/job-completion-index": "0",
            },
        },
        "status": {
            "phase": "Succeeded",
            "containerStatuses": [
                {
                    "state": {
                        "terminated": {
                            "message": json.dumps(report),
                        }
                    }
                }
            ],
        },
    }
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = ' '.join(sys.argv[1:])\n"
        "if 'apply' in args:\n"
        "    sys.stdin.read()\n"
        "elif 'wait' in args:\n"
        "    pass\n"
        "elif 'get' in args and 'pods' in args:\n"
        f"    print(json.dumps({json.dumps({'items': [pod]})}))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    ready = KubernetesTargetReady(
        TargetJobConfig(
            template=template,
            namespace="default",
            world_size=1,
            steps=2,
            job_id="handoff-a",
            checkpoint_template="/targets/{version_id}/rank{rank}",
            timeout=5,
            kubectl=str(kubectl),
        )
    )

    ready("step-200")
