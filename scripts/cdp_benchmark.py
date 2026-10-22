from __future__ import annotations

import argparse
import hashlib
import json
import random
import resource
import sys
import time
from pathlib import Path

from shardferry.acceptance import check_report
from shardferry.cdp import (
    CheckpointFollower,
    ChunkingPolicy,
    VersionMarker,
    VersionTarget,
    write_marker,
)
from shardferry.cutover import CutoverPolicy, CutoverSession
from shardferry.planner import plan
from shardferry.policy import AdaptiveChunkingPolicy
from shardferry.target_layout import DirectIncrementalApplyFileProvider


MiB = 1024 * 1024


def zero_checksum(size: int) -> str:
    digest = hashlib.sha256()
    block = b"\0" * MiB
    while size:
        take = min(size, len(block))
        digest.update(block[:take])
        size -= take
    return "sha256:" + digest.hexdigest()


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * MiB), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def make_version(
    root: Path,
    version_id: str,
    *,
    step: int,
    parent_id: str | None,
    source_world: int,
    target_world: int,
    row_elements: int,
    change_index: int | None,
    fill: str,
    zero_digest: str | None = None,
) -> tuple[dict, dict]:
    source_root = root / "sources" / version_id
    source_root.mkdir(parents=True, exist_ok=True)
    row_bytes = row_elements * 4
    shards = []
    for rank in range(source_world):
        path = source_root / f"rank{rank}.bin"
        with path.open("wb") as stream:
            if fill == "random":
                stream.write(random.Random(1000 + rank).randbytes(row_bytes))
            else:
                stream.truncate(row_bytes)
            if change_index is not None and rank == change_index % source_world:
                offset = (change_index * 4096) % max(1, row_bytes)
                stream.seek(offset)
                stream.write(bytes([1 + change_index % 251]))
        shards.append(
            {
                "tensor": "w",
                "rank": rank,
                "uri": path.as_uri(),
                "offset": [rank, 0],
                "size": [1, row_elements],
                "checksum": zero_digest or file_checksum(path),
            }
        )
    spec = {
        "version": "1",
        "checkpoint": {
            "format": "toy",
            "tensors": [
                {
                    "name": "w",
                    "shape": [source_world, row_elements],
                    "dtype": "float32",
                }
            ],
        },
        "source": {
            "mesh": {
                "shape": [source_world],
                "world_size": source_world,
                "placements": ["Shard(0)"],
            },
            "storage": {"uri": source_root.as_uri()},
            "shards": shards,
        },
        "target": {
            "mesh": {
                "shape": [target_world],
                "world_size": target_world,
                "placements": ["Shard(0)"],
            },
            "storage": {"uri": (root / "target").as_uri()},
        },
    }
    return spec, {"version_id": version_id, "step": step, "parent_id": parent_id}


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mb", type=int, default=1024)
    parser.add_argument("--source-world", type=int, default=128)
    parser.add_argument("--target-world", type=int, default=64)
    parser.add_argument("--versions", type=int, default=3)
    parser.add_argument("--chunk-min-kb", type=int, default=256)
    parser.add_argument("--chunk-avg-kb", type=int, default=1024)
    parser.add_argument("--chunk-max-kb", type=int, default=4096)
    parser.add_argument(
        "--chunk-alg",
        choices=("file", "cdc", "fixed", "adaptive"),
        default="file",
        help="file is the fast target-layout path; cdc is for sub-file delta",
    )
    parser.add_argument(
        "--objective",
        choices=("speed", "bytes", "balanced"),
        default="speed",
        help="adaptive strategy objective; ignored for fixed chunk algorithms",
    )
    parser.add_argument("--fill", choices=("zero", "random"), default="random")
    parser.add_argument(
        "--mode",
        choices=("plan", "cdp"),
        default="cdp",
        help="plan only measures transfer planning; cdp runs the full CDP path",
    )
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--skip-cutover", action="store_true")
    parser.add_argument(
        "--assert-p1",
        action="store_true",
        help="fail with exit code 1 when P1 RTO thresholds are not met",
    )
    args = parser.parse_args(argv)
    if args.versions < 1:
        raise SystemExit("versions must be positive")

    root = Path(args.work_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"work directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    versions_root = root / "versions"
    versions_root.mkdir()
    target = VersionTarget(root / "target")
    adaptive = None
    if args.chunk_alg == "adaptive":
        chunking = ChunkingPolicy.file()
        adaptive = AdaptiveChunkingPolicy(args.objective)
    elif args.chunk_alg == "file":
        chunking = ChunkingPolicy.file()
    elif args.chunk_alg == "fixed":
        chunking = ChunkingPolicy.fixed(args.chunk_avg_kb * 1024)
    else:
        chunking = ChunkingPolicy.content_defined(
            min_bytes=args.chunk_min_kb * 1024,
            avg_bytes=args.chunk_avg_kb * 1024,
            max_bytes=args.chunk_max_kb * 1024,
        )

    row_elements = max(4, args.size_mb * MiB // (args.source_world * 4))
    source_bytes = args.source_world * row_elements * 4
    zero_digest = zero_checksum(row_elements * 4) if args.fill == "zero" else None
    rows: list[dict] = []
    started_all = time.perf_counter()
    previous_id: str | None = None
    registry: dict[str, tuple[dict, dict]] = {}
    provider = None
    follower = None
    if args.mode == "cdp":
        provider = DirectIncrementalApplyFileProvider(
            lambda descriptor: registry[descriptor.version_id],
            target=target,
            adaptive=adaptive,
        )
        follower = CheckpointFollower(
            versions_root,
            target,
            chunking=chunking,
            manifest_provider=provider,
        )

    for index in range(args.versions):
        version_id = f"step-{(index + 1) * 1000}"
        spec, marker = make_version(
            root,
            version_id,
            step=(index + 1) * 1000,
            parent_id=previous_id,
            source_world=args.source_world,
            target_world=args.target_world,
            row_elements=row_elements,
            change_index=None if index == 0 else index,
            fill=args.fill,
            zero_digest=zero_digest,
        )
        plan_started = time.perf_counter()
        plan_payload = plan(spec)
        plan_seconds = time.perf_counter() - plan_started

        if args.mode == "plan":
            rows.append(
                {
                    "version_id": version_id,
                    "source_bytes": source_bytes,
                    "target_bytes": plan_payload["total_bytes"],
                    "chunks": len(plan_payload["nodes"]),
                    "plan_seconds": plan_seconds,
                    "sync_seconds": 0.0,
                    "missing_bytes": 0,
                    "delta_ratio": 0.0,
                    "transfer_ratio": 0.0,
                    "shadow_lag": 0,
                    "target_storage_ratio": (
                        plan_payload["total_bytes"] / source_bytes
                        if source_bytes
                        else 0.0
                    ),
                }
            )
            previous_id = version_id
            continue

        registry[version_id] = (spec, plan_payload)
        version_dir = versions_root / version_id
        version_dir.mkdir()
        write_marker(
            version_dir,
            VersionMarker(
                version="1",
                version_id=version_id,
                job_uid="benchmark",
                step=marker["step"],
                parent_id=marker["parent_id"],
                metadata_hash="sha256:" + "a" * 64,
                created_at=f"2026-10-22T00:00:{index:02d}Z",
                complete=True,
            ),
        )
        sync_started = time.perf_counter()
        results = follower.sync_once()
        sync_seconds = time.perf_counter() - sync_started
        manifest = target.manifests.read(version_id)
        staged = [item for item in results if item.version_id == version_id]
        delta = staged[0] if staged else None
        rows.append(
            {
                "version_id": version_id,
                "source_bytes": source_bytes,
                "target_bytes": sum(chunk.size for chunk in manifest.chunks),
                "chunks": len(manifest.chunks),
                "plan_seconds": plan_seconds,
                "sync_seconds": sync_seconds,
                "missing_bytes": delta.missing_bytes if delta else 0,
                "delta_ratio": delta.delta_ratio if delta else 0.0,
                "transfer_ratio": (
                    (delta.missing_bytes / source_bytes) if delta else 0.0
                ),
                "shadow_lag": 0,
                "target_storage_ratio": (
                    sum(chunk.size for chunk in manifest.chunks) / source_bytes
                    if source_bytes
                    else 0.0
                ),
            }
        )
        previous_id = version_id

    cutover = None
    if args.mode == "cdp" and not args.skip_cutover:
        session = CutoverSession(
            versions_root,
            target,
            policy=CutoverPolicy(max_pause_seconds=120, max_lag_versions=10),
            chunking=chunking,
        )
        cutover_started = time.perf_counter()
        result = session.cutover(previous_id or "step-1000")
        cutover = {
            "state": result.state,
            "version_id": result.version_id,
            "paused_seconds": result.paused_seconds,
            "target_ready_seconds": result.target_ready_seconds,
            "wall_seconds": time.perf_counter() - cutover_started,
        }

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "size_mb": args.size_mb,
        "mode": args.mode,
        "chunk_alg": args.chunk_alg,
        "fill": args.fill,
        "source_world": args.source_world,
        "target_world": args.target_world,
        "versions": rows,
        "cutover": cutover,
        "wall_seconds": time.perf_counter() - started_all,
        "max_rss_mb": rss_kb / 1024,
    }
    (root / "cdp-benchmark.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))
    if args.assert_p1:
        failures = check_report(report)
        if failures:
            print(
                json.dumps(
                    {"acceptance": "failed", "failures": failures},
                    indent=2,
                ),
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(json.dumps({"acceptance": "passed"}), file=sys.stderr)
    return report


if __name__ == "__main__":
    main()
