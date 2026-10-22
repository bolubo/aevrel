from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shardferry.cdp import VersionMarker, write_marker
from shardferry.cutover import CutoverError
from shardferry.cutover_service import CutoverService
from shardferry.handoff import CommandPauseHook


def make_version(root: Path, version_id: str, data: bytes) -> None:
    version = root / version_id
    version.mkdir(parents=True)
    (version / "a.bin").write_bytes(data)
    write_marker(
        version,
        VersionMarker(
            version="1",
            version_id=version_id,
            job_uid="job-1",
            step=100,
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )


def test_command_pause_hook_substitutes_version_and_env(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.txt"
    script = tmp_path / "hook.py"
    script.write_text(
        "import os, pathlib, sys\n"
        "phase = sys.argv[1]\n"
        "version = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "pathlib.Path(sys.argv[3]).open('a').write(\n"
        "    f'{phase}:{version}:{os.environ.get(\"SHARDFERRY_VERSION_ID\", \"\")}\\n'\n"
        ")\n",
        encoding="utf-8",
    )
    hook = CommandPauseHook(
        [sys.executable, str(script), "pause", "{version_id}", str(events)],
        [sys.executable, str(script), "resume", "", str(events)],
    )

    hook.pause("step-200")
    hook.resume()

    assert events.read_text(encoding="utf-8").splitlines() == [
        "pause:step-200:step-200",
        "resume::",
    ]


def test_command_pause_hook_fails_closed(tmp_path: Path) -> None:
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.exit(7)\n", encoding="utf-8")
    hook = CommandPauseHook(
        [sys.executable, str(script)],
        [sys.executable, str(script)],
    )
    with pytest.raises(CutoverError, match="exited 7"):
        hook.pause("step-200")


class RecordingHook:
    def __init__(self, *, fail_ready: bool = False) -> None:
        self.events: list[tuple[str, str | None]] = []
        self.fail_ready = fail_ready

    def pause(self, version_id: str) -> None:
        self.events.append(("pause", version_id))

    def resume(self) -> None:
        self.events.append(("resume", None))

    def ready(self, version_id: str) -> None:
        del version_id
        if self.fail_ready:
            raise RuntimeError("target not ready")


def test_handoff_service_pauses_source_before_promote(tmp_path: Path) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1")
    hook = RecordingHook()
    service = CutoverService(
        source,
        tmp_path / "target",
        pause_hook=hook,
    )
    service.follow_once()

    result = service.cutover(None)

    assert result["state"] == "promoted"
    assert hook.events == [("pause", "step-100")]


def test_handoff_promotes_checkpoint_written_during_pause(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1")

    class FinalCheckpointHook:
        def pause(self, version_id: str) -> None:
            del version_id
            make_version(source, "step-200", b"v2")

        def resume(self) -> None:
            return

    service = CutoverService(
        source,
        tmp_path / "target",
        pause_hook=FinalCheckpointHook(),
    )
    service.follow_once()

    result = service.cutover(None)

    assert result["state"] == "promoted"
    assert result["version_id"] == "step-200"
    assert service.status()["current"]["version_id"] == "step-200"


def test_handoff_explicit_version_rejects_new_pause_version(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1")

    class FinalCheckpointHook:
        def pause(self, version_id: str) -> None:
            del version_id
            make_version(source, "step-200", b"v2")

        def resume(self) -> None:
            return

    service = CutoverService(
        source,
        tmp_path / "target",
        pause_hook=FinalCheckpointHook(),
    )
    service.follow_once()

    with pytest.raises(CutoverError, match="newer checkpoint appeared"):
        service.cutover("step-100")


def test_handoff_service_resumes_source_when_target_fails(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    make_version(source, "step-100", b"v1")
    hook = RecordingHook(fail_ready=True)
    service = CutoverService(
        source,
        tmp_path / "target",
        pause_hook=hook,
        target_ready=hook.ready,
    )
    service.follow_once()

    with pytest.raises(Exception, match="target not ready"):
        service.cutover("step-100")

    assert ("pause", "step-100") in hook.events
    assert ("resume", None) in hook.events
