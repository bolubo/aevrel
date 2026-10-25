from __future__ import annotations

import hmac
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qs, unquote, urlsplit

from .. import fastkernels
from ..identity import resolve_source, source_maps
from ..parallel import worker_count
from ..planning.layout import DTYPE_BYTES, local_region
from ..planning.mesh import parse_mesh
from ..planning import validate_plan
from ..planning.tensor_io import (
    byte_runs,
    paired_run_batches,
    reduce_bytes,
    region_bytes,
    verify_source_checksums,
)
from ..storage.file import FileStorage
from ..storage.router import StorageRouter
from ..targets import target_file_path


class VerificationError(ValueError):
    """Raised when a target checkpoint does not match the plan."""


def _local_source(uri: str) -> tuple[str, int] | None:
    """Path and starting byte offset for a locally readable source.

    Returns `None` for anything the kernel cannot read directly: HTTP sources,
    or a DCP entry that is actually compressed and needs a decompressor.
    """
    parts = urlsplit(uri)
    if parts.scheme == "file":
        return (unquote(parts.path), 0)
    if parts.scheme != "dcpzip":
        return None
    query = parse_qs(parts.query)
    try:
        offset = int(query["offset"][0])
        compression = int(query["compression"][0])
    except (KeyError, IndexError, ValueError):
        return None
    if compression != 0:  # zipfile.ZIP_STORED
        return None
    return (unquote(parts.path), offset)


def _verify_node_locally(
    target_path: Path,
    source_uris: list[str],
    batches: list[Any],
    op: str,
    dtype: str,
    tensor_name: str,
    rank: int,
) -> int | None:
    """Verify one node in a single native scan.

    Returns the number of bytes verified, or `None` when any source cannot be
    read as a local byte range and the caller has to fall back to the storage
    layer. Raises `VerificationError` on the first mismatch, like the loop it
    replaces.
    """
    resolved = [_local_source(uri) for uri in source_uris]
    if any(item is None for item in resolved):
        return None
    resolved = [item for item in resolved if item is not None]
    paths = [item[0] for item in resolved]
    bases = [item[1] for item in resolved]
    native_batches = [
        (
            target_offset,
            target_size,
            [
                (base + offset, size)
                for (offset, size), base in zip(sources, bases)
            ],
        )
        for (target_offset, target_size), sources in batches
    ]
    outcome = fastkernels.verify_batches(
        str(target_path), paths, native_batches, op, dtype
    )
    if outcome is not None:
        raise VerificationError(
            f"bitwise mismatch for {tensor_name}/rank{rank} "
            f"at {target_path} byte {outcome}"
        )
    return sum(target_size for (_, target_size), _ in batches)


class _NodeJob(NamedTuple):
    """One target node to verify, with everything its runners need."""

    tensor_name: str
    rank: int
    dtype: str
    op: str
    path: Path
    target_uri: str
    source_uris: list[str]
    batches: list[Any]
    node: dict[str, Any]


def _run_local_job(job: _NodeJob) -> tuple[str, Any]:
    """Try the native scan for one node. Never raises; reports a status.

    The pool needs a function that returns instead of raising so every task
    runs to completion and the first failure (in plan order) is the one we
    surface -- exactly the ordering the sequential loop produced.
    """
    try:
        verified = _verify_node_locally(
            job.path,
            job.source_uris,
            job.batches,
            job.op,
            job.dtype,
            job.tensor_name,
            job.rank,
        )
    except VerificationError as error:
        return ("fail", error)
    if verified is None:
        return ("fallback", None)
    return ("ok", verified)


def _verify_node_via_storage(
    job: _NodeJob,
    source_by_id: dict,
    source_by_rank: dict,
    source_storage: StorageRouter,
    target_storage: FileStorage,
) -> int:
    """Storage-backed verify for a node the native kernel could not read.

    This is the path taken when a source lives on HTTP (or any scheme the
    kernel declines): it goes through the storage router, which already knows
    how to fetch remote ranges and how to read compressed DCP entries.
    """
    bytes_verified = 0
    for target_range, source_ranges in job.batches:
        target_offset, run_size = target_range
        if job.op == "zeros":
            expected = b"\0" * run_size
        else:
            source_data = [
                source_storage.read_range(
                    resolve_source(
                        source_by_id,
                        source_by_rank,
                        job.tensor_name,
                        source_input,
                    )["uri"],
                    source_offset,
                    source_size,
                )
                for source_input, (source_offset, source_size) in zip(
                    job.node["inputs"], source_ranges
                )
            ]
            expected = reduce_bytes(source_data, job.op, job.dtype)
        actual = target_storage.read_range(job.target_uri, target_offset, run_size)
        if not hmac.compare_digest(actual, expected):
            raise VerificationError(
                f"bitwise mismatch for {job.tensor_name}/rank{job.rank} "
                f"at {job.node['target_offset']}"
            )
        bytes_verified += run_size
    return bytes_verified


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

    jobs: list[_NodeJob] = []
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
                source_uris: list[str] = []
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
                    source_uris.append(source_shard["uri"])

                batches = paired_run_batches(target_runs, source_run_lists)
                jobs.append(
                    _NodeJob(
                        tensor_name=tensor_name,
                        rank=rank,
                        dtype=dtype,
                        op=node["op"],
                        path=path,
                        target_uri=uri,
                        source_uris=source_uris,
                        batches=batches,
                        node=node,
                    )
                )

    # Every node is independent: it owns one target file and its source ranges,
    # and `verify_batches` releases the GIL for the whole scan. The only thing
    # holding that back used to be `sha256_file`, which did not -- see
    # `native/aevrel-fast/src/lib.rs`. The pool preserves plan order, so the
    # first mismatch in plan order is the one we surface, matching the
    # sequential loop it replaced.
    workers = worker_count(
        len(jobs), (uri for job in jobs for uri in job.source_uris)
    )
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(_run_local_job, jobs))
    else:
        outcomes = [_run_local_job(job) for job in jobs]

    bytes_verified = 0
    for index, (status, payload) in enumerate(outcomes):
        if status == "fail":
            raise payload
        if status == "ok":
            bytes_verified += payload
        else:  # fallback: the native kernel could not read a source
            bytes_verified += _verify_node_via_storage(
                jobs[index],
                source_by_id,
                source_by_rank,
                source_storage,
                target_storage,
            )

    return {
        "passed": True,
        "method": "bitwise",
        "details": (
            f"{len(plan['nodes']) if target_rank is None else 'rank-scoped'} "
            f"nodes, {bytes_verified} target bytes verified"
        ),
    }
