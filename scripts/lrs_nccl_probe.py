#!/usr/bin/env python3
"""4-rank NCCL probe for the LRS 4-GPU runbook."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import torch
import torch.distributed as dist

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.lrs.backends import ensure_nccl_backend  # noqa: E402


def main() -> int:
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    torch.cuda.set_device(local_rank)
    ensure_nccl_backend()
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    world = dist.get_world_size()
    device = torch.device("cuda", local_rank)
    if world != 4:
        if rank == 0:
            print(f"expected world_size=4, got {world}")
        dist.destroy_process_group()
        return 2

    tensor = torch.full(
        (1024, 1024),
        float(rank + 1),
        device=device,
    )
    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    expected = (
        sum(range(1, world + 1)) * 1024 * 1024
    )
    diff = float(torch.max(torch.abs(tensor - expected)))

    left = torch.randn(256, 256, device=device)
    right = torch.randn(256, 256, device=device)
    product = left @ right
    torch.cuda.synchronize()
    if rank == 0:
        print(
            f"world={world} device={torch.cuda.get_device_name(0)} "
            f"all_reduce_max_diff={diff:.3e} "
            f"matmul_sum={float(product.sum()):.6f}"
        )
    dist.destroy_process_group()
    if diff > 1e-6:
        if rank == 0:
            print("NCCL PROBE FAILED")
        return 1
    if rank == 0:
        print("NCCL PROBE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
