#!/usr/bin/env python3
"""Probe torchcomms dynamic NCCL reconfigure for the LRS 4-GPU gate."""

from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import torch
import torch.distributed as dist
import torchcomms

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aevrel.lrs.backends import ensure_gloo_backend  # noqa: E402


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
    interface = _default_interface()
    if interface and "NCCL_SOCKET_IFNAME" not in os.environ:
        os.environ["NCCL_SOCKET_IFNAME"] = interface
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    device = torch.device("cuda", local_rank)
    torch.cuda.set_device(device)

    ensure_gloo_backend()
    dist.init_process_group(
        "gloo",
        timeout=timedelta(seconds=120),
    )

    comm = torchcomms.new_comm(
        "nccl",
        device,
        "lrs-torchcomms-probe",
        enable_reconfigure=True,
        timeout=timedelta(seconds=120),
    )
    handle = comm.get_init_handle()
    handles: list[str | None] = [None] * world
    dist.all_gather_object(handles, handle)
    if any(item is None for item in handles):
        print("failed to collect init handles")
        return 2

    started = time.perf_counter()
    work = comm.reconfigure(
        uuid=1,
        init_handles=[str(item) for item in handles],
        timeout=timedelta(seconds=120),
    )
    work.wait_blocking()
    reconfigure_seconds = time.perf_counter() - started

    tensor = torch.full(
        (1024,),
        float(rank + 1),
        dtype=torch.float32,
        device=device,
    )
    comm.all_reduce(
        tensor,
        op=torchcomms.ReduceOp.SUM,
        async_op=False,
    )
    torch.cuda.synchronize()
    expected = float(sum(range(1, world + 1)))
    value = float(tensor[0].item())
    diff = abs(value - expected)

    if rank == 0:
        print(
            f"world={world} reconfigure_seconds={reconfigure_seconds:.3f} "
            f"all_reduce_value={value:.1f} expected={expected:.1f} "
            f"diff={diff:.3e}"
        )
        if diff <= 1e-6:
            print("TORCHCOMMS RECONFIGURE PROBE PASSED")
        else:
            print("TORCHCOMMS RECONFIGURE PROBE FAILED")

    comm.finalize()
    dist.destroy_process_group()
    return 0 if diff <= 1e-6 else 1


if __name__ == "__main__":
    raise SystemExit(main())
