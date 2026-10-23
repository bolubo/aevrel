from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any


def source_id(
    tensor: str,
    rank: int,
    offset: list[int] | tuple[int, ...],
    size: list[int] | tuple[int, ...],
    checksum: str,
) -> str:
    payload = json.dumps(
        {
            "tensor": tensor,
            "rank": int(rank),
            "offset": [int(value) for value in offset],
            "size": [int(value) for value in size],
            "checksum": checksum,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_id_for_shard(shard: dict[str, Any]) -> str:
    return shard.get("id") or source_id(
        shard["tensor"],
        shard["rank"],
        shard["offset"],
        shard["size"],
        shard["checksum"],
    )


def source_maps(
    shards: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, int], list[dict[str, Any]]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_rank: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for shard in shards:
        identifier = source_id_for_shard(shard)
        by_id[identifier] = shard
        by_rank[(shard["tensor"], int(shard["rank"]))].append(shard)
    return by_id, by_rank


def resolve_source(
    by_id: dict[str, dict[str, Any]],
    by_rank: dict[tuple[str, int], list[dict[str, Any]]],
    tensor: str,
    source_input: dict[str, Any],
) -> dict[str, Any]:
    identifier = source_input.get("source_id")
    if identifier:
        try:
            return by_id[identifier]
        except KeyError as exc:
            raise KeyError(f"source id not found: {identifier}") from exc
    candidates = by_rank.get((tensor, int(source_input["source_rank"])), [])
    if len(candidates) != 1:
        raise KeyError(
            f"source input for {tensor}/rank{source_input['source_rank']} "
            f"requires source_id"
        )
    return candidates[0]
