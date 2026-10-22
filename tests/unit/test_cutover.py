from __future__ import annotations

import pytest

from shardferry.cdp import (
    CheckpointFollower,
    FenceStore,
    VersionMarker,
    VersionTarget,
    write_marker,
)
from shardferry.cutover import (
    CutoverError,
    CutoverPolicy,
    CutoverRecoveryError,
    CutoverSession,
)


def make_version(root, version_id, files, *, parent_id=None) -> None:
    version_dir = root / version_id
    version_dir.mkdir(parents=True)
    for name, data in files.items():
        path = version_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    write_marker(
        version_dir,
        VersionMarker(
            version="1",
            version_id=version_id,
            job_uid="job-1",
            step=int(version_id.removeprefix("step-")),
            parent_id=parent_id,
            metadata_hash="sha256:" + "a" * 64,
            created_at=f"2026-10-22T00:00:{len(version_id):02d}Z",
            complete=True,
        ),
    )


class RecordingPauseHook:
    def __init__(self) -> None:
        self.events: list[tuple[str, str | None]] = []

    def pause(self, version_id: str) -> None:
        self.events.append(("pause", version_id))

    def resume(self) -> None:
        self.events.append(("resume", None))


class PauseFailureHook(RecordingPauseHook):
    def pause(self, version_id: str) -> None:
        super().pause(version_id)
        raise RuntimeError("injected pause failure")


class ResumeFailureHook(RecordingPauseHook):
    def resume(self) -> None:
        super().resume()
        raise RuntimeError("injected resume failure")


def test_cutover_pauses_catches_up_and_promotes(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(
        source,
        "step-200",
        {"a.bin": b"v2-data"},
        parent_id="step-100",
    )
    target = VersionTarget(tmp_path / "target")
    follower = CheckpointFollower(source, target, chunk_bytes=4)
    follower.sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5, max_lag_versions=1),
        chunk_bytes=4,
    )

    result = session.cutover("step-200", owner="controller-a", epoch=1)
    assert result.state == "promoted"
    assert result.version_id == "step-200"
    assert target.current().version_id == "step-200"
    assert hook.events == [("pause", "step-200")]
    assert FenceStore(target.root).current() == result.fence


