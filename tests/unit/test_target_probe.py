"""Polling, deadline and failure mapping of ``aevrel.target_probe``.

The poll loop reads ``time.monotonic`` and calls ``time.sleep`` through the
module's own ``time`` reference. The tests swap that reference for a fake
clock, so no test waits for real time and attempts are counted, not timed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest

from aevrel import target_probe
from aevrel.cdp import (
    CDPError,
    ChunkRef,
    VersionManifest,
    VersionTarget,
    manifest_root,
)
from aevrel.target_probe import (
    TargetProbeError,
    check_target,
    main,
    wait_for_target,
)


class FakeClock:
    """Stands in for the ``time`` module inside ``target_probe``."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedProbe:
    """Replaces ``check_target``: plays back one outcome per attempt.

    An exception instance is raised, anything else is returned. The last
    outcome repeats once the script runs out.
    """

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[object, str]] = []

    def __call__(self, target_root: object, version_id: str) -> object:
        self.calls.append((target_root, version_id))
        index = min(len(self.calls), len(self.outcomes)) - 1
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


READY = {
    "version_id": "v1",
    "manifest_root": "sha256:" + "0" * 64,
    "chunks": 1,
    "shadow": "/target/shadow/v1",
}


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(target_probe, "time", fake)
    return fake


def _install(monkeypatch: pytest.MonkeyPatch, probe: ScriptedProbe) -> None:
    monkeypatch.setattr(target_probe, "check_target", probe)


# --- wait_for_target: the poll loop -----------------------------------------


def test_ready_on_first_attempt_does_not_sleep(monkeypatch, clock) -> None:
    probe = ScriptedProbe(READY)
    _install(monkeypatch, probe)

    result = wait_for_target("/target", "v1", timeout=5.0, poll_interval=0.5)

    assert result == READY
    assert probe.calls == [("/target", "v1")]
    assert clock.sleeps == []


def test_ready_after_n_attempts_counts_attempts(monkeypatch, clock) -> None:
    probe = ScriptedProbe(
        TargetProbeError("target has no current version"),
        TargetProbeError("target has no current version"),
        TargetProbeError("target shadow is missing: /target/shadow/v1"),
        READY,
    )
    _install(monkeypatch, probe)

    result = wait_for_target("/target", "v1", timeout=5.0, poll_interval=0.25)

    assert result == READY
    assert len(probe.calls) == 4
    # One sleep between consecutive attempts, never one after success.
    assert clock.sleeps == [0.25, 0.25, 0.25]


def test_poll_interval_is_passed_to_sleep(monkeypatch, clock) -> None:
    probe = ScriptedProbe(TargetProbeError("not yet"), READY)
    _install(monkeypatch, probe)

    wait_for_target("/target", "v1", timeout=60.0, poll_interval=7.5)

    assert clock.sleeps == [7.5]


def test_zero_poll_interval_still_polls(monkeypatch, clock) -> None:
    probe = ScriptedProbe(TargetProbeError("not yet"), READY)
    _install(monkeypatch, probe)

    result = wait_for_target("/target", "v1", timeout=1.0, poll_interval=0)
    assert result == READY
    assert len(probe.calls) == 2
    assert clock.sleeps == [0]


def test_deadline_raises_timeout_and_stops_polling(monkeypatch, clock) -> None:
    last = TargetProbeError("target CAS is missing 3 chunk(s)")
    probe = ScriptedProbe(last)
    _install(monkeypatch, probe)

    with pytest.raises(TargetProbeError) as caught:
        wait_for_target("/target", "v1", timeout=1.0, poll_interval=0.25)

    # Attempts at t=0, 0.25, 0.5, 0.75 and 1.0; the one at the deadline is
    # the last, and no sleep follows it.
    assert len(probe.calls) == 5
    assert clock.sleeps == [0.25, 0.25, 0.25, 0.25]
    assert str(caught.value) == (
        "target did not become ready within 1.000s: "
        "target CAS is missing 3 chunk(s)"
    )
    assert caught.value.__cause__ is last


