from __future__ import annotations

import numpy as np


def make_tensor(shape: tuple[int, ...] = (8, 4), dtype=np.float32) -> np.ndarray:
    return np.arange(np.prod(shape), dtype=dtype).reshape(shape)


def make_mesh(
    shape: tuple[int, ...] = (2, 2),
    placements: tuple[str, ...] = ("Shard(0)", "Shard(1)"),
) -> dict:
    return {
        "shape": list(shape),
        "world_size": int(np.prod(shape)),
        "placements": list(placements),
    }


def make_toy_spec() -> dict:
    return {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [8, 4], "dtype": "float32"}],
        },
        "source": {
            "mesh": make_mesh((2, 2), ("Shard(0)", "Shard(1)")),
            "storage": {"uri": "file:///tmp/shardferry/source"},
            "shards": [],
        },
        "target": {
            "mesh": make_mesh((3, 1), ("Shard(0)", "Replicate()")),
            "storage": {"uri": "file:///tmp/shardferry/target"},
        },
    }
