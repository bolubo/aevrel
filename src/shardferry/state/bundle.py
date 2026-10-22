"""Content-addressed bundles for rank-local resume state."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .manifest import StateEntry, StateManifest
from .providers import StateProvider


class StateBundleError(RuntimeError):
    """Raised when a state bundle is incomplete, corrupt, or unmappable."""


@dataclass(frozen=True)
class StateBundle:
    root: Path
    manifest: StateManifest
    blobs: dict[str, bytes]

    @staticmethod
    def manifest_path(root: str | Path) -> Path:
        return Path(root) / "state" / "manifest.json"

    @staticmethod
    def blob_path(root: str | Path, digest: str) -> Path:
        name = digest.split(":", 1)[-1]
        return Path(root) / "state" / "blobs" / name

    def write(self) -> None:
        target = self.root / "state"
        target.mkdir(parents=True, exist_ok=True)
        for digest, payload in self.blobs.items():
            path = self.blob_path(self.root, digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, payload)
        manifest_bytes = json.dumps(
            self.manifest.to_dict(),
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _atomic_write(self.manifest_path(self.root), manifest_bytes)

    @classmethod
    def read(cls, root: str | Path) -> StateBundle:
        base = Path(root)
        manifest_path = cls.manifest_path(base)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = StateManifest.from_dict(payload)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateBundleError(f"could not read state manifest: {exc}") from exc
        if manifest.root != manifest.compute_root():
            raise StateBundleError("state manifest root mismatch")
        blobs: dict[str, bytes] = {}
        for entry in manifest.entries:
            path = cls.blob_path(base, entry.digest)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise StateBundleError(
                    f"missing state blob for rank {entry.rank}/{entry.name}"
                ) from exc
            if len(data) != entry.size or _digest(data) != entry.digest:
                raise StateBundleError(
                    f"state blob mismatch for rank {entry.rank}/{entry.name}"
                )
            blobs[entry.digest] = data
        return cls(root=base, manifest=manifest, blobs=blobs)


def capture_state(
    *,
    root: str | Path,
    checkpoint_id: str,
    version: str,
    rank: int,
    source_world_size: int,
    providers: Iterable[StateProvider],
    target_world_size: int | None = None,
    rank_map: dict[int, int] | None = None,
) -> StateBundle:
    existing_entries: list[StateEntry] = []
    blobs: dict[str, bytes] = {}
    manifest_path = StateBundle.manifest_path(root)
    if manifest_path.is_file():
        existing = StateBundle.read(root)
        if (
            existing.manifest.checkpoint_id != checkpoint_id
            or existing.manifest.version != version
            or existing.manifest.source_world_size != source_world_size
            or (
                target_world_size is not None
                and existing.manifest.target_world_size != target_world_size
            )
            or (
                rank_map is not None
                and existing.manifest.rank_map != rank_map
            )
        ):
            raise StateBundleError(
                "existing state bundle belongs to a different checkpoint/version/topology"
            )
        existing_entries = [
            entry
            for entry in existing.manifest.entries
            if entry.rank != rank
        ]
        blobs.update(existing.blobs)
    entries: list[StateEntry] = existing_entries
    for provider in providers:
        payload = bytes(provider.capture())
        digest = _digest(payload)
        entries.append(
            StateEntry(
                rank=rank,
                name=provider.name,
                format=provider.format,
                digest=digest,
                size=len(payload),
            )
        )
        blobs[digest] = payload
    manifest = StateManifest.create(
        checkpoint_id=checkpoint_id,
        version=version,
        source_world_size=source_world_size,
        target_world_size=target_world_size,
        rank_map=rank_map,
        entries=entries,
    )
    bundle = StateBundle(root=Path(root), manifest=manifest, blobs=blobs)
    bundle.write()
    return bundle


def restore_state(
    bundle: StateBundle,
    *,
    target_rank: int,
    providers: Iterable[StateProvider],
    strict: bool = True,
) -> None:
    providers = list(providers)
    source_rank = _source_rank_for_target(bundle.manifest.rank_map, target_rank)
    by_name = {
        entry.name: entry
        for entry in bundle.manifest.entries
        if entry.rank == source_rank
    }
    provided = {provider.name for provider in providers}
    missing = sorted(set(by_name) - provided)
    if missing and strict:
        raise StateBundleError(
            f"target providers do not cover source rank {source_rank} "
            f"state: {', '.join(missing)}"
        )
    for provider in providers:
        entry = by_name.get(provider.name)
        if entry is None:
            if strict:
                raise StateBundleError(
                    f"missing state {provider.name} for source rank {source_rank}"
                )
            continue
        payload = bundle.blobs.get(entry.digest)
        if payload is None or _digest(payload) != entry.digest:
            raise StateBundleError(
                f"state {provider.name} for source rank {source_rank} is corrupt"
            )
        provider.restore(payload)


def verify_restored(
    bundle: StateBundle,
    *,
    target_rank: int,
    providers: Iterable[StateProvider],
) -> None:
    """Verify that every restored provider captures back to its entry."""
    providers = list(providers)
    source_rank = _source_rank_for_target(bundle.manifest.rank_map, target_rank)
    by_name = {
        entry.name: entry
        for entry in bundle.manifest.entries
        if entry.rank == source_rank
    }
    for provider in providers:
        entry = by_name.get(provider.name)
        if entry is None:
            raise StateBundleError(
                f"missing state {provider.name} for source rank {source_rank}"
            )
        entry_payload = bundle.blobs.get(entry.digest)
        if entry_payload is None:
            raise StateBundleError(
                f"missing state blob for source rank {source_rank} "
                f"{provider.name}"
            )
        verifier = getattr(provider, "verify", None)
        if callable(verifier):
            verifier(entry_payload)
        else:
            payload = bytes(provider.capture())
            if _digest(payload) != entry.digest:
                raise StateBundleError(
                    f"restored state {provider.name} for source rank "
                    f"{source_rank} does not match its entry"
                )


def verify_state(bundle: StateBundle, *, require_complete: bool = False) -> None:
    if bundle.manifest.root != bundle.manifest.compute_root():
        raise StateBundleError("state manifest root mismatch")
    for entry in bundle.manifest.entries:
        payload = bundle.blobs.get(entry.digest)
        if payload is None or len(payload) != entry.size or _digest(payload) != entry.digest:
            raise StateBundleError(
                f"state blob mismatch for rank {entry.rank}/{entry.name}"
            )
    if require_complete and not bundle.manifest.complete:
        raise StateBundleError(
            "state bundle is not closure-complete for its declared rank map"
        )


def _source_rank_for_target(rank_map: dict[int, int], target_rank: int) -> int:
    matches = [source for source, target in rank_map.items() if target == target_rank]
    if not matches:
        raise StateBundleError(
            f"target rank {target_rank} has no source state mapping"
        )
    if len(matches) > 1:
        raise StateBundleError(
            f"target rank {target_rank} has multiple source mappings"
        )
    return matches[0]


def _digest(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
