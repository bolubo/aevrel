"""Tests for the checkpoint-level resume-state closure contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from aevrel.state import (
    RESUME_COMPONENT_INVALID,
    RESUME_COMPONENT_MISSING,
    RESUME_MANIFEST_INVALID,
    RESUME_OPTIONAL_COMPONENTS,
    RESUME_RANK_COVERAGE_INCOMPLETE,
    RESUME_REASON_CODES,
    RESUME_REQUIRED_COMPONENTS,
    RESUME_STATE_COMPLETE,
    RESUME_STATE_MALFORMED,
    RESUME_STATE_MISSING,
    RESUME_STATE_PARTIAL,
    RESUME_STATE_STATUSES,
    RESUME_TOPOLOGY_INCOMPATIBLE,
    ResumeComponent,
    ResumeComponentCoverage,
    ResumeStateManifest,
    classify_resume_state,
    parse_resume_state,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "resume-state"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def codes(report) -> set[str]:
    return {reason.code for reason in report.reasons}


def kinds_for(report, code: str) -> set[str]:
    return {
        str(reason.details["kind"])
        for reason in report.reasons
        if reason.code == code and "kind" in reason.details
    }


# --- four-state classification -------------------------------------------


def test_complete_fixture_is_closed() -> None:
    report = classify_resume_state(load("complete.json"))
    assert report.status == RESUME_STATE_COMPLETE
    assert report.closed is True
    assert report.reasons == ()
    assert report.manifest is not None
    assert report.to_error() is None


def test_incomplete_fixture_is_partial_with_named_kinds() -> None:
    report = classify_resume_state(load("incomplete.json"))
    assert report.status == RESUME_STATE_PARTIAL
    assert report.closed is False
    assert kinds_for(report, RESUME_COMPONENT_MISSING) == {"dataloader", "rng"}
    assert kinds_for(report, RESUME_RANK_COVERAGE_INCOMPLETE) == {"optimizer"}
    coverage = next(
        reason
        for reason in report.reasons
        if reason.code == RESUME_RANK_COVERAGE_INCOMPLETE
    )
    assert coverage.details["missingRanks"] == [1]


def test_malformed_fixture_is_malformed() -> None:
    report = classify_resume_state(load("malformed.json"))
    assert report.status == RESUME_STATE_MALFORMED
    assert report.closed is False
    assert report.manifest is None
    assert codes(report) == {RESUME_MANIFEST_INVALID}
    error = report.to_error()
    assert error is not None
    assert error.code == RESUME_MANIFEST_INVALID


def test_all_required_absent_is_missing_state() -> None:
    payload = load("complete.json")
    for entry in payload["components"]:
        if entry["kind"] in RESUME_REQUIRED_COMPONENTS:
            entry["present"] = False
            entry["digest"] = None
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_MISSING
    assert report.closed is False
    assert kinds_for(report, RESUME_COMPONENT_MISSING) == set(
        RESUME_REQUIRED_COMPONENTS
    )


# --- #5054 anchor: model-only manifest fails closed -----------------------


def test_model_only_manifest_fails_closed() -> None:
    report = classify_resume_state(load("model-only.json"))
    assert report.status == RESUME_STATE_PARTIAL
    assert report.closed is False
    missing = kinds_for(report, RESUME_COMPONENT_MISSING)
    assert {"optimizer", "global_step", "dataloader", "rng"} <= missing
    assert missing & set(RESUME_REQUIRED_COMPONENTS) == {
        "optimizer",
        "global_step",
        "dataloader",
        "rng",
    }
    error = report.to_error()
    assert error is not None
    assert error.code == RESUME_COMPONENT_MISSING
    assert error.details["status"] == RESUME_STATE_PARTIAL


# --- topology and declaration contradictions -----------------------------


def test_component_world_size_mismatch_is_topology_incompatible() -> None:
    report = classify_resume_state(load("topology-mismatch.json"))
    assert report.closed is False
    assert kinds_for(report, RESUME_TOPOLOGY_INCOMPATIBLE) == {"model"}


def test_placement_count_mismatch_is_topology_incompatible() -> None:
    payload = load("complete.json")
    payload["topology"]["placements"] = ["node-a/rank-0"]
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_PARTIAL
    assert RESUME_TOPOLOGY_INCOMPATIBLE in codes(report)
    topology_reason = next(
        reason
        for reason in report.reasons
        if reason.code == RESUME_TOPOLOGY_INCOMPATIBLE
    )
    assert topology_reason.details["placementCount"] == 1


def test_duplicate_component_kind_is_invalid() -> None:
    payload = load("complete.json")
    payload["components"].append(copy.deepcopy(payload["components"][0]))
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_PARTIAL
    assert "model" in kinds_for(report, RESUME_COMPONENT_INVALID)
    with pytest.raises(ValueError, match="duplicate component kinds"):
        parse_resume_state(payload)


def test_present_without_digest_is_invalid() -> None:
    payload = load("complete.json")
    payload["components"][0]["digest"] = None
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_PARTIAL
    assert payload["components"][0]["kind"] in kinds_for(
        report, RESUME_COMPONENT_INVALID
    )


def test_absent_with_digest_is_invalid() -> None:
    payload = load("incomplete.json")
    for entry in payload["components"]:
        if entry["kind"] == "lr_scheduler":
            entry["present"] = False
    report = classify_resume_state(payload)
    assert "lr_scheduler" in kinds_for(report, RESUME_COMPONENT_INVALID)


def test_required_flag_must_match_core_set() -> None:
    payload = load("complete.json")
    for entry in payload["components"]:
        if entry["kind"] == "model":
            entry["required"] = False
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_PARTIAL
    reason = next(
        item
        for item in report.reasons
        if item.code == RESUME_COMPONENT_INVALID
        and item.details.get("kind") == "model"
    )
    assert reason.details["expected"] is True


def test_undeclared_optional_component_is_missing() -> None:
    payload = load("complete.json")
    payload["components"] = [
        entry for entry in payload["components"] if entry["kind"] != "sampler"
    ]
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_PARTIAL
    assert "sampler" in kinds_for(report, RESUME_COMPONENT_MISSING)
    assert "sampler" in RESUME_OPTIONAL_COMPONENTS


def test_provider_defined_kind_allowed_when_optional() -> None:
    payload = load("complete.json")
    payload["components"].append(
        {
            "kind": "custom.activation_cache",
            "name": "activation-cache",
            "provider": "custom.provider",
            "digest": "sha256:" + "ab" * 32,
            "coverage": {"worldSize": 2, "ranks": [0, 1]},
            "required": False,
            "present": True,
        }
    )
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_COMPLETE
    assert report.closed is True


def test_provider_defined_kind_cannot_be_required() -> None:
    payload = load("complete.json")
    payload["components"].append(
        {
            "kind": "custom.activation_cache",
            "name": "activation-cache",
            "provider": "custom.provider",
            "digest": "sha256:" + "ab" * 32,
            "coverage": {"worldSize": 2, "ranks": [0, 1]},
            "required": True,
            "present": True,
        }
    )
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_PARTIAL
    assert "custom.activation_cache" in kinds_for(
        report, RESUME_COMPONENT_INVALID
    )


# --- contract invariants -------------------------------------------------


def test_schema_version_is_enforced() -> None:
    payload = load("complete.json")
    payload["schemaVersion"] = "2"
    report = classify_resume_state(payload)
    assert report.status == RESUME_STATE_MALFORMED
    assert RESUME_MANIFEST_INVALID in codes(report)


def test_required_and_optional_sets_are_frozen() -> None:
    assert RESUME_REQUIRED_COMPONENTS == (
        "model",
        "optimizer",
        "global_step",
        "dataloader",
        "rng",
    )
    assert RESUME_OPTIONAL_COMPONENTS == ("lr_scheduler", "sampler", "rank_local")


def test_reason_codes_cover_rfc_family() -> None:
    assert {
        "RESUME_COMPONENT_MISSING",
        "RESUME_COMPONENT_INVALID",
        "RESUME_COMPONENT_DIGEST_MISMATCH",
        "RESUME_RANK_COVERAGE_INCOMPLETE",
        "RESUME_TOPOLOGY_INCOMPATIBLE",
        "RESUME_STATE_UNVERIFIED",
    } <= RESUME_REASON_CODES
    assert RESUME_STATE_STATUSES == {
        RESUME_STATE_COMPLETE,
        RESUME_STATE_PARTIAL,
        RESUME_STATE_MISSING,
        RESUME_STATE_MALFORMED,
    }


def test_manifest_digest_is_canonical_and_order_independent() -> None:
    payload = load("complete.json")
    first = parse_resume_state(payload).compute_digest()
    shuffled = load("complete.json")
    shuffled["components"] = list(reversed(shuffled["components"]))
    second = parse_resume_state(shuffled).compute_digest()
    assert first == second
    assert first.startswith("sha256:")


def test_manifest_roundtrip_is_lossless() -> None:
    manifest = parse_resume_state(load("complete.json"))
    assert isinstance(manifest, ResumeStateManifest)
    again = parse_resume_state(manifest.to_dict())
    assert again == manifest


def test_manifest_never_asserts_loadability() -> None:
    manifest = parse_resume_state(load("complete.json"))
    report = classify_resume_state(load("complete.json"))
    serialized = json.dumps(report.to_dict())
    assert "loadable" not in serialized
    assert "readiness" not in serialized
    assert not hasattr(manifest, "loadable")
    assert set(report.to_dict()) == {
        "status",
        "closed",
        "manifestDigest",
        "reasons",
    }


def test_report_status_always_in_frozen_set() -> None:
    for name in (
        "complete.json",
        "incomplete.json",
        "model-only.json",
        "topology-mismatch.json",
        "malformed.json",
    ):
        assert classify_resume_state(load(name)).status in RESUME_STATE_STATUSES


def test_malformed_input_types_do_not_raise() -> None:
    for payload in (None, [], "text", 42, {"schemaVersion": "1"}):
        report = classify_resume_state(payload)
        assert report.status == RESUME_STATE_MALFORMED
        assert report.closed is False


def test_typed_component_rejects_bad_digest() -> None:
    with pytest.raises(ValueError, match="digest must be sha256"):
        ResumeComponent(
            kind="model",
            name="model",
            provider="lrs.training",
            digest="sha256:not-hex",
            coverage=ResumeComponentCoverage(world_size=1, ranks=(0,)),
            required=True,
            present=True,
        )