def test_deadline_reports_the_last_failure_not_the_first(
    monkeypatch, clock
) -> None:
    first = TargetProbeError("target has no current version")
    later = TargetProbeError("target current is v0, expected v1")
    probe = ScriptedProbe(first, later)
    _install(monkeypatch, probe)

    with pytest.raises(TargetProbeError) as caught:
        wait_for_target("/target", "v1", timeout=0.5, poll_interval=0.5)

    assert len(probe.calls) == 2
    assert caught.value.__cause__ is later
    assert "target current is v0, expected v1" in str(caught.value)


def test_slow_probe_counts_against_the_deadline(monkeypatch, clock) -> None:
    """The deadline is wall time, not attempts times interval."""

    def slow_probe(target_root: object, version_id: str) -> object:
        slow_probe.calls += 1
        clock.now += 2.0
        raise TargetProbeError("not yet")

    slow_probe.calls = 0
    monkeypatch.setattr(target_probe, "check_target", slow_probe)

    with pytest.raises(TargetProbeError, match="within 3.000s"):
        wait_for_target("/target", "v1", timeout=3.0, poll_interval=0.1)

    assert slow_probe.calls == 2
    assert clock.sleeps == [0.1]


def test_cdp_error_is_retried_and_kept_as_the_cause(
    monkeypatch, clock
) -> None:
    """A storage-level CDPError (unreadable pointer or manifest) is retried.

    It is retried like "not ready", but it stays distinguishable: the
    timeout error chains the original exception with its own type.
    """
    torn = CDPError("could not read current version: Expecting value")
    probe = ScriptedProbe(torn, READY)
    _install(monkeypatch, probe)

    result = wait_for_target("/target", "v1", timeout=5.0, poll_interval=0.25)
    assert result == READY
    assert len(probe.calls) == 2

    probe = ScriptedProbe(torn)
    _install(monkeypatch, probe)
    with pytest.raises(TargetProbeError) as caught:
        wait_for_target("/target", "v1", timeout=0.5, poll_interval=0.25)
    assert type(caught.value.__cause__) is CDPError
    assert caught.value.__cause__ is torn


def test_not_ready_cause_is_a_probe_error_not_a_cdp_error(
    monkeypatch, clock
) -> None:
    probe = ScriptedProbe(TargetProbeError("target has no current version"))
    _install(monkeypatch, probe)

    with pytest.raises(TargetProbeError) as caught:
        wait_for_target("/target", "v1", timeout=0.5, poll_interval=0.25)

    assert type(caught.value.__cause__) is TargetProbeError


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("permission denied: /target/current"),
        OSError("transport endpoint is not connected"),
        KeyError("chunks"),
    ],
)
def test_transport_error_propagates_without_retry(
    monkeypatch, clock, error
) -> None:
    probe = ScriptedProbe(error, READY)
    _install(monkeypatch, probe)

    with pytest.raises(type(error)) as caught:
        wait_for_target("/target", "v1", timeout=60.0, poll_interval=0.1)

    assert caught.value is error
    assert len(probe.calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("timeout", "poll_interval", "message"),
    [
        (0.0, 0.1, "timeout must be positive"),
        (-1.0, 0.1, "timeout must be positive"),
        (1.0, -0.1, "poll_interval must be non-negative"),
    ],
)
def test_invalid_timing_is_rejected_before_probing(
    monkeypatch, clock, timeout, poll_interval, message
) -> None:
    probe = ScriptedProbe(READY)
    _install(monkeypatch, probe)

    with pytest.raises(ValueError, match=message):
        wait_for_target(
            "/target", "v1", timeout=timeout, poll_interval=poll_interval
        )

    assert probe.calls == []


def test_defaults_are_unchanged() -> None:
    defaults = wait_for_target.__kwdefaults__
    assert defaults == {"timeout": 30.0, "poll_interval": 0.1}


# --- check_target: what "not ready" means ------------------------------------


