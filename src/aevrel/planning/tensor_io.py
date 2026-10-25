from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from math import prod
from typing import Callable, Iterable, TypeVar

from .. import fastkernels
from ..identity import resolve_source, source_id_for_shard, source_maps
from ..parallel import worker_count
from .layout import DTYPE_BYTES
from ..storage.file import StorageError


class TensorIOError(ValueError):
    """Raised when a tensor byte range cannot be read, reduced, or written."""


def _default_run_batch_bytes() -> int:
    configured = os.environ.get("AEVREL_RUN_BATCH_BYTES", "")
    if not configured:
        codec = os.environ.get("AEVREL_GPU_CODEC", "").strip().lower()
        if codec not in {"", "off", "identity"}:
            return 16 * 1024 * 1024
        return 4 * 1024 * 1024
    try:
        value = int(configured)
    except ValueError:
        return 4 * 1024 * 1024
    return value if value > 0 else 4 * 1024 * 1024


DEFAULT_RUN_BATCH_BYTES = _default_run_batch_bytes()

T = TypeVar("T")


def _native(kernel: Callable[..., T], *args: object) -> T:
    """Call a native kernel, mapping its `ValueError` onto `TensorIOError`.

    The native kernels reproduce the exact messages the retired Python
    implementations raised, so callers see the same text and only the exception
    class changes to the domain-specific one they already catch.
    """
    try:
        return kernel(*args)
    except ValueError as exc:
        raise TensorIOError(str(exc)) from None


def region_bytes(region_size: tuple[int, ...], item_size: int) -> int:
    return prod(region_size) * item_size


def byte_runs(
    local_shape: tuple[int, ...],
    local_offset: tuple[int, ...],
    region_size: tuple[int, ...],
    item_size: int,
) -> list[tuple[int, int]]:
    """Contiguous byte runs covering one region of a row-major local tensor."""
    return _native(
        fastkernels.byte_runs, local_shape, local_offset, region_size, item_size
    )


def coalesce_runs(
    runs: Iterable[tuple[int, int]],
    max_bytes: int = DEFAULT_RUN_BATCH_BYTES,
) -> list[tuple[int, int]]:
    """Merge physically adjacent runs without exceeding `max_bytes`."""
    return _native(fastkernels.coalesce_runs, runs, max_bytes)


def paired_run_batches(
    target_runs: Iterable[tuple[int, int]],
    source_run_lists: Iterable[Iterable[tuple[int, int]]],
    max_bytes: int = DEFAULT_RUN_BATCH_BYTES,
) -> list[tuple[tuple[int, int], list[tuple[int, int]]]]:
    """Pair target runs with source runs, merging both sides in lock step."""
    return _native(
        fastkernels.paired_run_batches, target_runs, source_run_lists, max_bytes
    )


def _split_runs(
    runs: Iterable[tuple[int, int]],
    max_bytes: int,
) -> list[tuple[int, int]]:
    return _native(fastkernels.split_runs, runs, max_bytes)


def reduce_bytes(data: Iterable[bytes], op: str, dtype: str) -> bytes:
    """Reduce a stack of equally sized buffers element-wise."""
    return _native(fastkernels.reduce_bytes, data, op, dtype)


def verify_source_checksums(
    spec: dict,
    plan: dict,
    storage,
    target_rank: int | None = None,
) -> int:
    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    used: dict[str, dict] = {}
    for node in plan["nodes"]:
        if target_rank is not None and node["target_rank"] != target_rank:
            continue
        for source_input in node["inputs"]:
            shard = resolve_source(
                source_by_id,
                source_by_rank,
                node["tensor"],
                source_input,
            )
            used[source_id_for_shard(shard)] = shard
    identifiers = sorted(used)

    def check(identifier: str) -> "StorageError | None":
        shard = used[identifier]
        uri = shard["uri"]
        if not storage.exists(uri):
            return StorageError(f"source shard not found: {uri}")
        actual = storage.checksum(uri)
        if actual != shard["checksum"]:
            return StorageError(
                f"source checksum mismatch for {shard['tensor']}/rank{shard['rank']}: "
                f"expected {shard['checksum']}, got {actual}"
            )
        return None

    workers = worker_count(len(identifiers), (used[i]["uri"] for i in identifiers))
    if workers > 1:
        # All shards are hashed; the first failure (in sorted-id order) is the
        # one we surface, exactly as the sequential loop did. The native kernels
        # release the GIL for the whole scan, so hashing actually overlaps.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(check, identifiers))
    else:
        outcomes = [check(identifier) for identifier in identifiers]
    for outcome in outcomes:
        if outcome is not None:
            raise outcome
    return len(used)


def item_size(dtype: str) -> int:
    try:
        return DTYPE_BYTES[dtype]
    except KeyError as exc:
        raise TensorIOError(f"unsupported dtype: {dtype}") from exc
