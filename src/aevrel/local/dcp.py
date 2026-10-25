"""Read-only DCP inventory provider for the local Python adapter."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LocalError
from .models import (
    CheckpointRecord,
    RankInventory,
    Reason,
    ReasonCode,
    ShardRef,
    iso_utc,
)
from ..dcp import read_dcp_metadata
from .provider import ProviderCapabilities


class DCPStateProvider:
    """Inventory DCP metadata and local rank files without reading tensors."""

    name = "dcp"

    def supports(self, checkpoint: str | Path) -> bool:
        path = Path(checkpoint).expanduser()
        return path.is_dir() and (path / ".metadata").is_file()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            version="1",
            discover=True,
            inventory=True,
            read=True,
            reshard=True,
        )

    def inventory(
        self,
        checkpoint: str | Path,
        *,
        namespace: str = "",
        job_uid: str = "",
        version: str = "",
        step: int | None = None,
    ) -> CheckpointRecord:
        path = Path(checkpoint).expanduser().resolve()
        metadata_file = path / ".metadata"
        if not path.is_dir():
            raise LocalError(
                "SOURCE_NOT_FOUND",
                f"checkpoint directory not found: {path}",
            )
        if not metadata_file.is_file():
            raise LocalError(
                "SOURCE_NOT_FOUND",
                f"DCP metadata not found: {metadata_file}",
            )
        try:
            metadata = read_dcp_metadata(path)
            metadata_bytes = metadata_file.read_bytes()
        except Exception as exc:
            raise LocalError(
                "SOURCE_UNREADABLE",
                f"could not read DCP metadata: {exc}",
            ) from exc

        storage_data = metadata.storage_data or {}
        by_rank: dict[int, list[ShardRef]] = {}
        reasons: list[Reason] = []
        mtimes = [metadata_file.stat().st_mtime]
        for metadata_index, info in storage_data.items():
            relative = str(getattr(info, "relative_path", "") or "")
            if not relative:
                continue
            match = re.match(r"__(\d+)_", Path(relative).name)
            if not match:
                reasons.append(
                    Reason(
                        ReasonCode.SOURCE_UNREADABLE.value,
                        f"cannot infer rank from {relative}",
                    )
                )
                continue
            rank = int(match.group(1))
            offset = int(getattr(info, "offset", 0))
            length = int(getattr(info, "length", 0))
            shard_path = path / relative
            required = offset + length
            if not shard_path.is_file():
                reasons.append(
                    Reason(
                        ReasonCode.SHARD_MISSING.value,
                        f"{relative} (rank {rank})",
                    )
                )
                continue
            actual = shard_path.stat().st_size
            mtimes.append(shard_path.stat().st_mtime)
            if actual < required:
                reasons.append(
                    Reason(
                        ReasonCode.SHARD_TRUNCATED.value,
                        f"{relative} has {actual} bytes, needs {required}",
                    )
                )
            by_rank.setdefault(rank, []).append(
                ShardRef(
                    name=str(getattr(metadata_index, "fqn", "")),
                    relative_path=relative,
                    rank=rank,
                    offset=offset,
                    length=length,
                    size_bytes=length,
                )
            )

        ranks: list[RankInventory] = []
        for rank in sorted(by_rank):
            shards = tuple(sorted(by_rank[rank], key=lambda item: item.name))
            ranks.append(
                RankInventory(
                    rank=rank,
                    shards=shards,
                    bytes=sum(shard.size_bytes for shard in shards),
                )
            )
        if not ranks:
            reasons.append(
                Reason(
                    ReasonCode.SOURCE_UNREADABLE.value,
                    "metadata has no local rank shards",
                )
            )
        else:
            expected_ranks = set(range(max(by_rank) + 1))
            missing_ranks = sorted(expected_ranks - set(by_rank))
            for rank in missing_ranks:
                reasons.append(
                    Reason(
                        ReasonCode.RANK_MISSING.value,
                        f"rank {rank} has no local shard files",
                    )
                )

        latest = max(mtimes) if mtimes else None
        return CheckpointRecord(
            checkpoint=str(path),
            namespace=namespace,
            job_uid=job_uid or path.name,
            version=version or path.name,
            step=step,
            provider=self.name,
            format_version=str(getattr(metadata, "version", "1.0.0")),
            metadata_digest=(
                "sha256:" + hashlib.sha256(metadata_bytes).hexdigest()
            ),
            ranks=tuple(ranks),
            observed_at=iso_utc(datetime.now(timezone.utc)),
            latest_mtime=(
                iso_utc(datetime.fromtimestamp(latest, timezone.utc))
                if latest is not None
                else None
            ),
            complete=not reasons,
            reasons=tuple(reasons),
        )
