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


SCHEMA_VERSION = "1"
DEFAULT_REPORT_PATH = "/dev/termination-log"


class TargetReportError(ValueError):
    """Raised when a target-ready report is incomplete or inconsistent."""


@dataclass(frozen=True)
class TargetReadyReport:
    migration_uid: str
    version: str
    rank: int
    role: str
    model_loaded: bool
    optimizer_loaded: bool
    steps_ran: int
    state_complete: bool = False
    ok: bool = True
    error: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
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

    def to_dict(self) -> dict[str, Any]:
        return {
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
    value = path or os.environ.get("SHARDFERRY_REPORT_PATH")
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


def write_target_ready_report(
    *,
    model_loaded: bool,
    optimizer_loaded: bool,
    steps_ran: int,
    migration_uid: str | None = None,
    version: str | None = None,
    rank: int | None = None,
    role: str = "worker",
    state_complete: bool = False,
    ok: bool = True,
    error: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    migration_uid = migration_uid or os.environ.get(
        "SHARDFERRY_MIGRATION_UID",
        "",
    )
    version = version or os.environ.get("SHARDFERRY_VERSION", "")
    report = TargetReadyReport(
        migration_uid=migration_uid,
        version=version,
        rank=environment_rank() if rank is None else rank,
        role=role,
        model_loaded=bool(model_loaded),
        optimizer_loaded=bool(optimizer_loaded),
        steps_ran=int(steps_ran),
        state_complete=bool(state_complete),
        ok=bool(ok),
        error=error,
    )
    report.write(path)
    return report.to_dict()


def load_target_ready_report(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TargetReportError("target-ready report must be a JSON object")
    required = {
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
    missing = sorted(required - set(payload))
    extra = sorted(set(payload) - required)
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
        ok=payload["ok"],
        error=payload["error"],
        schema_version=payload["schemaVersion"],
    )
    return report.to_dict()
