#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import pickle
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shardferry.dcp import export_dcp, inspect_dcp, migrate_dcp
from shardferry.executor import apply
from shardferry.planning import plan
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)
from dcp_range_benchmark import build_sparse_stored_zip


def rss_kb() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage)


def tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def make_source_dcp(root: Path, size_mb: int, source_world: int) -> Path:
    elements = max(1, size_mb * 1024 * 1024 // 4)
    spec, arrays, _ = generate_sharded_spec(
        (elements,),
        (source_world,),
        ("Shard(0)",),
        (source_world,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, root)
    apply(plan(spec), spec)
    checkpoint = root / "source.dcp"
    export_dcp(spec, checkpoint)
    return checkpoint


def make_sparse_dcp(root: Path, size_bytes: int) -> Path:
    import torch
    from torch.distributed.checkpoint.filesystem import _StorageInfo
    from torch.distributed.checkpoint.metadata import (
        ChunkStorageMetadata,
        Metadata,
        MetadataIndex,
        StorageMeta,
        TensorProperties,
        TensorStorageMetadata,
    )

    checkpoint = root / "sparse.dcp"
    checkpoint.mkdir(parents=True)
    chunk = checkpoint / "__0_0.distcp"
    build_sparse_stored_zip(chunk, size_bytes)
    metadata = Metadata(
        state_dict_metadata={
            "w": TensorStorageMetadata(
                properties=TensorProperties(dtype=torch.float32),
                size=torch.Size([size_bytes // 4]),
                chunks=[
                    ChunkStorageMetadata(
                        offsets=torch.Size([0]),
                        sizes=torch.Size([size_bytes // 4]),
                    )
                ],
            )
        },
        planner_data={"w": ("w",)},
        storage_data={
            MetadataIndex(
                fqn="w",
                offset=torch.Size([0]),
                index=0,
            ): _StorageInfo(
                relative_path="__0_0.distcp",
                offset=0,
                length=chunk.stat().st_size,
            )
        },
        storage_meta=StorageMeta(
            checkpoint_id=checkpoint,
            save_id="benchmark",
        ),
        version="1.0.0",
    )
    with (checkpoint / ".metadata").open("wb") as stream:
        pickle.dump(metadata, stream)
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=64)
    parser.add_argument("--source-world", type=int, default=4)
    parser.add_argument("--target-world", type=int, default=3)
    parser.add_argument("--sparse-gb", type=int, default=0)
    parser.add_argument("--skip-legacy", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="shardferry-migrate-bench-"))
    result: dict[str, object] = {
        "size_mb": args.size_mb,
        "source_world": args.source_world,
        "target_world": args.target_world,
    }
    try:
        if args.sparse_gb:
            checkpoint = make_sparse_dcp(
                root / "source",
                args.sparse_gb * 1024 * 1024 * 1024,
            )
            args.source_world = 1
            args.target_world = 1
            result["size_mb"] = args.sparse_gb * 1024
            result["source_world"] = 1
            result["target_world"] = 1
        else:
            checkpoint = make_source_dcp(
                root / "source",
                args.size_mb,
                args.source_world,
            )

        new_work = root / "new-work"
        new_output = root / "new.dcp"
        before = rss_kb()
        started = time.perf_counter()
        migrate_dcp(
            checkpoint,
            new_output,
            new_work,
            target_world_size=args.target_world,
        )
        result["zero_materialization"] = {
            "seconds": time.perf_counter() - started,
            "work_bytes": tree_bytes(new_work),
            "source_raw_exists": (new_work / "source").exists(),
            "rss_kb": rss_kb(),
            "rss_before_kb": before,
        }

        if not args.skip_legacy:
            old_work = root / "old-work"
            old_output = root / "old.dcp"
            started = time.perf_counter()
            old_spec = inspect_dcp(
                checkpoint,
                old_work,
                target_world_size=args.target_world,
            )
            old_plan = plan(old_spec)
            apply(old_plan, old_spec)
            export_dcp(old_spec, old_output)
            result["legacy_materialized"] = {
                "seconds": time.perf_counter() - started,
                "work_bytes": tree_bytes(old_work),
                "source_raw_exists": (old_work / "source").exists(),
                "rss_kb": rss_kb(),
            }
        result["ok"] = True
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
