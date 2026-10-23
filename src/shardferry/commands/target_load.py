#!/usr/bin/env python3
"""Model-independent DCP target load smoke test.

This verifies that a migrated target DCP can be read by the installed
PyTorch DCP runtime. It does not replace a training job's own model load
check; it catches metadata, rank, file and dtype failures before handing the
checkpoint to users.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]


def _rss_bytes() -> int | None:
    if resource is None:
        return None
    try:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, OSError):
        return None
    # Linux reports KiB, macOS reports bytes.
    if sys.platform == "darwin":
        return int(value)
    return int(value) * 1024


def _metadata_summary(metadata_dir: Path) -> tuple[int, int]:
    from shardferry.dcp import read_dcp_metadata

    metadata = read_dcp_metadata(metadata_dir)
    tensors = 0
    byte_count = 0
    for value in metadata.state_dict_metadata.values():
        size = getattr(value, "size", None)
        properties = getattr(value, "properties", None)
        if size is None or properties is None:
            continue
        elements = 1
        for dimension in size:
            elements *= int(dimension)
        byte_count += elements * int(properties.dtype.itemsize)
        tensors += 1
    return tensors, byte_count


def _restore_state(
    *,
    root: str | Path,
    target_rank: int,
    provider_names: list[str],
    require_complete: bool,
) -> dict[str, Any]:
    from shardferry.state import (
        StateBundle,
        builtin_providers,
        restore_state,
        verify_restored,
        verify_state,
    )

    bundle = StateBundle.read(root)
    verify_state(bundle, require_complete=require_complete)
    providers = builtin_providers(provider_names)
    restore_state(
        bundle,
        target_rank=target_rank,
        providers=providers,
        strict=True,
    )
    verify_restored(
        bundle,
        target_rank=target_rank,
        providers=providers,
    )
    return {
        "state_complete": bundle.manifest.complete,
        "state_root": bundle.manifest.root,
        "state_entries": len(bundle.manifest.entries),
        "state_providers": [provider.name for provider in providers],
        "state_target_rank": target_rank,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Load a target DCP directory with the installed PyTorch DCP "
            "runtime and report whether it is readable."
        )
    )
    parser.add_argument(
        "--checkpoint",
        required=True,
        help="Target DCP directory to load.",
    )
    parser.add_argument(
        "--metadata-dir",
        default=None,
        help="Directory containing .metadata (defaults to checkpoint).",
    )
    parser.add_argument(
        "--rank",
        type=int,
        default=None,
        help="Load only this rank's local chunks instead of the full state dict.",
    )
    parser.add_argument(
        "--version-id",
        default="smoke",
        help="Human-readable version/step label used in errors.",
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help="Optional rank-local state bundle to restore after DCP load.",
    )
    parser.add_argument(
        "--state-providers",
        default="python.random,numpy.random,torch.cpu.rng",
        help="Comma-separated state provider names.",
    )
    parser.add_argument(
        "--state-target-rank",
        type=int,
        default=None,
        help="Target rank for state restore (defaults to --rank or 0).",
    )
    parser.add_argument(
        "--require-state-complete",
        action="store_true",
        help="Fail if the state bundle is not closure-complete.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    checkpoint = Path(args.checkpoint).expanduser().resolve()
    metadata_dir = (
        Path(args.metadata_dir).expanduser().resolve()
        if args.metadata_dir
        else checkpoint
    )
    if args.rank is not None and args.rank < 0:
        print("target-load: --rank must be non-negative", file=sys.stderr)
        return 2
    if args.state_target_rank is not None and args.state_target_rank < 0:
        print(
            "target-load: --state-target-rank must be non-negative",
            file=sys.stderr,
        )
        return 2
    if args.require_state_complete and not args.state_root:
        print(
            "target-load: --require-state-complete needs --state-root",
            file=sys.stderr,
        )
        return 2
    if not checkpoint.is_dir():
        print(
            f"target-load: checkpoint directory not found: {checkpoint}",
            file=sys.stderr,
        )
        return 2
    if not (metadata_dir / ".metadata").is_file():
        print(
            f"target-load: DCP metadata not found: {metadata_dir / '.metadata'}",
            file=sys.stderr,
        )
        return 2

    started = time.perf_counter()
    try:
        from shardferry.verification import DcpTargetReady

        tensors, expected_bytes = _metadata_summary(metadata_dir)
        if tensors == 0:
            raise RuntimeError("DCP metadata contains no tensor entries")
        DcpTargetReady(
            checkpoint,
            metadata_dir=metadata_dir,
            rank=args.rank,
        )(args.version_id)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        result = {
            "status": "failed",
            "checkpoint": str(checkpoint),
            "metadata_dir": str(metadata_dir),
            "rank": args.rank,
            "version_id": args.version_id,
            "duration_seconds": round(elapsed, 6),
            "error": str(exc),
            "state_complete": False,
            "state_restored": False,
        }
        if args.json_output:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"target-load failed: {exc}", file=sys.stderr)
        return 2

    state_result: dict[str, Any] | None = None
    if args.state_root:
        state_target_rank = (
            args.state_target_rank
            if args.state_target_rank is not None
            else (args.rank if args.rank is not None else 0)
        )
        provider_names = [
            name.strip()
            for name in args.state_providers.split(",")
            if name.strip()
        ]
        try:
            state_result = _restore_state(
                root=Path(args.state_root).expanduser().resolve(),
                target_rank=state_target_rank,
                provider_names=provider_names,
                require_complete=args.require_state_complete,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            result = {
                "status": "failed",
                "checkpoint": str(checkpoint),
                "metadata_dir": str(metadata_dir),
                "rank": args.rank,
                "version_id": args.version_id,
                "duration_seconds": round(elapsed, 6),
                "error": f"state restore failed: {exc}",
                "state_complete": False,
                "state_restored": False,
                "state_root": str(
                    Path(args.state_root).expanduser().resolve()
                ),
            }
            if args.json_output:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(
                    f"target-load state restore failed: {exc}",
                    file=sys.stderr,
                )
            return 2

    elapsed = time.perf_counter() - started
    result: dict[str, Any] = {
        "status": "ok",
        "checkpoint": str(checkpoint),
        "metadata_dir": str(metadata_dir),
        "mode": "rank-local" if args.rank is not None else "full",
        "rank": args.rank,
        "version_id": args.version_id,
        "tensors": tensors,
        "bytes": expected_bytes,
        "duration_seconds": round(elapsed, 6),
        "peak_rss_bytes": _rss_bytes(),
        "state_complete": bool(
            state_result and state_result["state_complete"]
        ),
        "state_restored": state_result is not None,
        "state": state_result,
    }
    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "target-load ok: "
            f"mode={result['mode']} tensors={tensors} bytes={expected_bytes} "
            f"seconds={elapsed:.3f} state_complete={result['state_complete']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
