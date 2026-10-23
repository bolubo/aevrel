from __future__ import annotations

from pathlib import Path
from typing import Any

from ..defaults import (
    DEFAULT_SOURCE_PLACEMENTS,
    DEFAULT_TARGET_PLACEMENTS,
)
from .checkpoint import (
    DcpError,
    inventory_dcp_descriptors,
    local_dcp_ranks,
)
from ..manifest import validate_spec
from ..storage.dcpzip import dcpzip_uri


def build_local_range_spec(
    checkpoint: str | Path,
    *,
    target_storage_uri: str,
    source_mesh_shape: tuple[int, ...] | None = None,
    source_placements: tuple[str, ...] = DEFAULT_SOURCE_PLACEMENTS,
    target_world_size: int | None = None,
    target_mesh_shape: tuple[int, ...] | None = None,
    target_placements: tuple[str, ...] = DEFAULT_TARGET_PLACEMENTS,
    metadata_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build a MigrationSpec with direct DCP ZIP range URIs.

    This avoids `inspect_dcp`'s raw-shard materialization. The executor and
    verifier read each source chunk directly from its `.distcp` ZIP.
    """
    checkpoint_path = Path(checkpoint).resolve()
    ranks = local_dcp_ranks(
        checkpoint_path,
        metadata_dir=metadata_dir,
    )
    source_world = max(ranks) + 1
    if ranks != list(range(source_world)):
        raise DcpError(
            f"source ranks are not contiguous: {ranks}"
        )
    if source_mesh_shape is None:
        source_mesh_shape = (source_world,)
    if len(source_mesh_shape) != len(source_placements):
        raise DcpError("source mesh shape and placements differ")
    if target_world_size is None:
        target_world_size = source_world
    if target_mesh_shape is None:
        target_mesh_shape = (target_world_size,)
    if len(target_mesh_shape) != len(target_placements):
        raise DcpError("target mesh shape and placements differ")

    tensors: dict[str, dict[str, Any]] = {}
    shards: list[dict[str, Any]] = []
    for rank in ranks:
        inventory, refs = inventory_dcp_descriptors(
            checkpoint_path,
            rank,
            metadata_dir=metadata_dir,
        )
        for tensor in inventory["tensors"]:
            tensors.setdefault(tensor["name"], tensor)
        for chunk in inventory["chunks"]:
            ref = refs.get(chunk["checksum"])
            if ref is None:
                raise DcpError(
                    f"missing DCP chunk descriptor for {chunk['tensor']}/rank{rank}"
                )
            shards.append(
                {
                    "tensor": chunk["tensor"],
                    "rank": chunk["rank"],
                    "uri": dcpzip_uri(
                        checkpoint_path / ref.relative_path,
                        name=ref.data_name,
                        data_offset=ref.data_offset,
                        size=ref.data_size,
                        compression=ref.data_compression,
                    ),
                    "offset": chunk["tensor_offset"],
                    "size": chunk["tensor_size"],
                    "checksum": chunk["checksum"],
                }
            )

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "dcp",
            "tensors": list(tensors.values()),
        },
        "source": {
            "mesh": {
                "shape": list(source_mesh_shape),
                "world_size": source_world,
                "placements": list(source_placements),
            },
            "storage": {"uri": checkpoint_path.as_uri()},
            "shards": sorted(
                shards,
                key=lambda row: (
                    row["tensor"],
                    row["rank"],
                    tuple(row["offset"]),
                ),
            ),
        },
        "target": {
            "mesh": {
                "shape": list(target_mesh_shape),
                "world_size": target_world_size,
                "placements": list(target_placements),
            },
            "storage": {"uri": target_storage_uri},
        },
    }
    validate_spec(spec)
    return spec