def _promote(
    root: Path,
    version_id: str,
    files: dict[str, bytes],
) -> VersionManifest:
    """Promote a version with one whole-file chunk per file.

    Chunks are written straight into the CAS so the test does not need the
    native chunker.
    """
    target = VersionTarget(root)
    chunks = []
    for name, data in sorted(files.items()):
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        path = target.chunks.path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        chunks.append(
            ChunkRef(
                digest=digest,
                size=len(data),
                file=name,
                offset=0,
                length=len(data),
            )
        )
    fields = {
        "version_id": version_id,
        "parent_id": None,
        "metadata_hash": "sha256:" + "a" * 64,
        "created_at": "2026-01-01T00:00:00+00:00",
        "chunks": tuple(chunks),
    }
    manifest = VersionManifest(version="1", root=manifest_root(**fields), **fields)
    target.promote(manifest)
    return manifest


FILES = {"model/rank0.bin": b"rank-zero-bytes", "optim/rank0.bin": b"optimizer"}


def test_check_target_reports_a_ready_target(tmp_path: Path) -> None:
    manifest = _promote(tmp_path, "step/100", FILES)

    result = check_target(tmp_path, "step/100")

    assert result == {
        "version_id": "step/100",
        "manifest_root": manifest.root,
        "chunks": 2,
        "shadow": str(
            VersionTarget(tmp_path).shadow / quote("step/100", safe="")
        ),
    }


def test_check_target_without_current_version(tmp_path: Path) -> None:
    with pytest.raises(TargetProbeError, match="target has no current version"):
        check_target(tmp_path, "v1")


def test_check_target_with_another_current_version(tmp_path: Path) -> None:
    _promote(tmp_path, "v1", FILES)

    with pytest.raises(
        TargetProbeError, match="target current is v1, expected v2"
    ):
        check_target(tmp_path, "v2")


def test_check_target_with_root_mismatch(tmp_path: Path) -> None:
    _promote(tmp_path, "v1", FILES)
    current = tmp_path / "current"
    pointer = json.loads(current.read_text(encoding="utf-8"))
    pointer["root"] = "sha256:" + "f" * 64
    current.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(
        TargetProbeError, match="target current root does not match manifest"
    ):
        check_target(tmp_path, "v1")


def test_check_target_with_missing_shadow(tmp_path: Path) -> None:
    _promote(tmp_path, "v1", FILES)
    shutil.rmtree(VersionTarget(tmp_path).shadow / "v1")

    with pytest.raises(TargetProbeError, match="target shadow is missing: "):
        check_target(tmp_path, "v1")


def test_check_target_with_missing_cas_chunk(tmp_path: Path) -> None:
    manifest = _promote(tmp_path, "v1", FILES)
    target = VersionTarget(tmp_path)
    for chunk in manifest.chunks:
        # The shadow may hard-link the CAS object; replace the shadow file
        # with a copy so removing the CAS object leaves the shadow intact.
        shadow_file = target.shadow / "v1" / chunk.file
        data = shadow_file.read_bytes()
        shadow_file.unlink()
        shadow_file.write_bytes(data)
        target.chunks.path(chunk.digest).unlink()

    with pytest.raises(
        TargetProbeError, match=r"target CAS is missing 2 chunk\(s\)"
    ):
        check_target(tmp_path, "v1")


def test_check_target_with_missing_shadow_file(tmp_path: Path) -> None:
    _promote(tmp_path, "v1", FILES)
    (VersionTarget(tmp_path).shadow / "v1" / "optim" / "rank0.bin").unlink()

    with pytest.raises(
        TargetProbeError, match=r"target shadow is missing 1 file\(s\)"
    ):
        check_target(tmp_path, "v1")


def test_check_target_lets_a_torn_pointer_surface_as_cdp_error(
    tmp_path: Path,
) -> None:
    _promote(tmp_path, "v1", FILES)
    (tmp_path / "current").write_text("{", encoding="utf-8")

    with pytest.raises(CDPError, match="could not read current version"):
        check_target(tmp_path, "v1")


def test_wait_for_target_polls_a_real_target_until_promoted(
    tmp_path: Path, monkeypatch, clock
) -> None:
    """End to end: the version is promoted between two polls."""
    attempts = []
    real_check = target_probe.check_target

    def check_then_promote(target_root, version_id):
        attempts.append(version_id)
        try:
            return real_check(target_root, version_id)
        finally:
            if len(attempts) == 2:
                _promote(tmp_path, "v1", FILES)

    monkeypatch.setattr(target_probe, "check_target", check_then_promote)

    result = wait_for_target(tmp_path, "v1", timeout=5.0, poll_interval=0.5)

    assert result["version_id"] == "v1"
    assert len(attempts) == 3
    assert clock.sleeps == [0.5, 0.5]


