from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


class TargetReadyError(RuntimeError):
    """Raised when the target workload does not become ready."""


class CommandTargetReady:
    """Run a command after promote and fail closed on non-zero/timeout."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout: float = 300.0,
        cwd: str | Path | None = None,
    ) -> None:
        if not command:
            raise ValueError("target-ready command must not be empty")
        if timeout <= 0:
            raise ValueError("target-ready timeout must be positive")
        self.command = tuple(command)
        self.timeout = timeout
        self.cwd = Path(cwd) if cwd is not None else None

    def __call__(self, version_id: str) -> None:
        command = [
            arg.replace("{version_id}", version_id)
            for arg in self.command
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                timeout=self.timeout,
                check=False,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise TargetReadyError(
                f"target-ready timeout after {self.timeout:.3f}s"
            ) from exc
        except OSError as exc:
            raise TargetReadyError(f"target-ready command failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise TargetReadyError(
                f"target-ready command exited {completed.returncode}: {detail}"
            )
