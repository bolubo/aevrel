from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .defaults import DEFAULT_DEMO_DIR
from .executor import apply
from .layout import local_region
from .mesh import parse_mesh
from .planner import plan
from .verify import verify


class DemoError(RuntimeError):
    """Raised when the local demo cannot start."""


def run_demo(work_dir: str | Path | None = None) -> dict[str, Any]:
    root = (
        Path(work_dir).resolve()
        if work_dir is not None
        else (Path.cwd() / DEFAULT_DEMO_DIR).resolve()
    )
    if root.exists() and any(root.iterdir()):
        raise DemoError(
            f"demo directory already exists and is not empty: {root}; "
            "choose another --work-dir"
        )
    source_dir = root / "source"
    target_dir = root / "target"
    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    tensor = np.arange(32, dtype=np.float32).reshape(8, 4)
    source_mesh = parse_mesh(
        {
            "shape": [2, 2],
            "world_size": 4,
            "placements": ["Shard(0)", "Shard(1)"],
        }
    )
    shards = []
    for rank in range(source_mesh.world_size):
        region = local_region(
            source_mesh,
            source_mesh.placements,
            tensor.shape,
            rank,
        )
        slices = tuple(
            slice(start, start + length)
            for start, length in zip(region.offset, region.size)
        )
        data = tensor[slices].copy().tobytes()
        path = source_dir / "w" / f"rank{rank}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": path.as_uri(),
                "offset": list(region.offset),
                "size": list(region.size),
                "checksum": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [
                {"name": "w", "shape": [8, 4], "dtype": "float32"}
            ],
        },
        "source": {
            "mesh": {
                "shape": [2, 2],
                "world_size": 4,
                "placements": ["Shard(0)", "Shard(1)"],
            },
            "storage": {"uri": source_dir.as_uri()},
            "shards": shards,
        },
        "target": {
            "mesh": {
                "shape": [3, 1],
                "world_size": 3,
                "placements": ["Shard(0)", "Replicate()"],
            },
            "storage": {"uri": target_dir.as_uri()},
        },
    }
    spec_path = root / "migration.yaml"
    spec_path.write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )
    plan_payload = plan(spec)
    report = apply(plan_payload, spec)
    verification = verify(spec, plan_payload)
    (root / "plan.json").write_text(
        json.dumps(plan_payload, indent=2),
        encoding="utf-8",
    )
    (root / "report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    return {
        "work_dir": str(root),
        "spec": str(spec_path),
        "plan": str(root / "plan.json"),
        "report": str(root / "report.json"),
        "verification": verification,
    }
