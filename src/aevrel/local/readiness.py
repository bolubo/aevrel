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

#: Smallest number of training steps a restore drill must have run before its
#: outcome counts as loadability evidence. A drill that loaded the bytes and
#: stopped proves the bytes load, not that training resumes. The Go control
#: plane uses the same number (``control.MinDrillSteps``).
MIN_DRILL_STEPS = 1


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
    topology_verified: bool = False,
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
    evidence_blocked = False
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
            gap = _drill_evidence_gap(record, drill)
            if gap is not None:
                # A drill that reports success still has to prove that it ran
                # training steps, covered the whole world, and was produced
                # for this checkpoint. Anything less withdraws the claim
                # instead of rounding it up to loadable=True.
                evidence_blocked = True
                loadable = False
                reasons.append(gap)
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

    # movable=True means the source is complete and the target world size is
    # sane. It is not a topology verdict. The caveat is attached only when the
    # target actually changes the world size, because that is precisely the
    # case where the coarse check is doing the most work and a consumer is
    # most likely to over-read it.
    if (
        movable is True
        and not topology_verified
        and target_world_size is not None
        and record.ranks
        and target_world_size != len(record.ranks)
    ):
        reasons.append(
            Reason(
                ReasonCode.TARGET_TOPOLOGY_UNVERIFIED.value,
                f"target world size {target_world_size} differs from the "
                f"source's {len(record.ranks)} ranks; mesh, placement and "
                "format compatibility are unverified",
            )
        )

    if not record.complete:
        status = ReadinessStatus.INCOMPLETE
    elif not fresh:
        status = ReadinessStatus.STALE
    elif drill is not None and not drill.succeeded:
        status = ReadinessStatus.TARGET_BLOCKED
    elif evidence_blocked or anchor_blocked or manifest_blocked:
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


def _drill_evidence_gap(
    record: CheckpointRecord,
    drill: DrillOutcome,
) -> Reason | None:
    """Explain why a drill that calls itself successful proves nothing.

    The check is deliberately one-directional: it can only withdraw a claim,
    never create one. It mirrors ``control.drillEvidenceGap`` in Go so the two
    implementations cannot drift apart.
    """
    if (
        drill.checkpoint_metadata_digest
        and record.metadata_digest
        and drill.checkpoint_metadata_digest != record.metadata_digest
    ):
        return Reason(
            ReasonCode.DRILL_PROOF_STALE.value,
            f"drill proof is bound to metadata digest "
            f"{drill.checkpoint_metadata_digest!r}, the record is "
            f"{record.metadata_digest!r}",
        )
    if (
        drill.checkpoint_version
        and record.version
        and drill.checkpoint_version != record.version
    ):
        return Reason(
            ReasonCode.DRILL_PROOF_STALE.value,
            f"drill proof is bound to version {drill.checkpoint_version!r}, "
            f"the record is {record.version!r}",
        )
    if drill.steps_ran < MIN_DRILL_STEPS:
        return Reason(
            ReasonCode.DRILL_STEPS_MISSING.value,
            f"restore drill ran {drill.steps_ran} training steps, at least "
            f"{MIN_DRILL_STEPS} are required",
        )
    return _rank_coverage_gap(record, drill)


def _rank_coverage_gap(record, drill) -> Reason | None:
    """Report why the drill's rank coverage does not support loadable=True.

    Three answers, and they are deliberately different facts:

    - the drill named no rank: coverage is *unreported*
      (DRILL_RANK_COVERAGE_UNREPORTED). Absence is not zero, and it is
      certainly not "all ranks". This branch is checked first on purpose:
      measuring a silent drill against the inventory would report "covered 0
      of 4", which asserts that the drill ran and proved nothing - a fact we
      do not have;
    - the drill covered fewer ranks than the checkpoint has: the restore was
      partial (DRILL_RANK_COVERAGE_INCOMPLETE), and the missing ranks are
      named so an operator can act on them;
    - the drill covered every expected rank: no gap.
    """
    if not drill.ranks_reported:
        return Reason(
            ReasonCode.DRILL_RANK_COVERAGE_UNREPORTED.value,
            "restore drill reported no rank coverage, so full coverage is "
            "unproven",
        )
    reported = set(drill.ranks_reported)
    # The checkpoint's own rank inventory is authoritative when present: a
    # drill can only be judged against the world the checkpoint actually has.
    if record.ranks:
        expected = sorted(rank.rank for rank in record.ranks)
    else:
        expected = list(range(drill.ranks_expected))
    if not expected:
        # The drill named ranks but no world size, and the record carries no
        # inventory: there is nothing to measure completeness against, so the
        # claim stays unproven instead of being rounded up.
        return Reason(
            ReasonCode.DRILL_RANK_COVERAGE_UNREPORTED.value,
            f"restore drill reported ranks {_rank_list(drill.ranks_reported)} "
            "but no expected world size, so full coverage is unproven",
        )
    missing = [rank for rank in expected if rank not in reported]
    if missing:
        return Reason(
            ReasonCode.DRILL_RANK_COVERAGE_INCOMPLETE.value,
            f"restore drill covered {len(expected) - len(missing)} of "
            f"{len(expected)} ranks, missing ranks {_rank_list(missing)}",
        )
    return None


def _rank_list(ranks) -> str:
    """Render a rank set the way the Go side does (`[0 2 3]`).

    The verdict is published by both implementations, so the two must not
    disagree on how the same evidence is described.
    """
    return "[" + " ".join(str(rank) for rank in ranks) + "]"


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
