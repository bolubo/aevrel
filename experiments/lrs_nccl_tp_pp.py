#!/usr/bin/env python3
"""4-rank NCCL TP=2/PP=2 baseline for the LRS 4-GPU runbook."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import torch
import torch.distributed as dist

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aevrel.lrs.backends import ensure_nccl_backend  # noqa: E402


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

    pp = rank // 2
    tp = rank % 2
    tp_groups = [
        dist.new_group(ranks=[0, 1]),
        dist.new_group(ranks=[2, 3]),
    ]
    tp_group = tp_groups[pp]
    hidden = 2
    torch.manual_seed(7)
    x = torch.randn(64, 4, dtype=torch.float64)
    w1_true = torch.randn(4, 4, dtype=torch.float64)
    w2_true = torch.randn(4, 1, dtype=torch.float64)
    y = torch.relu(x @ w1_true) @ w2_true + 0.01 * torch.randn(
        64,
        1,
        dtype=torch.float64,
    )
    w1 = 0.1 * torch.randn(4, 4, dtype=torch.float64)
    w2 = 0.1 * torch.randn(4, 1, dtype=torch.float64)
    batch = torch.randperm(64)[:8]
    xb_cpu = x[batch]
    yb_cpu = y[batch]
    xb = xb_cpu.to(device)
    yb = yb_cpu.to(device)

    # Full-model baseline on CPU.
    pre_full = xb_cpu @ w1
    act_full = torch.relu(pre_full)
    pred_full = act_full @ w2
    loss_full = ((pred_full - yb_cpu) ** 2).mean()
    dy_full = 2.0 * (pred_full - yb_cpu) / xb_cpu.shape[0]
    dw2_full = act_full.T @ dy_full
    da_full = dy_full @ w2.T
    dh_full = da_full * (pre_full > 0)
    dw1_full = xb_cpu.T @ dh_full

    if pp == 0:
        local_w = w1[
            :,
            tp * hidden : (tp + 1) * hidden,
        ].to(device)
    else:
        local_w = w2[
            tp * hidden : (tp + 1) * hidden,
            :,
        ].to(device)

    if pp == 0:
        pre = xb @ local_w
        gathered = [torch.empty_like(pre) for _ in range(2)]
        tp_group.all_gather(gathered, pre)
        pre_act = torch.cat(gathered, dim=1)
        act = torch.relu(pre_act)
        dist.send(act, dst=2 + tp)
        da = torch.empty_like(act)
        dist.recv(da, src=2 + tp)
        dh = da * (pre_act > 0)
        dw = xb.T @ dh[:, tp * hidden : (tp + 1) * hidden]
        expected = dw1_full[:, tp * hidden : (tp + 1) * hidden]
        loss_diff = 0.0
    else:
        act = torch.empty(
            (xb.shape[0], 4),
            dtype=xb.dtype,
            device=device,
        )
        dist.recv(act, src=tp)
        slice_act = act[:, tp * hidden : (tp + 1) * hidden]
        pred = slice_act @ local_w
        tp_group.all_reduce(pred, op=dist.ReduceOp.SUM)
        loss = ((pred - yb) ** 2).mean()
        dy = 2.0 * (pred - yb) / xb.shape[0]
        dw = slice_act.T @ dy
        da_part = dy @ local_w.T
        gathered = [torch.empty_like(da_part) for _ in range(2)]
        tp_group.all_gather(gathered, da_part)
        da_full_gpu = torch.cat(gathered, dim=1)
        dist.send(da_full_gpu, dst=tp)
        expected = dw2_full[tp * hidden : (tp + 1) * hidden, :]
        loss_diff = float(abs(loss.item() - loss_full.item()))

    torch.cuda.synchronize()
    diff = float(torch.max(torch.abs(dw.cpu() - expected)))
    diff_t = torch.tensor([diff, loss_diff], device=device)
    dist.all_reduce(diff_t, op=dist.ReduceOp.MAX)
    if rank == 0:
        print(
            f"world={world} tp={tp} pp={pp} "
            f"max_grad_diff={float(diff_t[0]):.3e} "
            f"max_loss_diff={float(diff_t[1]):.3e}"
        )
        if float(diff_t[0]) <= 1e-8 and float(diff_t[1]) <= 1e-12:
            print("NCCL TP/PP BASELINE PASSED")
        else:
            print("NCCL TP/PP BASELINE FAILED")
    ok = float(diff_t[0]) <= 1e-8 and float(diff_t[1]) <= 1e-12
    dist.destroy_process_group()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
