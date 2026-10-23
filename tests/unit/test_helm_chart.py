from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "aevrel"


def test_chart_lints_and_renders_source_and_target_agents() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    lint = subprocess.run(
        [helm, "lint", str(CHART), "--set", "tls.enabled=false"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert lint.returncode == 0, lint.stderr + lint.stdout
    rendered = subprocess.run(
        [
            helm,
            "template",
            "aevrel",
            str(CHART),
            "--set",
            "target.enabled=true",
            "--set",
            "tls.enabled=false",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert "kind: Deployment" in rendered.stdout
    assert "kind: DaemonSet" in rendered.stdout
    assert "hostPort: 9443" in rendered.stdout
    assert "hostPort: 9444" in rendered.stdout
    assert "mountPath: /targets" in rendered.stdout
    assert 'resources: ["jobs"]' in rendered.stdout
