"""Local adapter data shapes; Go CRDs remain the control-plane authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReadinessStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"
    READY = "READY"
    TARGET_BLOCKED = "TARGET_BLOCKED"


class ReasonCode(str, Enum):
    METADATA_MISSING = "METADATA_MISSING"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    RANK_MISSING = "RANK_MISSING"
    RANK_DUPLICATE = "RANK_DUPLICATE"
    SHARD_MISSING = "SHARD_MISSING"
    SHARD_TRUNCATED = "SHARD_TRUNCATED"
    STALE = "STALE"
    DRILL_MISSING = "DRILL_MISSING"
    DRILL_FAILED = "DRILL_FAILED"
    TARGET_UNSPECIFIED = "TARGET_UNSPECIFIED"
    TARGET_BLOCKED = "TARGET_BLOCKED"


class DrillPhase(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


class RescueAction(str, Enum):
    RESTORE = "restore"
    RESTART = "restart"
    MIGRATE = "migrate"
    HOLD = "hold"


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ShardRef:
    name: str
    relative_path: str
    rank: int
    offset: int
    length: int
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "rank": self.rank,
            "offset": self.offset,
            "length": self.length,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class RankInventory:
    rank: int
    shards: tuple[ShardRef, ...]
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "shards": [shard.to_dict() for shard in self.shards],
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint: str
    namespace: str
    job_uid: str
    version: str
    step: int | None
    provider: str
    format_version: str
    metadata_digest: str
    ranks: tuple[RankInventory, ...]
    observed_at: str
    latest_mtime: str | None
    complete: bool
    reasons: tuple[Reason, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint,
            "namespace": self.namespace,
            "job_uid": self.job_uid,
            "version": self.version,
            "step": self.step,
            "provider": self.provider,
            "format_version": self.format_version,
            "metadata_digest": self.metadata_digest,
            "ranks": [rank.to_dict() for rank in self.ranks],
            "observed_at": self.observed_at,
            "latest_mtime": self.latest_mtime,
            "complete": self.complete,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class DrillOutcome:
    phase: str
    succeeded: bool
    steps_ran: int = 0
    proof_ref: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "succeeded": self.succeeded,
            "steps_ran": self.steps_ran,
            "proof_ref": self.proof_ref,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CheckpointReadiness:
    status: ReadinessStatus
    complete: bool
    fresh: bool
    loadable: bool | None
    movable: bool | None
    reasons: tuple[Reason, ...]
    observed_at: str
    target_world_size: int | None = None
    estimated_rto_seconds: int | None = None
    last_drill: str | None = None
    last_drill_version: str | None = None
    last_drill_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "complete": self.complete,
            "fresh": self.fresh,
            "loadable": self.loadable,
            "movable": self.movable,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "observed_at": self.observed_at,
            "target_world_size": self.target_world_size,
            "estimated_rto_seconds": self.estimated_rto_seconds,
            "last_drill": self.last_drill,
            "last_drill_version": self.last_drill_version,
            "last_drill_digest": self.last_drill_digest,
        }


@dataclass(frozen=True)
class RestoreDrill:
    record_version: str
    target_world_size: int
    steps: int
    phase: DrillPhase
    started_at: str
    finished_at: str | None = None
    proof_ref: str = ""
    reasons: tuple[Reason, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_version": self.record_version,
            "target_world_size": self.target_world_size,
            "steps": self.steps,
            "phase": self.phase.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "proof_ref": self.proof_ref,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


@dataclass(frozen=True)
class RescueDecision:
    event: str
    action: RescueAction
    reason: str
    code: str = ""
    target: str = ""
    proof_ref: str = ""
    decided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "action": self.action.value,
            "code": self.code,
            "reason": self.reason,
            "target": self.target,
            "proof_ref": self.proof_ref,
            "decided_at": self.decided_at,
        }
