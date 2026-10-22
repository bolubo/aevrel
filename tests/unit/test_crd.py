from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_checkpointmigration_crd_contract() -> None:
    path = (
        ROOT
        / "config"
        / "crd"
        / "bases"
        / "shardferry.io_checkpointmigrations.yaml"
    )
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert payload["apiVersion"] == "apiextensions.k8s.io/v1"
    assert payload["kind"] == "CustomResourceDefinition"
    spec = payload["spec"]
    assert spec["group"] == "shardferry.io"
    assert spec["names"]["kind"] == "CheckpointMigration"
    version = next(v for v in spec["versions"] if v["name"] == "v1")
    assert version["served"] is True
    assert version["storage"] is True
    required = version["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "required"
    ]
    assert set(required) == {
        "checkpoint",
        "source",
        "target",
        "policy",
        "artifacts",
    }
    status = version["schema"]["openAPIV3Schema"]["properties"]["status"][
        "properties"
    ]
    assert "proofRef" in status
    assert "proofHash" in status
    source = version["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "properties"
    ]["source"]
    assert "agentSelector" in source["properties"]
    assert "rankMap" not in source.get("required", [])
