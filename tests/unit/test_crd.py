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
    status = version["schema"]["openAPIV3Schema"]["properties"]["status"][
        "properties"
    ]
    assert "proofRef" in status
    assert "proofHash" in status
    assert "targetReady" in status
    assert "target-validating" in status["phase"]["enum"]
    assert "target-failed" in status["phase"]["enum"]
    source = version["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "properties"
    ]["source"]
    assert "agentSelector" in source["properties"]
    assert "rankMap" not in source.get("required", [])
    target = version["schema"]["openAPIV3Schema"]["properties"]["spec"][
        "properties"
    ]["target"]
    validation = target["properties"]["validation"]
    assert "jobTemplate" in validation["properties"]
    assert (
        validation["properties"]["jobTemplate"][
            "x-kubernetes-preserve-unknown-fields"
        ]
        is True
    )


def test_helm_crd_matches_config_crd() -> None:
    config = (
        ROOT
        / "config"
        / "crd"
        / "bases"
        / "shardferry.io_checkpointmigrations.yaml"
    ).read_text(encoding="utf-8")
    helm = (
        ROOT
        / "deploy"
        / "helm"
        / "shardferry"
        / "crds"
        / "checkpointmigration.yaml"
    ).read_text(encoding="utf-8")
    assert config == helm


def test_control_plane_crds_and_helm_copies_are_in_sync() -> None:
    pairs = {
        "checkpointrecords": "checkpointrecord",
        "restoredrills": "restoredrill",
        "rescuedecisions": "rescuedecision",
    }
    for plural, singular in pairs.items():
        config = (
            ROOT / "config" / "crd" / "bases" / f"shardferry.io_{plural}.yaml"
        ).read_text(encoding="utf-8")
        helm = (
            ROOT
            / "deploy"
            / "helm"
            / "shardferry"
            / "crds"
            / f"{singular}.yaml"
        ).read_text(encoding="utf-8")
        assert config == helm


def test_control_plane_crd_contracts() -> None:
    restore = yaml.safe_load(
        (
            ROOT
            / "config"
            / "crd"
            / "bases"
            / "shardferry.io_restoredrills.yaml"
        ).read_text(encoding="utf-8")
    )
    restore_spec = restore["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"
    ]["spec"]["properties"]
    assert "jobTemplate" in restore_spec
    assert "checkpointTemplate" in restore_spec
    assert (
        restore_spec["jobTemplate"]["x-kubernetes-preserve-unknown-fields"]
        is True
    )

    record = yaml.safe_load(
        (
            ROOT
            / "config"
            / "crd"
            / "bases"
            / "shardferry.io_checkpointrecords.yaml"
        ).read_text(encoding="utf-8")
    )
    record_spec = record["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"
    ]["spec"]["properties"]
    assert "agentSelector" in record_spec
    assert "agentEndpoint" in record_spec

    rescue = yaml.safe_load(
        (
            ROOT
            / "config"
            / "crd"
            / "bases"
            / "shardferry.io_rescuedecisions.yaml"
        ).read_text(encoding="utf-8")
    )
    rescue_spec = rescue["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"
    ]["spec"]["properties"]
    assert rescue_spec["fallback"]["enum"] == ["hold", "restart"]
    rescue_status = rescue["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"
    ]["status"]["properties"]
    assert "reasonCode" in rescue_status
