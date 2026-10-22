from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path

import shardferry.cutover as cutover_module
from shardferry.cutover import CutoverPolicy, CutoverSession


class RecordingHook:
    def __init__(self, path: Path, kill_at_pause: bool) -> None:
        self.path = path
        self.kill_at_pause = kill_at_pause

    def _record(self, state: str, version_id: str | None) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"state": state, "version": version_id}) + "\n")

    def pause(self, version_id: str) -> None:
        self._record("pause", version_id)
        if self.kill_at_pause:
            os.kill(os.getpid(), signal.SIGKILL)

    def resume(self) -> None:
        self._record("resume", None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--mode", choices=("pause", "delta", "promote", "rollback"))
    parser.add_argument("--events", required=True)
    args = parser.parse_args()

    hook = RecordingHook(Path(args.events), args.mode == "pause")
    session = CutoverSession(
        args.source,
        _target(args.target),
        pause_hook=hook,
        policy=CutoverPolicy(max_pause_seconds=5),
        chunk_bytes=4,
    )
    target = session.target
    if args.mode == "delta":
        def kill_delta():
            os.kill(os.getpid(), signal.SIGKILL)

        session.follower.sync_once = kill_delta
    elif args.mode == "promote":
        original_promote = target.promote

        def promote_then_kill(manifest, *, fence=None):
            result = original_promote(manifest, fence=fence)
            os.kill(os.getpid(), signal.SIGKILL)
            return result

        target.promote = promote_then_kill
    elif args.mode == "rollback":
        def rollback_then_kill(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGKILL)

        target.rollback = rollback_then_kill
        times = iter([0.0, 0.0, 10.0])
        cutover_module.monotonic = lambda: next(times)

    session.cutover(
        args.version,
        owner="controller-a",
        epoch=1,
    )


def _target(path: str):
    from shardferry.cdp import VersionTarget

    return VersionTarget(path)


if __name__ == "__main__":
    main()
