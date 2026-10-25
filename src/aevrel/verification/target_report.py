"""Per-rank target-ready report contract.

The controller reads this JSON from each validation Pod's termination
message. Training code only needs to call ``write_target_ready_report`` after
it has loaded model/optimizer state and completed the required steps.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..state import ResumeStateManifest, classify_resume_state


SCHEMA_VERSION = "2"
SUPPORTED_SCHEMA_VERSIONS = ("1", "2")
RESUME_STATE_CLOSURES = ("COMPLETE", "PARTIAL", "MISSING", "MALFORMED")
DEFAULT_REPORT_PATH = "/dev/termination-log"


class TargetReportError(ValueError):
    """Raised when a target-ready report is incomplete or inconsistent."""


@dataclass(frozen=True)
class ComponentEvidence:
    """Per-component presence as declared by the resume-state manifest."""

    kind: str
    required: bool
    present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "required": self.required,
            "present": self.present,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ComponentEvidence":
        return cls(
            kind=str(payload["kind"]),
            required=bool(payload["required"]),
            present=bool(payload["present"]),
        )


@dataclass(frozen=True)
class ResumeStateEvidence:
    """Binding of a readiness claim to one resume-state manifest.

    The manifest digest is the only binding point: component digests stay in
    the manifest, so a report can never disagree with itself.
    """

    manifest_digest: str
    closure: str
    components: tuple[ComponentEvidence, ...]

    def __post_init__(self) -> None:
        if not self.manifest_digest.startswith("sha256:"):
            raise TargetReportError("resumeState.manifestDigest must be sha256")
        if self.closure not in RESUME_STATE_CLOSURES:
            raise TargetReportError(
                f"unsupported resumeState closure: {self.closure}"
            )

    @classmethod
    def from_manifest(cls, manifest: ResumeStateManifest) -> "ResumeStateEvidence":
        """Derive evidence from a manifest, classifying it on the spot.

        Deriving the closure here (instead of accepting a caller-supplied
        value) makes a digest/closure disagreement impossible.
        """
        report = classify_resume_state(manifest.to_dict())
        return cls(
            manifest_digest=manifest.compute_digest(),
            closure=report.status,
            components=tuple(
                ComponentEvidence(
                    kind=component.kind,
                    required=component.required,
                    present=component.present,
                )
                for component in manifest.components
            ),
        )

    @property
    def complete(self) -> bool:
        return self.closure == "COMPLETE"

    def missing_required(self) -> tuple[str, ...]:
        return tuple(
            component.kind
            for component in self.components
            if component.required and not component.present
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifestDigest": self.manifest_digest,
            "closure": self.closure,
            "components": [
                component.to_dict() for component in self.components
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResumeStateEvidence":
        return cls(
            manifest_digest=str(payload["manifestDigest"]),
            closure=str(payload["closure"]),
            components=tuple(
                ComponentEvidence.from_dict(item)
                for item in payload["components"]
            ),
        )


@dataclass(frozen=True)
class TargetReadyReport:
    migration_uid: str
    version: str
    rank: int
    role: str
    model_loaded: bool
    optimizer_loaded: bool
    steps_ran: int
    state_complete: bool | None = None
    resume_state: ResumeStateEvidence | None = None
    ok: bool = True
    error: str = ""
    # The world size this rank was launched for. A rank knows its own launch
    # topology (AEVREL_TARGET_WORLD_SIZE) but cannot observe its peers, so the
    # expected count is the only coverage fact a single report can honestly
    # state; how many actually reported is counted by the aggregator.
    # None means the producer did not declare it.
    ranks_expected: int | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.state_complete is None:
            # Derive the legacy flag instead of letting callers restate what
            # the evidence already proves; a report without evidence can
            # never claim completeness.
            object.__setattr__(
                self,
                "state_complete",
                (
                    self.resume_state.complete
                    if self.resume_state is not None
                    else False
                ),
            )
        self.validate()

    def validate(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise TargetReportError(
                f"unsupported schemaVersion: {self.schema_version}"
            )
        if not isinstance(self.migration_uid, str) or not self.migration_uid:
            raise TargetReportError("migrationUID is required")
        if not isinstance(self.version, str) or not self.version:
            raise TargetReportError("version is required")
        if not isinstance(self.rank, int) or isinstance(self.rank, bool):
            raise TargetReportError("rank must be an integer")
        if self.rank < 0:
            raise TargetReportError("rank must be non-negative")
        if not isinstance(self.role, str) or not self.role.strip():
            raise TargetReportError("role is required")
        if not isinstance(self.steps_ran, int) or isinstance(self.steps_ran, bool):
            raise TargetReportError("stepsRan must be an integer")
        if self.steps_ran < 0:
            raise TargetReportError("stepsRan must be non-negative")
        if self.ranks_expected is not None:
            if not isinstance(self.ranks_expected, int) or isinstance(
                self.ranks_expected, bool
            ):
                raise TargetReportError("ranksExpected must be an integer")
            if self.ranks_expected < 1:
                raise TargetReportError("ranksExpected must be positive")
        for name, value in (
            ("modelLoaded", self.model_loaded),
            ("optimizerLoaded", self.optimizer_loaded),
            ("stateComplete", self.state_complete),
            ("ok", self.ok),
        ):
            if not isinstance(value, bool):
                raise TargetReportError(f"{name} must be a boolean")
        if not isinstance(self.error, str):
            raise TargetReportError("error must be a string")
        if self.ok and self.error:
            raise TargetReportError("ok report must not contain an error")
        if self.schema_version == SCHEMA_VERSION:
            if not isinstance(self.resume_state, ResumeStateEvidence):
                raise TargetReportError(
                    "schemaVersion 2 requires resumeState evidence"
                )
            if self.state_complete != self.resume_state.complete:
                raise TargetReportError(
                    "stateComplete disagrees with resumeState.closure"
                )
        elif self.resume_state is not None:
            raise TargetReportError(
                "schemaVersion 1 must not carry resumeState evidence"
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "migrationUID": self.migration_uid,
            "version": self.version,
            "rank": self.rank,
            "role": self.role,
            "modelLoaded": self.model_loaded,
            "optimizerLoaded": self.optimizer_loaded,
            "stepsRan": self.steps_ran,
            "stateComplete": self.state_complete,
            "ok": self.ok,
            "error": self.error,
        }
        if self.resume_state is not None:
            payload["resumeState"] = self.resume_state.to_dict()
        if self.ranks_expected is not None:
            payload["ranksExpected"] = self.ranks_expected
        return payload

    def write(self, path: str | Path | None = None) -> Path:
        destination = resolve_report_path(path)
        payload = json.dumps(
            self.to_dict(),
            indent=2,
            sort_keys=True,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The Kubernetes termination log is a mounted special file; writing
        # directly is required. Normal files use a same-directory replace so
        # an interrupted write cannot look like a valid report.
        if str(destination).startswith("/dev/") or not destination.parent.is_dir():
            destination.write_text(payload + "\n", encoding="utf-8")
            return destination
        temporary = destination.with_name(f".{destination.name}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        return destination


def resolve_report_path(path: str | Path | None = None) -> Path:
    value = path or os.environ.get("AEVREL_REPORT_PATH")
    if value is None:
        return Path(DEFAULT_REPORT_PATH)
    if not str(value).strip():
        raise TargetReportError("target-ready report path is empty")
    return Path(value).expanduser()


def environment_rank() -> int:
    value = os.environ.get("JOB_COMPLETION_INDEX", "0").strip() or "0"
    try:
        rank = int(value)
    except ValueError as exc:
        raise TargetReportError(
            f"JOB_COMPLETION_INDEX is not an integer: {value!r}"
        ) from exc
    if rank < 0:
        raise TargetReportError("JOB_COMPLETION_INDEX must be non-negative")
    return rank


def environment_world_size() -> int | None:
    """Return AEVREL_TARGET_WORLD_SIZE, or None when it is not set.

    A malformed value is an error rather than a silent None: a rank that
    cannot state the topology it was launched for cannot contribute a
    coverage claim.
    """
    value = os.environ.get("AEVREL_TARGET_WORLD_SIZE", "").strip()
    if not value:
        return None
    try:
        world_size = int(value)
    except ValueError as exc:
        raise TargetReportError(
            f"AEVREL_TARGET_WORLD_SIZE is not an integer: {value!r}"
        ) from exc
    if world_size < 1:
        raise TargetReportError("AEVREL_TARGET_WORLD_SIZE must be positive")
    return world_size


def write_target_ready_report(
    *,
    model_loaded: bool,
    optimizer_loaded: bool,
    steps_ran: int,
    resume_state: ResumeStateEvidence,
    migration_uid: str | None = None,
    version: str | None = None,
    rank: int | None = None,
    role: str = "worker",
    ok: bool = True,
    error: str = "",
    path: str | Path | None = None,
    ranks_expected: int | None = None,
) -> dict[str, Any]:
    migration_uid = migration_uid or os.environ.get(
        "AEVREL_MIGRATION_UID",
        "",
    )
    version = version or os.environ.get("AEVREL_VERSION", "")
    if ranks_expected is None:
        ranks_expected = environment_world_size()
    report = TargetReadyReport(
        migration_uid=migration_uid,
        version=version,
        rank=environment_rank() if rank is None else rank,
        role=role,
        model_loaded=bool(model_loaded),
        optimizer_loaded=bool(optimizer_loaded),
        steps_ran=int(steps_ran),
        state_complete=resume_state.complete,
        resume_state=resume_state,
        ok=bool(ok),
        error=error,
        ranks_expected=ranks_expected,
    )
    report.write(path)
    return report.to_dict()


def load_target_ready_report(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TargetReportError("target-ready report must be a JSON object")
    version = payload.get("schemaVersion")
    base_fields = {
        "schemaVersion",
        "migrationUID",
        "version",
        "rank",
        "role",
        "modelLoaded",
        "optimizerLoaded",
        "stepsRan",
        "stateComplete",
        "ok",
        "error",
    }
    if version == "1":
        required = base_fields
    elif version == SCHEMA_VERSION:
        required = base_fields | {"resumeState"}
    else:
        raise TargetReportError(f"unsupported schemaVersion: {version!r}")
    # ranksExpected is additive and optional: reports written before it
    # existed stay valid, and a reader that needs coverage treats its absence
    # as "not declared" rather than as full coverage.
    allowed = required | {"ranksExpected"}
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - allowed)
    if missing:
        raise TargetReportError(
            f"target-ready report is missing fields: {', '.join(missing)}"
        )
    if extra:
        raise TargetReportError(
            f"target-ready report has unknown fields: {', '.join(extra)}"
        )
    report = TargetReadyReport(
        migration_uid=payload["migrationUID"],
        version=payload["version"],
        rank=payload["rank"],
        role=payload["role"],
        model_loaded=payload["modelLoaded"],
        optimizer_loaded=payload["optimizerLoaded"],
        steps_ran=payload["stepsRan"],
        state_complete=payload["stateComplete"],
        resume_state=(
            ResumeStateEvidence.from_dict(payload["resumeState"])
            if version == SCHEMA_VERSION
            else None
        ),
        ok=payload["ok"],
        error=payload["error"],
        ranks_expected=payload.get("ranksExpected"),
        schema_version=version,
    )
    return report.to_dict()
