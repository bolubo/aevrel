"""Handoff hooks for the versioned checkpoint cutover path."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence

from .cutover import CutoverError, PauseHook


class CommandPauseHook(PauseHook):
    """Pause/resume a training source using explicit commands.

    The commands are argv templates, not shell strings. ``{version_id}`` is
    substituted into each argument. This keeps the hook portable and avoids
    silently enabling shell interpolation.
    """

    def __init__(
        self,
        pause_command: Sequence[str] | str,
        resume_command: Sequence[str] | str,
        *,
        timeout: float = 60.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("pause timeout must be positive")
        self.pause_argv = _argv(pause_command)
        self.resume_argv = _argv(resume_command)
        if not self.pause_argv:
            raise ValueError("pause command must not be empty")
        if not self.resume_argv:
            raise ValueError("resume command must not be empty")
        self.timeout = timeout
        self.env = dict(env or {})

    def pause(self, version_id: str) -> None:
        self._run(self.pause_argv, version_id=version_id, phase="pause")

    def resume(self) -> None:
        self._run(self.resume_argv, phase="resume")

    def _run(
        self,
        argv: Sequence[str],
        *,
        version_id: str | None = None,
        phase: str,
    ) -> None:
        command = [
            arg.replace("{version_id}", version_id or "")
            for arg in argv
        ]
        environment = os.environ.copy()
        environment.update(self.env)
        if version_id is not None:
            environment["SHARDFERRY_VERSION_ID"] = version_id
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise CutoverError(
                f"{phase} command not found: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CutoverError(
                f"{phase} command timed out after {self.timeout}s"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if len(detail) > 500:
                detail = detail[:500] + "..."
            suffix = f": {detail}" if detail else ""
            raise CutoverError(
                f"{phase} command exited {result.returncode}{suffix}"
            )


def _argv(command: Sequence[str] | str) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    return [str(item) for item in command]
