from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..contracts import validate_task


class AgentStateError(RuntimeError):
    """Raised when an agent task request or transition is invalid."""


class TaskPhase(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    PREPARED = "prepared"
    TRANSFERRING = "transferring"
    STAGED = "staged"
    COMMIT_READY = "commit-ready"
    COMMITTING = "committing"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMMIT_INCOMPLETE = "commit-incomplete"


@dataclass(frozen=True)
class TaskRecord:
    version: str
    task_id: str
    migration_uid: str
    commit_token: str | None
    rank: int
    task_hash: str
    target_root: str
    spec_ref: str
    plan_ref: str
    plan_slice: dict[str, Any]
    state: TaskPhase
    attempts: int
    error: str | None
    created_at: str
    updated_at: str
    report: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        if payload.get("commit_token") is None:
            payload.pop("commit_token", None)
        return payload


TRANSITIONS: dict[TaskPhase, frozenset[TaskPhase]] = {
    TaskPhase.CREATED: frozenset({TaskPhase.PREPARING, TaskPhase.CANCELLED}),
    TaskPhase.PREPARING: frozenset(
        {TaskPhase.PREPARED, TaskPhase.FAILED, TaskPhase.CANCELLED}
    ),
    TaskPhase.PREPARED: frozenset(
        {TaskPhase.TRANSFERRING, TaskPhase.FAILED, TaskPhase.CANCELLED}
    ),
    TaskPhase.TRANSFERRING: frozenset(
        {
            TaskPhase.PREPARED,
            TaskPhase.STAGED,
            TaskPhase.FAILED,
            TaskPhase.CANCELLED,
        }
    ),
    TaskPhase.STAGED: frozenset(
        {TaskPhase.COMMIT_READY, TaskPhase.FAILED, TaskPhase.CANCELLED}
    ),
    TaskPhase.COMMIT_READY: frozenset(
        {TaskPhase.COMMITTING, TaskPhase.FAILED, TaskPhase.CANCELLED}
    ),
    TaskPhase.COMMITTING: frozenset(
        {TaskPhase.COMMITTED, TaskPhase.COMMIT_INCOMPLETE}
    ),
    TaskPhase.COMMIT_INCOMPLETE: frozenset(
        {TaskPhase.COMMITTING, TaskPhase.FAILED}
    ),
    TaskPhase.COMMITTED: frozenset(),
    TaskPhase.CANCELLED: frozenset(),
    TaskPhase.FAILED: frozenset({TaskPhase.PREPARING}),
}


class TaskStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir).resolve()
        self.tasks_dir = self.root / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, TaskRecord] = {}
        for path in sorted(self.tasks_dir.glob("*.json")):
            record = self._load(path)
            self._tasks[record.task_id] = record

    def create(self, payload: dict[str, Any]) -> TaskRecord:
        validate_task(payload)
        if payload["state"] != TaskPhase.CREATED.value:
            raise AgentStateError("new tasks must start in created state")
        task_id = _task_id(payload)
        commit_token = payload.get("commit_token")
        if commit_token is not None and not isinstance(commit_token, str):
            raise AgentStateError("commit_token must be a string")
        existing = self._tasks.get(task_id)
        if existing is not None:
            if (
                existing.target_root != payload["target_root"]
                or existing.spec_ref != payload["spec_ref"]
                or existing.plan_ref != payload["plan_ref"]
                or existing.plan_slice != payload["plan_slice"]
            ):
                raise AgentStateError(
                    f"task id {task_id} already has different content"
                )
            return existing
        now = _now()
        record = TaskRecord(
            version="1",
            task_id=task_id,
            migration_uid=payload["migration_uid"],
            commit_token=commit_token,
            rank=payload["rank"],
            task_hash=payload["task_hash"],
            target_root=payload["target_root"],
            spec_ref=payload["spec_ref"],
            plan_ref=payload["plan_ref"],
            plan_slice=payload["plan_slice"],
            state=TaskPhase.CREATED,
            attempts=0,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self._persist(record)
        self._tasks[task_id] = record
        return record

    def get(self, task_id: str) -> TaskRecord:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise AgentStateError(f"task not found: {task_id}") from exc

    def list(self) -> list[TaskRecord]:
        return sorted(
            self._tasks.values(),
            key=lambda task: (task.created_at, task.task_id),
        )

    def transition(
        self,
        task_id: str,
        target: TaskPhase,
        *,
        error: str | None = None,
    ) -> TaskRecord:
        task = self.get(task_id)
        if target is task.state:
            return task
        if target not in TRANSITIONS[task.state]:
            raise AgentStateError(
                f"invalid task transition: {task.state.value} -> {target.value}"
            )
        attempts = task.attempts + (1 if target is TaskPhase.PREPARING else 0)
        record = replace(
            task,
            state=target,
            attempts=attempts,
            error=error if error is not None else task.error,
            updated_at=_now(),
        )
        self._persist(record)
        self._tasks[task_id] = record
        return record

    def fail(self, task_id: str, error: str) -> TaskRecord:
        return self.transition(task_id, TaskPhase.FAILED, error=error)

    def set_report(self, task_id: str, report: dict[str, Any]) -> TaskRecord:
        task = self.get(task_id)
        record = replace(task, report=report, updated_at=_now())
        self._persist(record)
        self._tasks[task_id] = record
        return record

    @contextmanager
    def commit_guard(self, task_id: str):
        yield self.get(task_id)

    def _persist(self, record: TaskRecord) -> None:
        path = self.tasks_dir / f"{record.task_id}.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(record.to_dict(), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(self.tasks_dir)
        except OSError as exc:
            raise AgentStateError(f"could not persist task {record.task_id}: {exc}") from exc

    def _load(self, path: Path) -> TaskRecord:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TaskRecord(
                version=payload["version"],
                task_id=payload["task_id"],
                migration_uid=payload["migration_uid"],
                commit_token=payload.get("commit_token"),
                rank=int(payload["rank"]),
                task_hash=payload["task_hash"],
                target_root=payload["target_root"],
                spec_ref=payload["spec_ref"],
                plan_ref=payload["plan_ref"],
                plan_slice=payload["plan_slice"],
                state=TaskPhase(payload["state"]),
                attempts=int(payload["attempts"]),
                error=payload.get("error"),
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                report=payload.get("report"),
            )
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise AgentStateError(f"could not load task {path}: {exc}") from exc


def _task_id(payload: dict[str, Any]) -> str:
    key = "\0".join(
        [
            payload["migration_uid"],
            payload.get("commit_token") or "",
            str(payload["rank"]),
            payload["task_hash"],
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
