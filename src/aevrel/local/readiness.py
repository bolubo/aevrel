"""Local convenience assessment; the Go controller is authoritative."""

from __future__ import annotations

from datetime import datetime, timezone

from .models import (
    CheckpointReadiness,
    CheckpointRecord,
    DrillOutcome,
    Reason,
    ReasonCode,
    ReadinessStatus,
)


DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def assess_readiness(
    record: CheckpointRecord,
    *,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    target_world_size: int | None = None,
    drill: DrillOutcome | None = None,
) -> CheckpointReadiness:
    current = now or datetime.now(timezone.utc)
    reasons = list(record.reasons)
    latest = _parse_time(record.latest_mtime)
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

    if not record.complete:
        status = ReadinessStatus.INCOMPLETE
    elif not fresh:
        status = ReadinessStatus.STALE
    elif drill is not None and not drill.succeeded:
        status = ReadinessStatus.TARGET_BLOCKED
    elif loadable is None:
        status = ReadinessStatus.UNVERIFIED
    elif movable is False:
        status = ReadinessStatus.TARGET_BLOCKED
    else:
        status = ReadinessStatus.READY

    return CheckpointReadiness(
        status=status,
        complete=record.complete,
        fresh=fresh,
        loadable=loadable,
        movable=movable,
        reasons=tuple(_dedupe(reasons)),
        observed_at=current.isoformat(),
        target_world_size=target_world_size,
        estimated_rto_seconds=(
            int(age_seconds) if age_seconds is not None else None
        ),
        last_drill=last_drill,
    )


def _dedupe(reasons: list[Reason]) -> list[Reason]:
    seen: set[tuple[str, str]] = set()
    result: list[Reason] = []
    for reason in reasons:
        key = (reason.code, reason.detail)
        if key not in seen:
            seen.add(key)
            result.append(reason)
    return result
