from __future__ import annotations

import hashlib
from math import prod
from pathlib import Path
from urllib.parse import quote

import numpy as np

from tests.fixtures.toy import make_tensor
from tests.oracle.reference import reference_reshard


def mesh_payload(
    shape: tuple[int, ...],
    placements: tuple[str, ...],
) -> dict:
    return {
        "shape": list(shape),
        "world_size": prod(shape),
        "placements": list(placements),
    }


def checksum(array: np.ndarray) -> str:
    return "sha256:" + hashlib.sha256(array.tobytes()).hexdigest()


def generate_sharded_spec(
    tensor_shape: tuple[int, ...],
    source_shape: tuple[int, ...],
    source_placements: tuple[str, ...],
    target_shape: tuple[int, ...],
    target_placements: tuple[str, ...],
) -> tuple[dict, dict[tuple[str, int], np.ndarray], np.ndarray]:
    tensor = make_tensor(tensor_shape)
    source_arrays: dict[tuple[str, int], np.ndarray] = {}
    shards = []
    for rank, (offset, array) in reference_reshard(
        tensor, source_shape, source_placements
    ).items():
        source_arrays[("w", rank)] = array
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": f"file:///source/rank{rank}.bin",
                "offset": list(offset),
                "size": list(array.shape),
                "checksum": checksum(array),
            }
        )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [
                {
                    "name": "w",
                    "shape": list(tensor_shape),
                    "dtype": "float32",
                }
            ],
        },
        "source": {
            "mesh": mesh_payload(source_shape, source_placements),
            "storage": {"uri": "file:///source"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload(target_shape, target_placements),
            "storage": {"uri": "file:///target"},
        },
    }
    return spec, source_arrays, tensor


def generate_partial_source_spec(
    tensor_shape: tuple[int, ...],
    source_world_size: int,
    target_shape: tuple[int, ...],
    target_placements: tuple[str, ...],
) -> tuple[dict, dict[tuple[str, int], np.ndarray], np.ndarray]:
    tensor = make_tensor(tensor_shape)
    source_arrays = {}
    shards = []
    for rank in range(source_world_size):
        array = tensor.copy() if rank == 0 else np.zeros_like(tensor)
        source_arrays[("w", rank)] = array
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": f"file:///partial/rank{rank}.bin",
                "offset": [0] * len(tensor_shape),
                "size": list(tensor_shape),
                "checksum": checksum(array),
            }
        )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [
                {
                    "name": "w",
                    "shape": list(tensor_shape),
                    "dtype": "float32",
                }
            ],
        },
        "source": {
            "mesh": mesh_payload(
                (source_world_size,), (f"Partial(sum)",)
            ),
            "storage": {"uri": "file:///partial"},
            "shards": shards,
        },
        "target": {
            "mesh": mesh_payload(target_shape, target_placements),
            "storage": {"uri": "file:///target"},
        },
    }
    return spec, source_arrays, tensor


def materialize_source_files(
    spec: dict,
    source_arrays: dict[tuple[str, int], np.ndarray],
    root: Path,
) -> dict:
    root = root.resolve()
    source_root = root / "source"
    target_root = root / "target"
    source_root.mkdir(parents=True, exist_ok=True)
    target_root.mkdir(parents=True, exist_ok=True)
    for shard in spec["source"]["shards"]:
        array = source_arrays[(shard["tensor"], shard["rank"])]
        path = source_root / quote(shard["tensor"], safe="") / f"rank{shard['rank']}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(array.tobytes())
        shard["uri"] = path.as_uri()
    spec["source"]["storage"]["uri"] = source_root.as_uri()
    spec["target"]["storage"]["uri"] = target_root.as_uri()
    return spec
