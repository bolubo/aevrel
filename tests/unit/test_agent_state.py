from __future__ import annotations

from aevrel.agent.state import AgentStateError, TaskPhase, TaskStore


def _task(
    *,
    commit_token: str | None = None,
    target_root: str = "/tmp/target/rank0",
) -> dict:
    task = {
        "version": "1",
        "migration_uid": "m-1",
        "rank": 0,
        "task_hash": "sha256:" + "a" * 64,
        "target_root": target_root,
        "spec_ref": "spec.yaml",
        "plan_ref": "plan.json",
        "plan_slice": {"nodes": [], "bytes": 0},
        "state": "created",
    }
    if commit_token is not None:
        task["commit_token"] = commit_token
    return task


def test_create_is_idempotent(tmp_path) -> None:
    store = TaskStore(tmp_path)
    first = store.create(_task())
    second = store.create(_task())
    assert first.task_id == second.task_id
    assert len(store.list()) == 1


def test_transition_happy_path(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(_task())
    for target in [
        TaskPhase.PREPARING,
        TaskPhase.PREPARED,
        TaskPhase.TRANSFERRING,
        TaskPhase.STAGED,
        TaskPhase.COMMIT_READY,
        TaskPhase.COMMITTING,
        TaskPhase.COMMITTED,
    ]:
        task = store.transition(task.task_id, target)
    assert task.state is TaskPhase.COMMITTED


def test_invalid_transition_is_rejected(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(_task())
    try:
        store.transition(task.task_id, TaskPhase.COMMITTED)
    except AgentStateError:
        pass
    else:
        raise AssertionError("invalid transition must fail")


def test_interrupted_transfer_can_be_prepared_again(tmp_path) -> None:
    store = TaskStore(tmp_path)
    task = store.create(_task())
    task = store.transition(task.task_id, TaskPhase.PREPARING)
    task = store.transition(task.task_id, TaskPhase.PREPARED)
    task = store.transition(task.task_id, TaskPhase.TRANSFERRING)
    task = store.transition(task.task_id, TaskPhase.PREPARED)
    assert task.state is TaskPhase.PREPARED


def test_persistence_survives_reopen(tmp_path) -> None:
    first = TaskStore(tmp_path)
    task = first.create(_task())
    second = TaskStore(tmp_path)
    assert second.get(task.task_id).state is TaskPhase.CREATED