def test_cutover_invokes_target_ready_hook(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    events: list[str] = []
    session = CutoverSession(
        source,
        target,
        target_ready=events.append,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )

    result = session.cutover("step-100", owner="controller-a", epoch=1)
    assert events == ["step-100"]
    assert result.target_ready_seconds >= 0


def test_cutover_rolls_back_when_target_ready_fails(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = RecordingPauseHook()

    def fail_ready(_version_id: str) -> None:
        raise RuntimeError("target ready failed")

    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        target_ready=fail_ready,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    with pytest.raises(RuntimeError, match="target ready failed"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert target.current() is None
    assert hook.events == [
        ("pause", "step-100"),
        ("resume", None),
    ]


def test_cutover_rejects_non_latest_before_pausing(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(
        source,
        "step-200",
        {"a.bin": b"v2-data"},
        parent_id="step-100",
    )
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(require_latest=True),
        chunk_bytes=4,
    )

    with pytest.raises(CutoverError, match="not the latest"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert hook.events == []
    assert target.current() is None


def test_cutover_resumes_source_when_promotion_fails(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )

    def fail_promote(*args, **kwargs):
        raise RuntimeError("injected promote failure")

    monkeypatch.setattr(target, "promote", fail_promote)
    with pytest.raises(RuntimeError, match="injected"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert hook.events == [
        ("pause", "step-100"),
        ("resume", None),
    ]
    assert target.current() is None


def test_cutover_aborts_before_promote_when_pause_budget_exceeded(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    times = iter([0.0, 10.0])
    monkeypatch.setattr("shardferry.cutover.monotonic", lambda: next(times))

    with pytest.raises(CutoverError, match="before promote"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert hook.events == [
        ("pause", "step-100"),
        ("resume", None),
    ]
    assert target.current() is None


def test_first_cutover_timeout_clears_current_pointer(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    times = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr("shardferry.cutover.monotonic", lambda: next(times))

    with pytest.raises(CutoverError, match="exceeded max pause"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert target.current() is None
    assert hook.events == [
        ("pause", "step-100"),
        ("resume", None),
    ]


def test_cutover_attempts_resume_when_pause_raises(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = PauseFailureHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )

    with pytest.raises(RuntimeError, match="pause failure"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert hook.events == [
        ("pause", "step-100"),
        ("resume", None),
    ]
    assert target.current() is None


def test_cutover_resumes_on_final_delta_failure(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )

    def fail_delta(*args, **kwargs):
        raise RuntimeError("injected final delta failure")

    monkeypatch.setattr(session.follower, "sync_once", fail_delta)
    with pytest.raises(RuntimeError, match="final delta"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert hook.events == [
        ("pause", "step-100"),
        ("resume", None),
    ]
    assert target.current() is None


def test_cutover_reports_resume_failure(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    hook = ResumeFailureHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )

    def fail_delta(*args, **kwargs):
        raise RuntimeError("injected final delta failure")

    monkeypatch.setattr(session.follower, "sync_once", fail_delta)
    with pytest.raises(CutoverRecoveryError) as error:
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert isinstance(error.value.original, RuntimeError)
    assert any("resume source" in item for item in error.value.recovery_errors)
    assert target.current() is None


def test_cutover_rolls_back_then_reports_resume_failure(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(source, "step-200", {"a.bin": b"v2-data"}, parent_id="step-100")
    target = VersionTarget(tmp_path / "target")
    follower = CheckpointFollower(source, target, chunk_bytes=4)
    follower.sync_once()
    target.promote(target.manifests.read("step-100"))
    hook = ResumeFailureHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    times = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr("shardferry.cutover.monotonic", lambda: next(times))

    with pytest.raises(CutoverRecoveryError) as error:
        session.cutover("step-200", owner="controller-a", epoch=1)
    assert any("resume source" in item for item in error.value.recovery_errors)
    assert target.current().version_id == "step-100"
    assert hook.events == [
        ("pause", "step-200"),
        ("resume", None),
    ]


def test_cutover_reports_rollback_failure(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(source, "step-200", {"a.bin": b"v2-data"}, parent_id="step-100")
    target = VersionTarget(tmp_path / "target")
    follower = CheckpointFollower(source, target, chunk_bytes=4)
    follower.sync_once()
    target.promote(target.manifests.read("step-100"))
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    times = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr("shardferry.cutover.monotonic", lambda: next(times))

    def fail_rollback(*args, **kwargs):
        raise RuntimeError("injected rollback failure")

    monkeypatch.setattr(target, "rollback", fail_rollback)
    with pytest.raises(CutoverRecoveryError) as error:
        session.cutover("step-200", owner="controller-a", epoch=1)
    assert any("restore target" in item for item in error.value.recovery_errors)
    assert target.current().version_id == "step-200"
    assert hook.events == [
        ("pause", "step-200"),
        ("resume", None),
    ]


def test_stale_controller_cannot_promote_after_newer_epoch(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    make_version(source, "step-200", {"a.bin": b"v2-data"}, parent_id="step-100")
    target = VersionTarget(tmp_path / "target")
    follower = CheckpointFollower(source, target, chunk_bytes=4)
    follower.sync_once()
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5, require_latest=False),
        chunk_bytes=4,
    )
    first = session.cutover("step-100", owner="controller-a", epoch=1)
    target.promote(target.manifests.read("step-200"))
    newer = FenceStore(target.root).acquire("controller-b", 2, "step-200")
    assert newer.epoch == 2

    with pytest.raises(Exception, match="stale"):
        target.promote(target.manifests.read("step-200"), fence=first.fence)
    assert target.current().version_id == "step-200"


def test_cutover_rejects_stale_fence_before_pausing(tmp_path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", {"a.bin": b"v1-data"})
    target = VersionTarget(tmp_path / "target")
    CheckpointFollower(source, target, chunk_bytes=4).sync_once()
    FenceStore(target.root).acquire("controller-a", 2, "step-100")
    hook = RecordingPauseHook()
    session = CutoverSession(
        source,
        target,
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )

    with pytest.raises(Exception, match="stale fence epoch"):
        session.cutover("step-100", owner="controller-a", epoch=1)
    assert hook.events == []
    assert target.current() is None
