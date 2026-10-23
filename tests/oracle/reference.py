from __future__ import annotations

from math import prod

import numpy as np

from aevrel.identity import resolve_source, source_id_for_shard, source_maps


def gather_to_global(shards: list[dict], shape: tuple[int, ...]) -> np.ndarray:
    """Brute-force oracle: place every shard into a dense global tensor."""
    if not shards:
        return np.zeros(shape, dtype=np.float32)
    out = np.zeros(shape, dtype=shards[0]["data"].dtype)
    for shard in shards:
        offset = tuple(shard["offset"])
        size = tuple(shard["size"])
        slices = tuple(slice(o, o + s) for o, s in zip(offset, size))
        out[slices] = shard["data"]
    return out


def reshard_1d(
    global_tensor: np.ndarray,
    world_size: int,
    placement: str,
) -> list[np.ndarray]:
    """Brute-force oracle for a 1D mesh: Replicate or Shard(dim)."""
    if placement == "Replicate()":
        return [global_tensor.copy() for _ in range(world_size)]
    if placement.startswith("Shard(") and placement.endswith(")"):
        dim = int(placement[len("Shard("):-1])
        return [chunk.copy() for chunk in np.array_split(global_tensor, world_size, axis=dim)]
    raise ValueError(f"unsupported placement: {placement}")


def assert_bitwise_equal(left: np.ndarray, right: np.ndarray) -> None:
    if left.shape != right.shape or not np.array_equal(left, right):
        raise AssertionError("tensors are not bitwise equal")


def reference_local_region(
    shape: tuple[int, ...],
    mesh_shape: tuple[int, ...],
    placements: tuple[str, ...],
    rank: int,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    coord = np.unravel_index(rank, mesh_shape)
    offset = [0] * len(shape)
    size = list(shape)
    for axis, placement in enumerate(placements):
        if placement == "Replicate()":
            continue
        if placement.startswith("Partial("):
            raise ValueError("Partial placement has no direct local region")
        dim = int(placement[len("Shard("):-1])
        chunks = np.array_split(np.arange(size[dim]), mesh_shape[axis])
        lengths = [len(chunk) for chunk in chunks]
        offset[dim] += sum(lengths[: coord[axis]])
        size[dim] = lengths[coord[axis]]
    return tuple(offset), tuple(size)


def reference_reshard(
    tensor: np.ndarray,
    mesh_shape: tuple[int, ...],
    placements: tuple[str, ...],
) -> dict[int, tuple[tuple[int, ...], np.ndarray]]:
    result = {}
    for rank in range(prod(mesh_shape)):
        offset, size = reference_local_region(
            tensor.shape, mesh_shape, placements, rank
        )
        slices = tuple(
            slice(start, start + length) for start, length in zip(offset, size)
        )
        result[rank] = (offset, tensor[slices].copy())
    return result


def materialize_plan(
    plan: dict,
    spec: dict,
    source_arrays: dict[tuple[str, int], np.ndarray],
) -> dict[tuple[str, int], np.ndarray]:
    target_mesh = spec["target"]["mesh"]
    target_shape = tuple(target_mesh["shape"])
    target_placements = tuple(target_mesh["placements"])
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    targets: dict[tuple[str, int], np.ndarray] = {}
    for tensor_name, tensor in tensors.items():
        for rank in range(target_mesh["world_size"]):
            offset, size = reference_local_region(
                tuple(tensor["shape"]),
                target_shape,
                target_placements,
                rank,
            )
            targets[(tensor_name, rank)] = (
                offset,
                np.zeros(size, dtype=np.dtype(tensor["dtype"])),
            )

    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    for node in plan["nodes"]:
        tensor_name = node["tensor"]
        target_rank = node["target_rank"]
        target_offset, target_array = targets[(tensor_name, target_rank)]
        local_offset = tuple(
            value - base
            for value, base in zip(node["target_offset"], target_offset)
        )
        local_slices = tuple(
            slice(start, start + length)
            for start, length in zip(local_offset, node["size"])
        )

        values = []
        for source_input in node["inputs"]:
            source_shard = resolve_source(
                source_by_id,
                source_by_rank,
                tensor_name,
                source_input,
            )
            identifier = source_id_for_shard(source_shard)
            source_key = (
                (tensor_name, identifier)
                if (tensor_name, identifier) in source_arrays
                else (tensor_name, int(source_shard["rank"]))
            )
            source_local_offset = tuple(
                value - base
                for value, base in zip(
                    source_input["source_offset"], source_shard["offset"]
                )
            )
            source_slices = tuple(
                slice(start, start + length)
                for start, length in zip(source_local_offset, node["size"])
            )
            values.append(source_arrays[source_key][source_slices])

        if node["op"] == "copy":
            value = values[0]
        elif node["op"] == "sum":
            value = np.sum(values, axis=0)
        elif node["op"] == "avg":
            value = np.mean(values, axis=0)
        elif node["op"] == "max":
            value = np.max(values, axis=0)
        elif node["op"] == "min":
            value = np.min(values, axis=0)
        else:
            raise ValueError(f"unknown op: {node['op']}")
        target_array[local_slices] = value
    return {key: value for key, (_, value) in targets.items()}
