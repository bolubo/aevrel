from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Mapping

from .cdp import (
    CheckpointFollower,
    ChunkStore,
    ChunkingPolicy,
    SyncResult,
    VersionDescriptor,
    VersionManifest,
    VersionTarget,
    scan_versions,
)


@dataclass(frozen=True)
class FollowerStatus:
    state: str = "starting"
    source_latest: str | None = None
    target_latest: str | None = None
    lag_versions: int = 0
    synced_versions: int = 0
    last_missing_bytes: int = 0
    total_missing_bytes: int = 0
    last_error: str | None = None
    last_success_at: str | None = None


class FollowerLoop:
    """Polling follower with bounded catch-up and observable lag."""

    def __init__(
        self,
        source_root,
        target: VersionTarget,
        *,
        chunk_bytes: int = 4 * 1024 * 1024,
        chunking: ChunkingPolicy | None = None,
        file_provider: Callable[[VersionDescriptor], Mapping[str, bytes]] | None = None,
        manifest_provider: (
            Callable[
                [VersionDescriptor, ChunkStore, ChunkingPolicy],
                VersionManifest,
            ]
            | None
        ) = None,
        follower: CheckpointFollower | None = None,
        lock: threading.RLock | None = None,
        poll_interval: float = 5.0,
        max_versions_per_cycle: int = 1,
    ) -> None:
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        if max_versions_per_cycle < 1:
            raise ValueError("max_versions_per_cycle must be positive")
        self.follower = follower or CheckpointFollower(
            source_root,
            target,
            chunk_bytes=chunk_bytes,
            chunking=chunking,
            file_provider=file_provider,
            manifest_provider=manifest_provider,
        )
        self.poll_interval = poll_interval
        self.max_versions_per_cycle = max_versions_per_cycle
        self._lock = lock or threading.RLock()
        self._status_lock = threading.RLock()
        self._status = FollowerStatus()

    def status(self) -> FollowerStatus:
        with self._status_lock:
            return self._status

    def _update(self, **changes) -> None:
        with self._status_lock:
            self._status = replace(self._status, **changes)

    def _lag(self) -> tuple[int, str | None, str | None]:
        descriptors = scan_versions(self.follower.source_root)
        if not descriptors:
            return 0, None, None
        positions = {
            descriptor.version_id: index
            for index, descriptor in enumerate(descriptors)
        }
        manifests = self.follower.target.manifests.list()
        indices = [
            positions[manifest.version_id]
            for manifest in manifests
            if manifest.version_id in positions
        ]
        if not indices:
            return len(descriptors), descriptors[-1].version_id, None
        latest_index = max(indices)
        return (
            len(descriptors) - 1 - latest_index,
            descriptors[-1].version_id,
            descriptors[latest_index].version_id,
        )

    def sync_once(
        self,
        *,
        max_versions: int | None = None,
    ) -> list[SyncResult]:
        """Synchronize once under the loop's shared target lock."""
        with self._lock:
            results = self.follower.sync_once(max_versions=max_versions)
            self._record_success(results)
            return results

    def _record_success(self, results: list[SyncResult]) -> None:
        staged = [result for result in results if result.staged]
        lag, source_latest, target_latest = self._lag()
        now = datetime.now(timezone.utc).isoformat()
        self._update(
            state="catching-up" if lag > 0 else "following",
            source_latest=source_latest,
            target_latest=target_latest,
            lag_versions=lag,
            synced_versions=self._status.synced_versions + len(staged),
            last_missing_bytes=sum(
                result.missing_bytes for result in staged
            ),
            total_missing_bytes=(
                self._status.total_missing_bytes
                + sum(result.missing_bytes for result in staged)
            ),
            last_error=None,
            last_success_at=now if staged else self._status.last_success_at,
        )

    def step(self) -> int:
        """Catch up at most one batch. Errors are recorded and retried later."""
        try:
            results = self.sync_once(
                max_versions=self.max_versions_per_cycle
            )
            return len([result for result in results if result.staged])
        except Exception as exc:
            self._update(
                state="error",
                last_error=str(exc),
            )
            return 0

    def run(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        try:
            while not stop.is_set():
                self.step()
                if stop.wait(self.poll_interval):
                    break
        finally:
            self._update(state="stopped")