# --- main: exit codes and output --------------------------------------------


def test_main_requires_a_target(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version-id", "v1"])
    assert caught.value.code == 2
    assert "one of --target-root or --dcp-checkpoint is required" in (
        capsys.readouterr().err
    )


def test_main_rejects_rank_without_dcp_checkpoint(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "--target-root",
                "/target",
                "--version-id",
                "v1",
                "--dcp-rank",
                "0",
            ]
        )
    assert caught.value.code == 2
    assert "--dcp-rank requires --dcp-checkpoint" in capsys.readouterr().err


def test_main_passes_timeout_and_interval_to_the_wait(monkeypatch, capsys) -> None:
    seen = {}

    def fake_wait(target_root, version_id, *, timeout, poll_interval):
        seen.update(
            target_root=target_root,
            version_id=version_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        return dict(READY)

    monkeypatch.setattr(target_probe, "wait_for_target", fake_wait)

    code = main(
        [
            "--target-root",
            "/target",
            "--version-id",
            "v1",
            "--timeout",
            "4.5",
            "--poll-interval",
            "0.75",
        ]
    )

    assert code == 0
    assert seen == {
        "target_root": "/target",
        "version_id": "v1",
        "timeout": 4.5,
        "poll_interval": 0.75,
    }
    assert capsys.readouterr().out == "target ready: v1 (1 chunks)\n"


def test_main_uses_the_default_timing(monkeypatch) -> None:
    seen = {}

    def fake_wait(target_root, version_id, *, timeout, poll_interval):
        seen.update(timeout=timeout, poll_interval=poll_interval)
        return dict(READY)

    monkeypatch.setattr(target_probe, "wait_for_target", fake_wait)

    assert main(["--target-root", "/target", "--version-id", "v1"]) == 0
    assert seen == {"timeout": 30.0, "poll_interval": 0.1}


def test_main_prints_json_for_a_ready_target(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        target_probe, "wait_for_target", lambda *args, **kwargs: dict(READY)
    )

    code = main(["--target-root", "/target", "--version-id", "v1", "--json"])

    assert code == 0
    assert json.loads(capsys.readouterr().out) == READY


def test_main_maps_a_timeout_to_exit_code_2(monkeypatch, clock, capsys) -> None:
    probe = ScriptedProbe(TargetProbeError("target has no current version"))
    _install(monkeypatch, probe)

    code = main(
        [
            "--target-root",
            "/target",
            "--version-id",
            "v1",
            "--timeout",
            "0.75",
            "--poll-interval",
            "0.25",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == (
        "aevrel target-probe: target did not become ready within 0.750s: "
        "target has no current version\n"
    )
    assert len(probe.calls) == 4


def test_main_maps_a_transport_error_to_exit_code_2_without_retry(
    monkeypatch, clock, capsys
) -> None:
    probe = ScriptedProbe(OSError("transport endpoint is not connected"), READY)
    _install(monkeypatch, probe)

    code = main(["--target-root", "/target", "--version-id", "v1"])

    assert code == 2
    assert len(probe.calls) == 1
    assert clock.sleeps == []
    assert capsys.readouterr().err == (
        "aevrel target-probe: transport endpoint is not connected\n"
    )


def test_main_maps_invalid_timing_to_exit_code_2(
    monkeypatch, clock, capsys
) -> None:
    probe = ScriptedProbe(READY)
    _install(monkeypatch, probe)

    code = main(
        ["--target-root", "/target", "--version-id", "v1", "--timeout", "0"]
    )

    assert code == 2
    assert probe.calls == []
    assert capsys.readouterr().err == (
        "aevrel target-probe: timeout must be positive\n"
    )


def test_main_does_not_swallow_unexpected_errors(monkeypatch, clock) -> None:
    probe = ScriptedProbe(KeyError("chunks"))
    _install(monkeypatch, probe)

    with pytest.raises(KeyError):
        main(["--target-root", "/target", "--version-id", "v1"])
    assert len(probe.calls) == 1
