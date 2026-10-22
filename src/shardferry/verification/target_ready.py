from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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


class HttpTargetReady:
    """Poll a target readiness endpoint until it returns a 2xx response."""

    _RETRYABLE = {404, 409, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 300.0,
        interval: float = 0.5,
        request_timeout: float = 10.0,
        token: str | None = None,
    ) -> None:
        if not url:
            raise ValueError("target-ready URL must not be empty")
        if timeout <= 0:
            raise ValueError("target-ready timeout must be positive")
        if interval < 0:
            raise ValueError("target-ready interval must be non-negative")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.url = url
        self.timeout = timeout
        self.interval = interval
        self.request_timeout = request_timeout
        self.token = token

    def __call__(self, version_id: str) -> None:
        url = self.url.replace("{version_id}", version_id)
        deadline = time.monotonic() + self.timeout
        last_error: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TargetReadyError(
                    f"target-ready URL timed out after {self.timeout:.3f}s: "
                    f"{last_error or 'no response'}"
                )
            request = Request(url)
            if self.token:
                request.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urlopen(
                    request,
                    timeout=min(self.request_timeout, remaining),
                ) as response:
                    if 200 <= response.status < 300:
                        return
                    last_error = f"HTTP {response.status}"
            except HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code not in self._RETRYABLE:
                    raise TargetReadyError(
                        f"target-ready URL returned {last_error}: {url}"
                    ) from exc
            except (URLError, OSError) as exc:
                last_error = str(exc)
            wait = min(self.interval, max(0.0, deadline - time.monotonic()))
            if wait:
                time.sleep(wait)


class TargetReadyChain:
    """Run multiple readiness checks in order for one version."""

    def __init__(self, *checks) -> None:
        if not checks:
            raise ValueError("target-ready chain must not be empty")
        self.checks = tuple(checks)

    def __call__(self, version_id: str) -> None:
        for check in self.checks:
            check(version_id)


class DcpTargetReady:
    """Load a complete target DCP checkpoint as a readiness proof.

    This intentionally performs a real DCP load, not just a metadata check.
    It allocates the target state dict in CPU memory, so callers running a
    large model should use it as a target-job hook only when that budget is
    acceptable.
    """

    def __init__(
        self,
        checkpoint: str | Path,
        *,
        metadata_dir: str | Path | None = None,
        rank: int | None = None,
    ) -> None:
        if rank is not None and rank < 0:
            raise ValueError("DCP rank must be non-negative")
        self.checkpoint = Path(checkpoint).resolve()
        self.metadata_dir = (
            Path(metadata_dir).resolve()
            if metadata_dir is not None
            else self.checkpoint
        )
        self.rank = rank

    def __call__(self, version_id: str) -> None:
        if self.rank is not None:
            self._load_rank(version_id)
            return
        try:
            from torch.distributed.checkpoint import load as dcp_load
            import torch

            from ..dcp import read_dcp_metadata

            metadata = read_dcp_metadata(self.metadata_dir)
            state_dict = {}
            for name, tensor_metadata in metadata.state_dict_metadata.items():
                if not hasattr(tensor_metadata, "size"):
                    continue
                state_dict[name] = torch.empty(
                    tuple(tensor_metadata.size),
                    dtype=tensor_metadata.properties.dtype,
                )
            if not state_dict:
                raise ValueError("target DCP contains no tensors")
            dcp_load(state_dict, checkpoint_id=str(self.checkpoint))
        except Exception as exc:
            raise TargetReadyError(
                f"target DCP load failed for {version_id}: {exc}"
            ) from exc

    def _load_rank(self, version_id: str) -> None:
        try:
            from ..dcp import (
                inventory_dcp_descriptors,
                read_dcp_tensor_chunk,
            )

            inventory, refs = inventory_dcp_descriptors(
                self.checkpoint,
                self.rank,
                metadata_dir=self.metadata_dir,
            )
            chunk_meta = {
                chunk["checksum"]: chunk
                for chunk in inventory["chunks"]
            }
            for digest, ref in refs.items():
                tensor = read_dcp_tensor_chunk(self.checkpoint, ref)
                expected = tuple(chunk_meta[digest]["tensor_size"])
                if tuple(tensor.shape) != expected:
                    raise ValueError(
                        f"chunk shape mismatch for rank {self.rank}: "
                        f"{tuple(tensor.shape)} != {expected}"
                    )
                del tensor
            if not refs:
                raise ValueError(
                    f"target rank {self.rank} has no local DCP chunks"
                )
        except Exception as exc:
            raise TargetReadyError(
                f"target rank {self.rank} DCP load failed for "
                f"{version_id}: {exc}"
            ) from exc
