"""Local restore-drill runner.

This adapter does not fake model steps. The supplied target command must load
the checkpoint, run the requested steps, and write a target-ready report.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from ..verification.target_report import (
    TargetReportError,
    load_target_ready_report,
)
from .errors import LocalError


def run_restore_drill(
    checkpoint: str | Path,
    *,
    target_message: str,
    target_world_size: int = 1,
    rank: int = 0,
    steps: int = 10,
    version: str = "",
    migration_uid: str = "",
    report_path: str | Path,
    timeout: float = 900.0,
    require_state_complete: bool = False,
    cwd: str | Path | None = None,
) -> dict[str, object]:
    checkpoint_path = Path(checkpoint).expanduser().resolve()
    report = Path(report_path).expanduser().resolve()
    if target_world_size < 1:
        raise LocalError("INVALID_SPEC", "target world size must be positive")
    if not 0 <= rank < target_world_size:
        raise LocalError("INVALID_SPEC", "rank is outside target world")
    if steps < 1:
        raise LocalError("INVALID_SPEC", "drill steps must be positive")
    if not target_message.strip():
        raise LocalError(
            "DRILL_COMMAND_REQUIRED",
            "restore drill requires a target training command",
        )

    migration_uid = migration_uid or checkpoint_path.name
    version = version or checkpoint_path.name
    values = {
        "checkpoint": str(checkpoint_path),
        "rank": str(rank),
        "version": version,
        "report": str(report),
    }
    try:
        command = shlex.split(target_message)
    except ValueError as exc:
        raise LocalError(
            "DRILL_COMMAND_INVALID",
            f"invalid target command: {exc}",
        ) from exc
    command = [
        item.replace("{checkpoint}", values["checkpoint"])
        .replace("{rank}", values["rank"])
        .replace("{version}", values["version"])
        .replace("{report}", values["report"])
        for item in command
    ]
    report.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "SHARDFERRY_CHECKPOINT": str(checkpoint_path),
            "SHARDFERRY_CHECKPOINT_TEMPLATE": str(checkpoint_path),
            "SHARDFERRY_RANK": str(rank),
            "SHARDFERRY_VERSION": version,
            "SHARDFERRY_TARGET_WORLD_SIZE": str(target_world_size),
            "SHARDFERRY_VALIDATION_STEPS": str(steps),
            "SHARDFERRY_MIGRATION_UID": migration_uid,
            "SHARDFERRY_REPORT_PATH": str(report),
            "JOB_COMPLETION_INDEX": str(rank),
        }
    )
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=Path(cwd) if cwd is not None else None,
            env=env,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalError(
            "DRILL_TIMEOUT",
            f"target command timed out after {timeout:.1f}s",
            retryable=True,
        ) from exc
    except OSError as exc:
        raise LocalError(
            "DRILL_COMMAND_FAILED",
            f"could not start target command: {exc}",
        ) from exc
    elapsed = time.perf_counter() - started
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise LocalError(
            "DRILL_FAILED",
            f"target command exited {completed.returncode}: {detail}",
            retryable=True,
        )
    try:
        payload = load_target_ready_report(report)
    except (OSError, TargetReportError, ValueError) as exc:
        raise LocalError(
            "DRILL_REPORT_INVALID",
            f"could not read drill report: {exc}",
        ) from exc
    if not payload["ok"]:
        raise LocalError(
            "DRILL_FAILED",
            f"target report is not ok: {payload.get('error', '')}",
        )
    if payload["migrationUID"] != migration_uid:
        raise LocalError(
            "DRILL_REPORT_INVALID",
            "target report migrationUID does not match",
        )
    if payload["version"] != version:
        raise LocalError(
            "DRILL_REPORT_INVALID",
            "target report version does not match",
        )
    if payload["rank"] != rank:
        raise LocalError(
            "DRILL_REPORT_INVALID",
            "target report rank does not match",
        )
    if not payload["modelLoaded"] or not payload["optimizerLoaded"]:
        raise LocalError(
            "DRILL_FAILED",
            "target did not load model and optimizer",
        )
    if payload["stepsRan"] < steps:
        raise LocalError(
            "DRILL_STEPS_INCOMPLETE",
            f"target ran {payload['stepsRan']} steps, need {steps}",
        )
    if require_state_complete and not payload["stateComplete"]:
        raise LocalError(
            "DRILL_STATE_INCOMPLETE",
            "target state closure is incomplete",
        )
    return {
        "status": "ok",
        "scope": "rank-local",
        "checkpoint": str(checkpoint_path),
        "report": str(report),
        "rank": rank,
        "target_world_size": target_world_size,
        "steps": steps,
        "duration_seconds": round(elapsed, 6),
        "proof": payload,
    }
