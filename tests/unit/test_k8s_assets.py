from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "deploy" / "k8s"


def test_k8s_manifests_are_valid_yaml() -> None:
    paths = sorted(K8S.glob("*.yaml"))
    assert paths
    for path in paths:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        assert payload.get("apiVersion")
        assert payload.get("kind")
