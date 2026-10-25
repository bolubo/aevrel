"""Evidence behind a readiness claim, and the rules that withdraw it.

Each case here is a claim that a consumer could otherwise read as true: a
restore drill that says it succeeded, a movable target, a complete closure.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aevrel.local.models import (
    CheckpointRecord,
    DrillOutcome,
    RankInventory,
    ReasonCode,
    ReadinessStatus,
)
from aevrel.local.readiness import MIN_DRILL_STEPS, assess_readiness
from aevrel.state import build_manifest, classify_resume_state


NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64


def _record(*, ranks: int = 2) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint="/ckpt/step-100",
        namespace="default",
        job_uid="job42",
        version="step-100",
        step=100,
        provider="dcp",
        format_version="1",
        metadata_digest=DIGEST,
        ranks=tuple(
            RankInventory(rank=index, shards=(), bytes=1024)
            for index in range(ranks)
        ),
        observed_at=NOW.isoformat(),
        latest_mtime=NOW.isoformat(),
        created_at=NOW.isoformat(),
        complete=True,
        reasons=(),
    )


def _manifest():
    return build_manifest(
        checkpoint_uid="job42",
        version="step-100",
        metadata_digest=DIGEST,
        world_size=2,
        present={
            "model": "lrs.training",
            "optimizer": "lrs.training",
            "global_step": "lrs.metadata",
            "dataloader": "lrs.training",
            "rng": "lrs.rng",
        },
        digests={
            "model": DIGEST,
            "optimizer": DIGEST,
            "global_step": DIGEST,
            "dataloader": DIGEST,
            "rng": DIGEST,
        },
    )


def _drill(**overrides) -> DrillOutcome:
    values = {
        "phase": "Succeeded",
        "succeeded": True,
        "steps_ran": 10,
        "proof_ref": "proof://drill/1",
        "resume_state_digests": (_manifest().compute_digest(),),
        "ranks_reported": (0, 1),
        "ranks_expected": 2,
        "checkpoint_version": "step-100",
        "checkpoint_metadata_digest": DIGEST,
    }
    values.update(overrides)
    return DrillOutcome(**values)


def _codes(result) -> set[str]:
    return {reason.code for reason in result.reasons}


def test_full_evidence_is_ready() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(),
        resume_state=classify_resume_state(_manifest().to_dict()),
        require_state_complete=True,
    )
    assert result.status == ReadinessStatus.READY
    assert result.loadable is True
    assert result.readiness_level == "L4"
    assert result.reasons == ()


def test_success_without_steps_is_withdrawn() -> None:
    assert MIN_DRILL_STEPS == 1
    result = assess_readiness(_record(), now=NOW, drill=_drill(steps_ran=0))
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    reasons = {reason.code: reason.detail for reason in result.reasons}
    detail = reasons[ReasonCode.DRILL_STEPS_MISSING.value]
    assert "0 training steps" in detail


def test_partial_rank_coverage_is_withdrawn() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(ranks_reported=(0,)),
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    reasons = {reason.code: reason.detail for reason in result.reasons}
    assert "covered 1 of 2 ranks, missing ranks [1]" in reasons[
        ReasonCode.DRILL_RANK_COVERAGE_INCOMPLETE.value
    ]


def test_half_of_a_four_rank_world_is_withdrawn() -> None:
    # The DoD states this case as "2 of 4": half a world is not a checkpoint.
    result = assess_readiness(
        _record(ranks=4),
        now=NOW,
        drill=_drill(ranks_reported=(0, 1), ranks_expected=4),
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    reasons = {reason.code: reason.detail for reason in result.reasons}
    assert "covered 2 of 4 ranks, missing ranks [2 3]" in reasons[
        ReasonCode.DRILL_RANK_COVERAGE_INCOMPLETE.value
    ]


def test_unreported_coverage_is_withdrawn() -> None:
    # Silence is not coverage. A drill that names no rank used to fall through
    # the gate and leave loadable=True; it must now withdraw the claim, and it
    # must not be dressed up as a partial restore either.
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(ranks_reported=(), ranks_expected=0),
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    codes = _codes(result)
    assert ReasonCode.DRILL_RANK_COVERAGE_UNREPORTED.value in codes
    assert ReasonCode.DRILL_RANK_COVERAGE_INCOMPLETE.value not in codes


def test_coverage_without_a_world_is_unproven() -> None:
    result = assess_readiness(
        _record(ranks=0),
        now=NOW,
        drill=_drill(ranks_reported=(0,), ranks_expected=0),
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert ReasonCode.DRILL_RANK_COVERAGE_UNREPORTED.value in _codes(result)


def test_proof_for_another_version_is_stale_not_failed() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(checkpoint_version="step-99"),
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    codes = _codes(result)
    assert ReasonCode.DRILL_PROOF_STALE.value in codes
    assert ReasonCode.DRILL_FAILED.value not in codes


def test_proof_for_another_digest_is_stale() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(checkpoint_metadata_digest=OTHER_DIGEST),
    )
    codes = _codes(result)
    assert ReasonCode.DRILL_PROOF_STALE.value in codes


def test_manifest_for_another_checkpoint_blocks() -> None:
    manifest = build_manifest(
        checkpoint_uid="job-other",
        version="step-100",
        metadata_digest=DIGEST,
        world_size=2,
        present={"model": "lrs.training", "optimizer": "lrs.training"},
        digests={"model": DIGEST, "optimizer": DIGEST},
    )
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(resume_state_digests=(manifest.compute_digest(),)),
        resume_state=classify_resume_state(manifest.to_dict()),
        require_state_complete=True,
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    assert result.readiness_level == "L3"
    codes = _codes(result)
    assert ReasonCode.RESUME_MANIFEST_INVALID.value in codes


def test_world_size_change_carries_a_topology_caveat() -> None:
    result = assess_readiness(_record(ranks=2), now=NOW, drill=_drill(), target_world_size=4)
    assert result.status == ReadinessStatus.READY
    assert result.movable is True
    codes = _codes(result)
    assert ReasonCode.TARGET_TOPOLOGY_UNVERIFIED.value in codes


def test_a_verified_topology_drops_the_caveat() -> None:
    result = assess_readiness(
        _record(ranks=2),
        now=NOW,
        drill=_drill(),
        target_world_size=4,
        topology_verified=True,
    )
    codes = _codes(result)
    assert ReasonCode.TARGET_TOPOLOGY_UNVERIFIED.value not in codes


def test_same_world_size_needs_no_caveat() -> None:
    result = assess_readiness(_record(ranks=2), now=NOW, drill=_drill(), target_world_size=2)
    assert result.movable is True
    codes = _codes(result)
    assert ReasonCode.TARGET_TOPOLOGY_UNVERIFIED.value not in codes
