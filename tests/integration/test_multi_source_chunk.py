from __future__ import annotations

from pathlib import Path

import numpy as np

from aevrel.executor import apply
from aevrel.planning import plan
from tests.fixtures.generated import checksum, mesh_payload


def test_multiple_source_chunks_per_rank_execute_bitwise(tmp_path: Path) -> None:
    tensor = np.arange(8, dtype=np.float32)
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()

    left_path = source_root / "rank0-left.bin"
    right_path = source_root / "rank0-right.bin"
    left = tensor[:4].copy()
    right = tensor[4:].copy()
    left_path.write_bytes(left.tobytes())
    right_path.write_bytes(right.tobytes())

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [{"name": "w", "shape": [8], "dtype": "float32"}],
        },
        "source": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": source_root.as_uri()},
            "shards": [
                {
                    "tensor": "w",
                    "rank": 0,
                    "uri": left_path.as_uri(),
                    "offset": [0],
                    "size": [4],
                    "checksum": checksum(left),
                },
                {
                    "tensor": "w",
                    "rank": 0,
                    "uri": right_path.as_uri(),
                    "offset": [4],
                    "size": [4],
                    "checksum": checksum(right),
                },
            ],
        },
        "target": {
            "mesh": mesh_payload((2,), ("Shard(0)",)),
            "storage": {"uri": target_root.as_uri()},
        },
    }

    plan_payload = plan(spec)
    report = apply(plan_payload, spec, target_root=target_root)

    assert report["status"] == "success"
    assert report["chunks"] == 2
    assert np.fromfile(
        target_root / "w" / "rank0.bin", dtype=np.float32
    ).tolist() == left.tolist()
    assert np.fromfile(
        target_root / "w" / "rank1.bin", dtype=np.float32
    ).tolist() == right.tolist()
