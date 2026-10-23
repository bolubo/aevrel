from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from ..identity import resolve_source, source_maps
from ..planning.layout import DTYPE_BYTES, local_region
from ..planning.mesh import parse_mesh
from ..planning import validate_plan
from ..storage.file import FileStorage
from ..storage.router import StorageRouter
from ..targets import target_file_path
from ..planning.tensor_io import (
    byte_runs,
    paired_run_batches,
    reduce_bytes,
    region_bytes,
    verify_source_checksums,
)


class VerificationError(ValueError):
    """Raised when a target checkpoint does not match the plan."""


def verify(
    spec: dict[str, Any],
    plan: dict[str, Any],
    report: dict[str, Any] | None = None,
    *,
    target_paths: dict[tuple[str, int], Path] | None = None,
    target_rank: int | None = None,
    target_root: str | Path | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    del report
    validate_plan(plan, spec)
    source_storage = StorageRouter(token=token)
    target_storage = FileStorage()
    verify_source_checksums(spec, plan, source_storage, target_rank=target_rank)

    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    nodes_by_target: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for node in plan["nodes"]:
        nodes_by_target.setdefault(
            (node["tensor"], node["target_rank"]), []
        ).append(node)

    target_mesh = parse_mesh(spec["target"]["mesh"])
    if target_rank is not None and not 0 <= target_rank < target_mesh.world_size:
        raise VerificationError(f"target rank {target_rank} is out of range")
    target_placements = target_mesh.placements
    bytes_verified = 0

    for tensor_name, tensor in tensors.items():
        shape = tuple(tensor["shape"])
        dtype = tensor["dtype"]
        bytes_per_item = DTYPE_BYTES[dtype]
        for rank in range(target_mesh.world_size):
            if target_rank is not None and rank != target_rank:
                continue
            expected_region = local_region(
                target_mesh,
                target_placements,
                shape,
                rank,
                allow_partial=True,
            )
            expected_size = region_bytes(expected_region.size, bytes_per_item)
            path = (
                target_paths[(tensor_name, rank)]
                if target_paths is not None
                else target_file_path(spec, tensor_name, rank, root=target_root)
            )
            uri = path.as_uri()
            if not target_storage.exists(uri):
                raise VerificationError(f"target file not found: {path}")
            actual_size = target_storage.size(uri)
            if actual_size != expected_size:
                raise VerificationError(
                    f"target size mismatch for {tensor_name}/rank{rank}: "
                    f"expected {expected_size}, got {actual_size}"
                )

            for node in nodes_by_target.get((tensor_name, rank), []):
                local_offset = tuple(
                    value - base
                    for value, base in zip(
                        node["target_offset"], expected_region.offset
                    )
                )
                target_runs = byte_runs(
                    expected_region.size,
                    local_offset,
                    tuple(node["size"]),
                    bytes_per_item,
                )
                source_run_lists = []
                for source_input in node["inputs"]:
                    source_shard = resolve_source(
                        source_by_id,
                        source_by_rank,
                        tensor_name,
                        source_input,
                    )
                    source_local_offset = tuple(
                        value - base
                        for value, base in zip(
                            source_input["source_offset"],
                            source_shard["offset"],
                        )
                    )
                    source_run_lists.append(
                        byte_runs(
                            tuple(source_shard["size"]),
                            source_local_offset,
                            tuple(node["size"]),
                            bytes_per_item,
                        )
                    )

                for target_range, source_ranges in paired_run_batches(
                    target_runs, source_run_lists
                ):
                    target_offset, run_size = target_range
                    if node["op"] == "zeros":
                        expected = b"\0" * run_size
                    else:
                        source_data = [
                            source_storage.read_range(
                                resolve_source(
                                    source_by_id,
                                    source_by_rank,
                                    tensor_name,
                                    source_input,
                                )["uri"],
                                source_offset,
                                source_size,
                            )
                            for source_input, (source_offset, source_size) in zip(
                                node["inputs"], source_ranges
                            )
                        ]
                        expected = reduce_bytes(source_data, node["op"], dtype)
                    actual = target_storage.read_range(uri, target_offset, run_size)
                    if not hmac.compare_digest(actual, expected):
                        raise VerificationError(
                            f"bitwise mismatch for {tensor_name}/rank{rank} "
                            f"at {node['target_offset']}"
                        )
                    bytes_verified += run_size

    return {
        "passed": True,
        "method": "bitwise",
        "details": (
            f"{len(plan['nodes']) if target_rank is None else 'rank-scoped'} "
            f"nodes, {bytes_verified} target bytes verified"
        ),
    }
