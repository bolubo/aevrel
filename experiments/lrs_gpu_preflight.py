#!/usr/bin/env python3
"""Single-process GPU/NCCL preflight for the LRS 4-GPU window."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import torch
import torch.distributed as dist

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aevrel.lrs.backends import ensure_nccl_backend  # noqa: E402


def _default_interface() -> str | None:
    try:
        with open("/proc/net/route", encoding="ascii") as route_file:
            next(route_file)
            for line in route_file:
                fields = line.split()
                if fields[1] == "00000000" and int(fields[3], 16) & 0x2:
                    return fields[0]
    except (OSError, ValueError, StopIteration):
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--require-torchcomms",
        action="store_true",
        help="fail if torchcomms is not importable",
    )
    args = parser.parse_args()

    if not torch.cuda.is_available():
        print("CUDA is not available")
        return 2

    interface = _default_interface()
    if interface and "NCCL_SOCKET_IFNAME" not in os.environ:
        os.environ["NCCL_SOCKET_IFNAME"] = interface

    print(
        f"torch={torch.__version__} cuda={torch.version.cuda} "
        f"devices={torch.cuda.device_count()}"
    )
    print(
        f"nccl={torch.cuda.nccl.version()} "
        f"socket_ifname={os.environ.get('NCCL_SOCKET_IFNAME', '<auto>')}"
    )

    torch.cuda.set_device(0)
    ensure_nccl_backend()
    with tempfile.TemporaryDirectory(prefix="lrs-preflight-") as temp:
        store = dist.FileStore(str(Path(temp) / "store"), 1)
        dist.init_process_group(
            "nccl",
            store=store,
            rank=0,
            world_size=1,
        )
        tensor = torch.ones(1024, device="cuda")
        dist.all_reduce(tensor)
        torch.cuda.synchronize()
        value = float(tensor[0].item())
        dist.destroy_process_group()
    print(f"nccl_world1_all_reduce={value:.1f}")
    if value != 1.0:
        print("GPU PREFLIGHT FAILED")
        return 1

    spec = importlib.util.find_spec("torchcomms")
    if spec is None:
        print("torchcomms=missing")
        if args.require_torchcomms:
            print("GPU PREFLIGHT FAILED: torchcomms is required")
            return 3
    else:
        try:
            import torchcomms

            try:
                package_version = version("torchcomms")
            except PackageNotFoundError:
                package_version = getattr(
                    torchcomms,
                    "__version__",
                    "unknown",
                )
            print(
                f"torchcomms={package_version} "
                f"path={torchcomms.__file__}"
            )
        except Exception as exc:
            print(f"torchcomms_import_error={exc!r}")
            if args.require_torchcomms:
                print("GPU PREFLIGHT FAILED: torchcomms import failed")
                return 3

    print("GPU PREFLIGHT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
