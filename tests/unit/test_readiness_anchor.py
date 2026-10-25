"""The drill proof and the closure verdict must describe the same manifest."""

from __future__ import annotations

from datetime import datetime, timezone

from aevrel.local.models import (
    CheckpointRecord,
    DrillOutcome,
    RankInventory,
    ReasonCode,
    ReadinessStatus,
)
from aevrel.local.readiness import assess_readiness
from aevrel.state import build_manifest, classify_resume_state


NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64


def _manifest():
    return build_manifest(
        checkpoint_uid="job42",
        version="step-100",
        metadata_digest=DIGEST,
        world_size=1,
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


def _record() -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint="/ckpt/step-100",
        namespace="default",
        job_uid="job42",
        version="step-100",
        step=100,
        provider="dcp",
        format_version="1",
        metadata_digest=DIGEST,
        ranks=(RankInventory(rank=0, shards=(), bytes=1024),),
        observed_at=NOW.isoformat(),
        latest_mtime=NOW.isoformat(),
        created_at=NOW.isoformat(),
        complete=True,
        reasons=(),
    )


def _drill(*digests: str) -> DrillOutcome:
    return DrillOutcome(
        phase="Succeeded",
        succeeded=True,
        steps_ran=10,
        proof_ref="proof://drill/1",
        resume_state_digests=digests,
        ranks_reported=(0,),
        ranks_expected=1,
    )


def test_matching_manifest_digest_stays_ready() -> None:
    manifest = _manifest()
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(manifest.compute_digest()),
        resume_state=classify_resume_state(manifest.to_dict()),
    )
    assert result.status == ReadinessStatus.READY
    assert result.loadable is True
    assert result.last_drill_digest == manifest.compute_digest()
    codes = {reason.code for reason in result.reasons}
    assert ReasonCode.RESUME_COMPONENT_DIGEST_MISMATCH.value not in codes


def test_mismatched_manifest_digest_fails_closed() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(OTHER_DIGEST),
        resume_state=classify_resume_state(_manifest().to_dict()),
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.loadable is False
    assert result.readiness_level == "L3"
    reasons = {reason.code: reason.detail for reason in result.reasons}
    assert (
        reasons[ReasonCode.RESUME_COMPONENT_DIGEST_MISMATCH.value]
        == "drill restored a different resume-state manifest"
    )


def test_mismatch_blocks_a_gated_assessment() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(OTHER_DIGEST),
        resume_state=classify_resume_state(_manifest().to_dict()),
        require_state_complete=True,
    )
    assert result.status == ReadinessStatus.TARGET_BLOCKED
    assert result.readiness_level == "L3"
    assert result.state_complete is True


def test_absent_digest_evidence_does_not_block() -> None:
    result = assess_readiness(
        _record(),
        now=NOW,
        drill=_drill(),
        resume_state=classify_resume_state(_manifest().to_dict()),
    )
    assert result.status == ReadinessStatus.READY
    assert result.last_drill_digest is None
