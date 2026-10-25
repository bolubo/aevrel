"""Versioned manifest for rank-local resume state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "1"


def _canonical(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def canonical_payload(payload: Any) -> bytes:
    """Canonical JSON encoding shared by every manifest digest.

    Public so sibling contracts (e.g. the checkpoint-level resume-state
    manifest) reuse one encoding instead of forking a second one.
    """
    return _canonical(payload)


def payload_digest(payload: Any) -> str:
    """Digest of a payload under the shared canonical encoding."""
    return _digest(canonical_payload(payload))


def blob_digest(payload: bytes) -> str:
    """Digest of raw blob bytes; bundle storage and manifests agree here."""
    return _digest(bytes(payload))


@dataclass(frozen=True)
class StateEntry:
    rank: int
    name: str
    format: str
    digest: str
    size: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rank < 0:
            raise ValueError("state rank must be non-negative")
        if not self.name:
            raise ValueError("state name is required")
        if not self.format:
            raise ValueError("state format is required")
        if not self.digest.startswith("sha256:"):
            raise ValueError("state digest must be sha256")
        if self.size < 0:
            raise ValueError("state size must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "name": self.name,
            "format": self.format,
            "digest": self.digest,
            "size": self.size,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StateEntry:
        return cls(
            rank=int(payload["rank"]),
            name=str(payload["name"]),
            format=str(payload["format"]),
            digest=str(payload["digest"]),
            size=int(payload["size"]),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class StateManifest:
    checkpoint_id: str
    version: str
    source_world_size: int
    target_world_size: int
    rank_map: dict[int, int]
    entries: tuple[StateEntry, ...]
    schema_version: str = SCHEMA_VERSION
    complete: bool = False
    root: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported state manifest schema {self.schema_version}"
            )
        if not self.checkpoint_id:
            raise ValueError("checkpoint_id is required")
        if not self.version:
            raise ValueError("version is required")
        if self.source_world_size < 1 or self.target_world_size < 1:
            raise ValueError("world sizes must be positive")
        if any(rank < 0 or rank >= self.source_world_size for rank in self.rank_map):
            raise ValueError("rank_map contains an invalid source rank")
        if any(
            target < 0 or target >= self.target_world_size
            for target in self.rank_map.values()
        ):
            raise ValueError("rank_map contains an invalid target rank")
        if len(set(self.rank_map.values())) != len(self.rank_map):
            raise ValueError("rank_map must be one-to-one")
        keys = [(entry.rank, entry.name) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("state entries must be unique by rank and name")

    def to_dict(self, *, include_root: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "checkpointId": self.checkpoint_id,
            "version": self.version,
            "sourceWorldSize": self.source_world_size,
            "targetWorldSize": self.target_world_size,
            "rankMap": {str(key): value for key, value in sorted(self.rank_map.items())},
            "entries": [
                entry.to_dict()
                for entry in sorted(
                    self.entries,
                    key=lambda item: (item.rank, item.name),
                )
            ],
            "complete": self.complete,
            "createdAt": self.created_at,
        }
        if include_root:
            payload["root"] = self.root
        return payload

    def compute_root(self) -> str:
        return _digest(_canonical(self.to_dict(include_root=False)))

    @classmethod
    def create(
        cls,
        *,
        checkpoint_id: str,
        version: str,
        source_world_size: int,
        target_world_size: int | None = None,
        rank_map: dict[int, int] | None = None,
        entries: tuple[StateEntry, ...] | list[StateEntry],
        created_at: str | None = None,
    ) -> StateManifest:
        target = target_world_size or source_world_size
        mapping = (
            dict(rank_map)
            if rank_map is not None
            else {rank: rank for rank in range(source_world_size)}
        )
        manifest = cls(
            checkpoint_id=checkpoint_id,
            version=version,
            source_world_size=source_world_size,
            target_world_size=target,
            rank_map=mapping,
            entries=tuple(entries),
            created_at=(
                created_at
                or datetime.now(timezone.utc).isoformat()
            ),
        )
        complete = _is_complete(manifest)
        manifest = cls(
            **{
                **manifest.__dict__,
                "complete": complete,
                "root": "",
            }
        )
        root = manifest.compute_root()
        return cls(**{**manifest.__dict__, "root": root})

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StateManifest:
        return cls(
            schema_version=str(payload["schemaVersion"]),
            checkpoint_id=str(payload["checkpointId"]),
            version=str(payload["version"]),
            source_world_size=int(payload["sourceWorldSize"]),
            target_world_size=int(payload["targetWorldSize"]),
            rank_map={
                int(key): int(value)
                for key, value in payload.get("rankMap", {}).items()
            },
            entries=tuple(
                StateEntry.from_dict(item)
                for item in payload.get("entries", [])
            ),
            complete=bool(payload.get("complete", False)),
            root=str(payload.get("root", "")),
            created_at=str(payload.get("createdAt", "")),
        )


def _is_complete(manifest: StateManifest) -> bool:
    if set(manifest.rank_map) != set(range(manifest.source_world_size)):
        return False
    if len(set(manifest.rank_map.values())) != manifest.target_world_size:
        return False
    source_ranks = {entry.rank for entry in manifest.entries}
    if source_ranks != set(range(manifest.source_world_size)):
        return False
    names_by_rank = {
        rank: {
            entry.name
            for entry in manifest.entries
            if entry.rank == rank
        }
        for rank in range(manifest.source_world_size)
    }
    first = names_by_rank.get(0)
    return bool(first) and all(names == first for names in names_by_rank.values())
