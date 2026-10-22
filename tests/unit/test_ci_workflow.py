from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ci_workflow_has_linux_python_go_and_helm_jobs() -> None:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(workflow["jobs"]) == {"python", "go", "helm", "docker"}
    assert "push" in workflow[True]
    assert "workflow_dispatch" in workflow[True]
