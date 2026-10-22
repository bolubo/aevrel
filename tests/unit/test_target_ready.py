from __future__ import annotations

import sys

import pytest

from shardferry.target_ready import CommandTargetReady, TargetReadyError


def test_command_target_ready_substitutes_version() -> None:
    command = CommandTargetReady(
        [
            sys.executable,
            "-c",
            "import sys; raise SystemExit(0 if sys.argv[1] == 'step-100' else 1)",
            "{version_id}",
        ]
    )
    command("step-100")


def test_command_target_ready_fails_closed() -> None:
    command = CommandTargetReady(
        [
            sys.executable,
            "-c",
            "import sys; print('not ready', file=sys.stderr); raise SystemExit(7)",
        ]
    )
    with pytest.raises(TargetReadyError, match="exited 7"):
        command("step-100")
