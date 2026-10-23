from __future__ import annotations

import os
from itertools import product
from math import prod
from typing import Iterable

import numpy as np

from ..identity import resolve_source, source_id_for_shard, source_maps
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


def region_bytes(region_size: tuple[int, ...], item_size: int) -> int:
    return prod(region_size) * item_size


def byte_runs(
    local_shape: tuple[int, ...],
    local_offset: tuple[int, ...],
    region_size: tuple[int, ...],
    item_size: int,
) -> list[tuple[int, int]]:
    if not (
        len(local_shape) == len(local_offset) == len(region_size)
    ):
        raise TensorIOError("tensor region rank mismatch")
    if any(length <= 0 for length in region_size):
        return []
    for offset, length, dimension in zip(local_offset, region_size, local_shape):
        if offset < 0 or length < 0 or offset + length > dimension:
            raise TensorIOError("tensor region is outside local shape")

    strides = [1] * len(local_shape)
    for index in range(len(local_shape) - 2, -1, -1):
        strides[index] = strides[index + 1] * local_shape[index + 1]

    run_size = region_size[-1] * item_size
    outer_sizes = region_size[:-1]
    runs: list[tuple[int, int]] = []
    for outer in product(*(range(length) for length in outer_sizes)):
        element_offset = local_offset[-1]
        for dim, value in enumerate(outer):
            element_offset += (local_offset[dim] + value) * strides[dim]
        runs.append((element_offset * item_size, run_size))
    return runs


def coalesce_runs(
    runs: Iterable[tuple[int, int]],
    max_bytes: int = DEFAULT_RUN_BATCH_BYTES,
) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for offset, length in runs:
        if result:
            previous_offset, previous_length = result[-1]
            if (
                previous_offset + previous_length == offset
                and previous_length + length <= max_bytes
            ):
                result[-1] = (previous_offset, previous_length + length)
                continue
        result.append((offset, length))
    return result


def paired_run_batches(
    target_runs: Iterable[tuple[int, int]],
    source_run_lists: Iterable[Iterable[tuple[int, int]]],
    max_bytes: int = DEFAULT_RUN_BATCH_BYTES,
) -> list[tuple[tuple[int, int], list[tuple[int, int]]]]:
    target_list = _split_runs(target_runs, max_bytes)
    source_lists = [
        _split_runs(runs, max_bytes) for runs in source_run_lists
    ]
    if any(len(runs) != len(target_list) for runs in source_lists):
        raise TensorIOError("target and source run counts differ")

    batches: list[tuple[tuple[int, int], list[tuple[int, int]]]] = []
    target_start = 0
    while target_start < len(target_list):
        target_end = target_start + 1
        target_offset, target_length = target_list[target_start]
        source_offsets = [
            runs[target_start][0] for runs in source_lists
        ]
        source_lengths = [
            runs[target_start][1] for runs in source_lists
        ]
        while target_end < len(target_list):
            next_target_offset, next_target_length = target_list[target_end]
            if target_offset + target_length != next_target_offset:
                break
            if any(
                runs[target_end][0] != source_offsets[index] + source_lengths[index]
                for index, runs in enumerate(source_lists)
            ):
                break
            if target_length + next_target_length > max_bytes:
                break
            target_length += next_target_length
            for index, runs in enumerate(source_lists):
                source_lengths[index] += runs[target_end][1]
            target_end += 1
        batches.append(
            (
                (target_offset, target_length),
                list(zip(source_offsets, source_lengths)),
            )
        )
        target_start = target_end
    return batches


def _split_runs(
    runs: Iterable[tuple[int, int]],
    max_bytes: int,
) -> list[tuple[int, int]]:
    if max_bytes <= 0:
        raise TensorIOError("run batch size must be positive")
    result: list[tuple[int, int]] = []
    for offset, length in runs:
        if length <= 0:
            continue
        remaining = length
        cursor = offset
        while remaining > 0:
            chunk = min(remaining, max_bytes)
            result.append((cursor, chunk))
            cursor += chunk
            remaining -= chunk
    return result


def reduce_bytes(data: Iterable[bytes], op: str, dtype: str) -> bytes:
    values = list(data)
    if not values:
        raise TensorIOError(f"{op} requires at least one input")
    if op == "copy":
        if len(values) != 1:
            raise TensorIOError("copy requires exactly one input")
        return values[0]
    if dtype == "bfloat16":
        raise TensorIOError("bfloat16 reduction is not supported yet")
    try:
        numpy_dtype = np.dtype(dtype)
    except TypeError as exc:
        raise TensorIOError(f"unsupported dtype: {dtype}") from exc
    arrays = [np.frombuffer(value, dtype=numpy_dtype) for value in values]
    if len({array.size for array in arrays}) != 1:
        raise TensorIOError("reduction inputs have different sizes")
    stacked = np.stack(arrays)
    if op == "sum":
        reduced = np.add.reduce(stacked, axis=0, dtype=numpy_dtype)
    elif op == "avg":
        reduced = np.mean(stacked, axis=0).astype(numpy_dtype)
    elif op == "max":
        reduced = np.max(stacked, axis=0)
    elif op == "min":
        reduced = np.min(stacked, axis=0)
    else:
        raise TensorIOError(f"unsupported reduction op: {op}")
    return np.asarray(reduced, dtype=numpy_dtype).tobytes()


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
    for identifier in sorted(used):
        shard = used[identifier]
        uri = shard["uri"]
        if not storage.exists(uri):
            raise StorageError(f"source shard not found: {uri}")
        actual = storage.checksum(uri)
        if actual != shard["checksum"]:
            raise StorageError(
                f"source checksum mismatch for {shard['tensor']}/rank{shard['rank']}: "
                f"expected {shard['checksum']}, got {actual}"
            )
    return len(used)


def item_size(dtype: str) -> int:
    try:
        return DTYPE_BYTES[dtype]
    except KeyError as exc:
        raise TensorIOError(f"unsupported dtype: {dtype}") from exc
