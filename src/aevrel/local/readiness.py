"""Local convenience assessment; the Go controller is authoritative.

Semantics follow `docs/READINESS-SEMANTICS.md`:

- freshness is derived from a *content* timestamp (provider-declared
  ``created_at`` first, filesystem ``latest_mtime`` as fallback), never
  from the scan time ``observed_at``;
- ``state_complete`` reports resume-state closure (RFC #30) as a
  tri-state: ``None`` = no closure evidence, ``True`` = closure complete,
  ``False`` = closure incomplete;
- the L4 gate (``require_state_complete``) is opt-in. Without it the
  assessment stays an L3 claim and merely surfaces ``state_complete``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .models import (
    CheckpointReadiness,
    CheckpointRecord,
    DrillOutcome,
    Reason,
    ReasonCode,
    ReadinessStatus,
    iso_utc,
)

if TYPE_CHECKING:
    from ..state.resume_state import ResumeStateReport


DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def content_timestamp(record: CheckpointRecord) -> datetime | None:
    """Newest timestamp that describes the checkpoint content itself.

    Provider-declared ``created_at`` wins; filesystem ``latest_mtime`` is a
    fallback because copies and object stores may rewrite it. The scan time
    (``observed_at``) is deliberately excluded: it measures our own process,
    so using it would report every checkpoint as fresh forever.
    """
    return _parse_time(record.created_at) or _parse_time(record.latest_mtime)


def assess_readiness(
    record: CheckpointRecord,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    target_world_size: int | None = None,
    drill: DrillOutcome | None = None,
    resume_state: "ResumeStateReport | None" = None,
    require_state_complete: bool = False,
) -> CheckpointReadiness:
    current = now or datetime.now(timezone.utc)
    reasons = list(record.reasons)
    latest = content_timestamp(record)
    age_seconds = (
        max(0.0, (current - latest).total_seconds())
        if latest is not None
        else None
    )
    fresh = age_seconds is not None and age_seconds <= max_age_seconds
    if age_seconds is not None and not fresh:
        reasons.append(
            Reason(
                ReasonCode.STALE.value,
                f"checkpoint is {int(age_seconds)} seconds old",
            )
        )

    if target_world_size is not None and target_world_size < 1:
        reasons.append(
            Reason(
                ReasonCode.TARGET_BLOCKED.value,
                "target world size must be positive",
            )
        )
    movable = None
    if target_world_size is not None:
        movable = record.complete and target_world_size > 0
        if not movable:
            reasons.append(
                Reason(
                    ReasonCode.TARGET_BLOCKED.value,
                    "source is incomplete or target is invalid",
                )
            )

    loadable = None
    last_drill = None
    if drill is not None:
        last_drill = drill.proof_ref or drill.phase
        loadable = drill.succeeded
        if not drill.succeeded:
            reasons.append(
                Reason(
                    ReasonCode.DRILL_FAILED.value,
                    drill.detail or "restore drill failed",
                )
            )
    else:
        reasons.append(
            Reason(
                ReasonCode.DRILL_MISSING.value,
                "no restore drill has verified this checkpoint",
            )
        )

    state_complete: bool | None = None
    closure_gate_blocked = False
    manifest_blocked = False
    if resume_state is not None:
        state_complete = resume_state.closed
        if require_state_complete and not state_complete:
            closure_gate_blocked = True
            loadable = False
            reasons.append(
                Reason(
                    ReasonCode.STATE_CLOSURE_INCOMPLETE.value,
                    _closure_detail(resume_state),
                )
            )

    # Anchor 1: the manifest must describe this checkpoint. A manifest for
    # another checkpoint or version would let a verdict vouch for state that
    # is not on disk here.
    if resume_state is not None and resume_state.manifest is not None:
        identity_issue = _manifest_identity_mismatch(record, resume_state.manifest)
        if identity_issue is not None:
            manifest_blocked = True
            loadable = False
            reasons.append(
                Reason(ReasonCode.RESUME_MANIFEST_INVALID.value, identity_issue)
            )

    # Anchor 2: the manifest the drill restored must be the manifest being
    # assessed. Without this, a drill proof and a closure verdict could
    # describe different checkpoints and still read as READY.
    last_drill_digest = None
    anchor_blocked = False
    if drill is not None and drill.resume_state_digests:
        if len(drill.resume_state_digests) == 1:
            last_drill_digest = drill.resume_state_digests[0]
        expected_digest = (
            resume_state.manifest_digest if resume_state is not None else None
        )
        if expected_digest is not None:
            if any(
                digest != expected_digest
                for digest in drill.resume_state_digests
            ):
                anchor_blocked = True
                loadable = False
                reasons.append(
                    Reason(
                        ReasonCode.RESUME_COMPONENT_DIGEST_MISMATCH.value,
                        "drill restored a different resume-state manifest",
                    )
                )

    if not record.complete:
        status = ReadinessStatus.INCOMPLETE
    elif not fresh:
        status = ReadinessStatus.STALE
    elif drill is not None and not drill.succeeded:
        status = ReadinessStatus.TARGET_BLOCKED
    elif anchor_blocked or manifest_blocked:
        status = ReadinessStatus.TARGET_BLOCKED
    elif loadable is None:
        status = ReadinessStatus.UNVERIFIED
    elif movable is False:
        status = ReadinessStatus.TARGET_BLOCKED
    else:
        status = ReadinessStatus.READY

    if closure_gate_blocked and status == ReadinessStatus.READY:
        status = ReadinessStatus.TARGET_BLOCKED

    # L4 is a claim, so it requires the gate to be applied, the closure to
    # be complete, the whole assessment to end READY, and the drill to have
    # proved it restored *this* manifest. A complete closure nobody resumed
    # from is an L3 fact, not an L4 proof.
    readiness_level = (
        "L4"
        if require_state_complete
        and state_complete is True
        and last_drill_digest is not None
        and status == ReadinessStatus.READY
        else "L3"
    )

    return CheckpointReadiness(
        status=status,
        complete=record.complete,
        fresh=fresh,
        loadable=loadable,
        movable=movable,
        reasons=tuple(_dedupe(reasons)),
        observed_at=iso_utc(current),
        target_world_size=target_world_size,
        estimated_rto_seconds=(
            int(age_seconds) if age_seconds is not None else None
        ),
        last_drill=last_drill,
        last_drill_digest=last_drill_digest,
        state_complete=state_complete,
        readiness_level=readiness_level,
    )


def _manifest_identity_mismatch(record, manifest) -> str | None:
    """Describe how a manifest disagrees with the record, or None."""
    checkpoint = manifest.checkpoint
    if record.job_uid and checkpoint.uid != record.job_uid:
        return (
            f"manifest describes checkpoint {checkpoint.uid!r}, "
            f"record is {record.job_uid!r}"
        )
    if record.version and checkpoint.version != record.version:
        return (
            f"manifest describes version {checkpoint.version!r}, "
            f"record is {record.version!r}"
        )
    if (
        record.metadata_digest
        and checkpoint.metadata_digest != record.metadata_digest
    ):
        return "manifest metadata digest does not match the checkpoint record"
    return None


def _closure_detail(report: "ResumeStateReport") -> str:
    codes = ", ".join(reason.code for reason in report.reasons)
    detail = f"resume-state closure is {report.status}"
    if codes:
        detail = f"{detail}: {codes}"
    return detail


def _dedupe(reasons: list[Reason]) -> list[Reason]:
    seen: set[tuple[str, str]] = set()
    result: list[Reason] = []
    for reason in reasons:
        key = (reason.code, reason.detail)
        if key not in seen:
            seen.add(key)
            result.append(reason)
    return result
