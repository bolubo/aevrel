from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Callable, Mapping, Protocol

from .cdp import (
    CDPError,
    CheckpointFollower,
    ChunkStore,
    ChunkingPolicy,
    FenceStore,
    FenceToken,
    VersionDescriptor,
    VersionManifest,
    VersionTarget,
    scan_versions,
)


class CutoverError(RuntimeError):
    """Raised when a cutoff cannot be completed safely."""


class CutoverRecoveryError(CutoverError):
    """Raised when the original failure and its cleanup both need attention."""

    def __init__(
        self,
        original: BaseException,
        recovery_errors: list[str],
    ) -> None:
        self.original = original
        self.recovery_errors = tuple(recovery_errors)
        details = "; ".join(recovery_errors)
        super().__init__(f"cutover failed: {original}; recovery errors: {details}")


class PauseHook(Protocol):
    def pause(self, version_id: str) -> None:
        ...

    def resume(self) -> None:
        ...


class NoopPauseHook:
    def pause(self, version_id: str) -> None:
        del version_id

    def resume(self) -> None:
        return


@dataclass(frozen=True)
class CutoverPolicy:
    max_pause_seconds: float = 60.0
    max_lag_versions: int = 1
    require_latest: bool = True

    def __post_init__(self) -> None:
        if self.max_pause_seconds <= 0:
            raise ValueError("max_pause_seconds must be positive")
        if self.max_lag_versions < 0:
            raise ValueError("max_lag_versions must be non-negative")


@dataclass(frozen=True)
class CutoverResult:
    state: str
    version_id: str
    previous_version: str | None
    manifest_root: str
    paused_seconds: float
    fence: FenceToken
    target_ready_seconds: float = 0.0


@dataclass(frozen=True)
class CutoverRecoveryResult:
    state: str
    version_id: str | None
    previous_version: str | None
    fence: FenceToken | None
    resumed: bool


