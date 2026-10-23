from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .cdp import CDPError, VersionTarget
from .verification import DcpTargetReady, TargetReadyError


class TargetProbeError(RuntimeError):
    """Raised when the promoted target checkpoint is not ready."""


def check_target(
    target_root: str | Path,
    version_id: str,
) -> dict[str, Any]:
    target = VersionTarget(target_root)
    current = target.current()
    if current is None:
        raise TargetProbeError("target has no current version")
    if current.version_id != version_id:
        raise TargetProbeError(
            f"target current is {current.version_id}, expected {version_id}"
        )
    manifest = target.manifests.read(version_id)
    if manifest.root != current.root:
        raise TargetProbeError(
            f"target current root does not match manifest: "
            f"{current.root} != {manifest.root}"
        )
    shadow = target.shadow / quote(version_id, safe="")
    if not shadow.is_dir():
        raise TargetProbeError(f"target shadow is missing: {shadow}")
    missing_chunks = [
        chunk.digest
        for chunk in manifest.chunks
        if not target.chunks.has(chunk.digest)
    ]
    if missing_chunks:
        raise TargetProbeError(
            f"target CAS is missing {len(missing_chunks)} chunk(s)"
        )
    missing_files = [
        chunk.file
        for chunk in manifest.chunks
        if not (shadow / chunk.file).is_file()
    ]
    if missing_files:
        raise TargetProbeError(
            f"target shadow is missing {len(missing_files)} file(s)"
        )
    return {
        "version_id": version_id,
        "manifest_root": manifest.root,
        "chunks": len(manifest.chunks),
        "shadow": str(shadow),
    }


def wait_for_target(
    target_root: str | Path,
    version_id: str,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if poll_interval < 0:
        raise ValueError("poll_interval must be non-negative")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            return check_target(target_root, version_id)
        except (CDPError, TargetProbeError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise TargetProbeError(
                f"target did not become ready within {timeout:.3f}s: "
                f"{last_error}"
            ) from last_error
        time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shardferry.target_probe",
        description="Check that a promoted target checkpoint is ready.",
    )
    parser.add_argument("--target-root", required=False)
    parser.add_argument("--dcp-checkpoint", required=False)
    parser.add_argument("--dcp-metadata", required=False)
    parser.add_argument("--dcp-rank", type=int, required=False)
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.target_root and not args.dcp_checkpoint:
        parser.error("one of --target-root or --dcp-checkpoint is required")
    if args.dcp_rank is not None and not args.dcp_checkpoint:
        parser.error("--dcp-rank requires --dcp-checkpoint")
    try:
        if args.dcp_checkpoint:
            DcpTargetReady(
                args.dcp_checkpoint,
                metadata_dir=args.dcp_metadata or None,
                rank=args.dcp_rank,
            )(args.version_id)
            result = {
                "version_id": args.version_id,
                "mode": (
                    "dcp-rank-load"
                    if args.dcp_rank is not None
                    else "dcp-load"
                ),
                "checkpoint": str(Path(args.dcp_checkpoint).resolve()),
                "rank": args.dcp_rank,
            }
        else:
            result = wait_for_target(
                args.target_root,
                args.version_id,
                timeout=args.timeout,
                poll_interval=args.poll_interval,
            )
    except (TargetProbeError, TargetReadyError, CDPError, OSError, ValueError) as exc:
        print(f"shardferry target-probe: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        if result.get("mode") in {"dcp-load", "dcp-rank-load"}:
            suffix = (
                f" rank {result['rank']}"
                if result.get("rank") is not None
                else ""
            )
            print(
                f"target ready: {result['version_id']} "
                f"(DCP load{suffix})"
            )
        else:
            print(
                f"target ready: {result['version_id']} "
                f"({result['chunks']} chunks)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
