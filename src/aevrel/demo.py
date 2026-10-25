from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .defaults import DEFAULT_DEMO_DIR
from .executor import apply
from .planning.layout import local_region
from .planning.mesh import parse_mesh
from .planning import plan
from .verification import verify


class DemoError(RuntimeError):
    """Raised when the local demo cannot start."""


def run_demo(
    work_dir: str | Path | None = None,
    *,
    include_dcp: bool = False,
) -> dict[str, Any]:
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
    result = {
        "work_dir": str(root),
        "spec": str(spec_path),
        "plan": str(root / "plan.json"),
        "report": str(root / "report.json"),
        "verification": verification,
    }
    if include_dcp:
        result["dcp"] = _run_dcp_demo(root, spec)
    return result


def _run_dcp_demo(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    from .dcp import export_dcp, migrate_dcp
    from .local.dcp import DCPStateProvider
    from .local.models import DrillOutcome
    from .local.readiness import assess_readiness
    from .verification import DcpTargetReady

    try:
        source_spec = deepcopy(spec)
        source_spec["target"] = deepcopy(spec["source"])
        source_spec["target"]["storage"] = deepcopy(
            spec["source"]["storage"]
        )
        source_dcp = export_dcp(source_spec, root / "source.dcp")
        migrate_result = migrate_dcp(
            source_dcp,
            root / "target-3.dcp",
            root / "dcp-work",
            target_world_size=3,
        )
        target_dcp = Path(migrate_result["output"])
        provider = DCPStateProvider()
        source_record = provider.inventory(source_dcp)
        target_record = provider.inventory(target_dcp)
        # The demo drill loads the restored state and takes real optimizer
        # steps on it, so the readiness verdict it reports is the verdict the
        # evidence supports. A load-only drill would be a different, weaker
        # claim.
        steps = 3
        DcpTargetReady(source_dcp, steps=steps)("demo-source")
        DcpTargetReady(target_dcp, steps=steps)("demo")
    except Exception as exc:
        raise DemoError(f"DCP demo failed: {exc}") from exc

    def drill(record, proof_ref: str) -> DrillOutcome:
        return DrillOutcome(
            phase="Succeeded",
            succeeded=True,
            steps_ran=steps,
            proof_ref=proof_ref,
            ranks_reported=tuple(rank.rank for rank in record.ranks),
            ranks_expected=len(record.ranks),
            checkpoint_version=record.version,
            checkpoint_metadata_digest=record.metadata_digest,
        )

    return {
        "source": str(source_dcp),
        "target": str(target_dcp),
        "source_readiness": assess_readiness(
            source_record,
            target_world_size=3,
            drill=drill(source_record, "demo-source-dcp-load"),
        ).to_dict(),
        "target_readiness": assess_readiness(
            target_record,
            target_world_size=3,
            drill=drill(target_record, "demo-target-dcp-load"),
        ).to_dict(),
        "target_load": "ok",
    }