class CutoverSession:
    """Pause source, catch up final deltas, verify, and promote one version."""

    def __init__(
        self,
        source_root,
        target: VersionTarget,
        *,
        pause_hook: PauseHook | None = None,
        policy: CutoverPolicy | None = None,
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
        target_ready: Callable[[str], None] | None = None,
    ) -> None:
        self.source_root = source_root
        self.target = target
        self.pause_hook = pause_hook or NoopPauseHook()
        self.target_ready = target_ready
        self.policy = policy or CutoverPolicy()
        self.follower = follower or CheckpointFollower(
            source_root,
            target,
            chunk_bytes=chunk_bytes,
            chunking=chunking,
            file_provider=file_provider,
            manifest_provider=manifest_provider,
        )
        self.fences = FenceStore(target.root)

    def sync_once(self) -> list:
        return self.follower.sync_once()

    def _descriptors(self):
        return scan_versions(self.source_root)

    def _requested(self, version_id: str):
        descriptors = self._descriptors()
        for descriptor in descriptors:
            if descriptor.version_id == version_id:
                return descriptor, descriptors
        raise CutoverError(f"source version is not complete: {version_id}")

    def current_lag(self) -> int:
        descriptors = self._descriptors()
        manifests = self.target.manifests.list()
        if not manifests:
            return len(descriptors)
        positions = {
            descriptor.version_id: index
            for index, descriptor in enumerate(descriptors)
        }
        index = max(
            (positions.get(manifest.version_id, -1) for manifest in manifests),
            default=-1,
        )
        if index < 0:
            return len(descriptors)
        return len(descriptors) - 1 - index

    def cutover(
        self,
        version_id: str,
        *,
        owner: str = "shardferry-controller",
        epoch: int = 1,
        accept_new_version: bool = False,
    ) -> CutoverResult:
        requested, descriptors = self._requested(version_id)
        requested_index = descriptors.index(requested)
        if self.policy.require_latest and requested_index != len(descriptors) - 1:
            raise CutoverError(
                f"version {version_id} is not the latest complete version"
            )

        # Stage before pausing. If the requested manifest is absent, catch up first.
        if not self.target.manifests.path(version_id).is_file():
            self.follower.sync_once()
        manifest = self.target.manifests.read(version_id)
        if manifest.metadata_hash != requested.marker.metadata_hash:
            raise CutoverError(
                f"target manifest metadata hash mismatch for {version_id}"
            )
        if not self.target._shadow_path(version_id).is_dir():
            self.target.stage(manifest)
        if self.current_lag() > self.policy.max_lag_versions:
            raise CutoverError(
                f"target lag exceeds policy: "
                f"{self.current_lag()} > {self.policy.max_lag_versions}"
            )

        fence = self.fences.acquire(owner, epoch, version_id)
        previous = self.target.current()
        pause_attempted = False
        promoted = False
        paused_seconds = 0.0
        try:
            pause_attempted = True
            self.pause_hook.pause(version_id)
            started = monotonic()

            # Training is paused at a checkpoint boundary; the final catch-up
            # is now cheap and still uses the normal delta path.
            self.follower.sync_once()
            after = self._descriptors()
            if accept_new_version and after:
                latest = after[-1]
                if latest.version_id != version_id:
                    requested = latest
                    version_id = latest.version_id
                    fence = self.fences.acquire(
                        owner,
                        epoch,
                        version_id,
                    )
                    manifest = self.target.manifests.read(version_id)
                    if manifest.metadata_hash != latest.marker.metadata_hash:
                        raise CutoverError(
                            f"final target manifest metadata hash mismatch "
                            f"for {version_id}"
                        )
            after_index = next(
                (
                    index
                    for index, item in enumerate(after)
                    if item.version_id == version_id
                ),
                -1,
            )
            if (
                self.policy.require_latest
                and not accept_new_version
                and after_index != len(after) - 1
            ):
                raise CutoverError(
                    f"a newer checkpoint appeared during cutover: {version_id}"
                )
            manifest = self.target.manifests.read(version_id)
            if manifest.metadata_hash != requested.marker.metadata_hash:
                raise CutoverError(
                    f"target manifest changed during cutover: {version_id}"
                )
            pause_before_promote = monotonic() - started
            if pause_before_promote > self.policy.max_pause_seconds:
                raise CutoverError(
                    f"cutover exceeded max pause before promote "
                    f"({pause_before_promote:.3f}s > "
                    f"{self.policy.max_pause_seconds:.3f}s)"
                )
            self.target.promote(manifest, fence=fence)
            promoted = True
            target_ready_seconds = 0.0
            if self.target_ready is not None:
                ready_started = monotonic()
                self.target_ready(version_id)
                target_ready_seconds = monotonic() - ready_started
            paused_seconds = monotonic() - started
            if paused_seconds > self.policy.max_pause_seconds:
                raise CutoverError(
                    f"cutover exceeded max pause "
                    f"({paused_seconds:.3f}s > {self.policy.max_pause_seconds:.3f}s)"
                )
            return CutoverResult(
                state="promoted",
                version_id=version_id,
                previous_version=(
                    previous.version_id if previous is not None else None
                ),
                manifest_root=manifest.root,
                paused_seconds=paused_seconds,
                fence=fence,
                target_ready_seconds=target_ready_seconds,
            )
        except Exception as original_error:
            recovery_errors: list[str] = []
            if pause_attempted:
                try:
                    self.pause_hook.resume()
                except Exception as exc:
                    recovery_errors.append(f"resume source: {exc}")
            if promoted:
                try:
                    if previous is not None:
                        self.target.rollback(
                            previous.version_id,
                            fence=fence,
                        )
                    else:
                        self.target.clear_current(fence=fence)
                except Exception as exc:
                    recovery_errors.append(f"restore target: {exc}")
            if recovery_errors:
                raise CutoverRecoveryError(
                    original=original_error,
                    recovery_errors=recovery_errors,
                ) from original_error
            raise

    def recover(
        self,
        *,
        owner: str | None = None,
    ) -> CutoverRecoveryResult:
        """Recover a cutover interrupted by process death.

        If the target already points at the fenced version, the promotion is
        considered complete. Otherwise the only safe action is to resume the
        paused source; the target current pointer is never guessed or rewritten.
        """
        fence = self.fences.current()
        current = self.target.current()
        if fence is None:
            return CutoverRecoveryResult(
                state="idle",
                version_id=current.version_id if current else None,
                previous_version=None,
                fence=None,
                resumed=False,
            )
        if owner is not None and fence.owner != owner:
            raise CutoverError(
                f"fence owner mismatch: {fence.owner} != {owner}"
            )
        if (
            current is not None
            and current.version_id == fence.version_id
        ):
            return CutoverRecoveryResult(
                state="promoted",
                version_id=current.version_id,
                previous_version=None,
                fence=fence,
                resumed=False,
            )
        self.pause_hook.resume()
        return CutoverRecoveryResult(
            state="resumed",
            version_id=fence.version_id,
            previous_version=current.version_id if current else None,
            fence=fence,
            resumed=True,
        )
