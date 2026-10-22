#!/usr/bin/env python3
"""Single-GPU CUDA smoke for the TP/PP toy math.

This proves the GPU and the TP/PP shard math; it does not replace the
4-rank NCCL validation, which needs four GPUs.
"""

from __future__ import annotations

import torch


def _forward_backward(x, y, w1, w2):
    pre = x @ w1
    act = torch.relu(pre)
    pred = act @ w2
    loss = ((pred - y) ** 2).mean()
    dy = 2.0 * (pred - y) / x.shape[0]
    dw2 = act.T @ dy
    da = dy @ w2.T
    dh = da * (pre > 0).to(dtype=pre.dtype)
    dw1 = x.T @ dh
    return loss, dw1, dw2


def main() -> int:
    if not torch.cuda.is_available():
        print("CUDA is not available")
        return 2
    device = torch.device("cuda")
    dtype = torch.float64
    print(
        f"torch={torch.__version__} cuda={torch.cuda.is_available()} "
        f"device={torch.cuda.get_device_name(0)}"
    )

    torch.manual_seed(7)
    x = torch.randn(64, 4, dtype=dtype)
    w1_true = torch.randn(4, 4, dtype=dtype)
    w2_true = torch.randn(4, 1, dtype=dtype)
    y = torch.relu(x @ w1_true) @ w2_true + 0.01 * torch.randn(
        64,
        1,
        dtype=dtype,
    )
    w1 = 0.1 * torch.randn(4, 4, dtype=dtype)
    w2 = 0.1 * torch.randn(4, 1, dtype=dtype)
    batch = torch.randperm(64)[:8]
    xb = x[batch]
    yb = y[batch]

    loss_cpu, dw1_cpu, dw2_cpu = _forward_backward(
        xb,
        yb,
        w1,
        w2,
    )
    xb_gpu = xb.to(device)
    yb_gpu = yb.to(device)
    w1_gpu = w1.to(device)
    w2_gpu = w2.to(device)
    loss_gpu, dw1_gpu, dw2_gpu = _forward_backward(
        xb_gpu,
        yb_gpu,
        w1_gpu,
        w2_gpu,
    )
    torch.cuda.synchronize()
    full_diffs = {
        "loss": float(abs(loss_cpu.item() - loss_gpu.item())),
        "dw1": float(torch.max(torch.abs(dw1_cpu - dw1_gpu.cpu()))),
        "dw2": float(torch.max(torch.abs(dw2_cpu - dw2_gpu.cpu()))),
    }

    hidden = 2
    pre0 = xb_gpu @ w1_gpu[:, :hidden]
    pre1 = xb_gpu @ w1_gpu[:, hidden:]
    pre = torch.cat([pre0, pre1], dim=1)
    act = torch.relu(pre)
    pred = (
        act[:, :hidden] @ w2_gpu[:hidden, :]
        + act[:, hidden:] @ w2_gpu[hidden:, :]
    )
    dy = 2.0 * (pred - yb_gpu) / xb_gpu.shape[0]
    dw2_0 = act[:, :hidden].T @ dy
    dw2_1 = act[:, hidden:].T @ dy
    da = torch.cat(
        [
            dy @ w2_gpu[:hidden, :].T,
            dy @ w2_gpu[hidden:, :].T,
        ],
        dim=1,
    )
    dh = da * (pre > 0).to(dtype=pre.dtype)
    dw1_0 = xb_gpu.T @ dh[:, :hidden]
    dw1_1 = xb_gpu.T @ dh[:, hidden:]
    torch.cuda.synchronize()
    shard_diffs = {
        "dw1_0": float(
            torch.max(torch.abs(dw1_0.cpu() - dw1_cpu[:, :hidden]))
        ),
        "dw1_1": float(
            torch.max(torch.abs(dw1_1.cpu() - dw1_cpu[:, hidden:]))
        ),
        "dw2_0": float(
            torch.max(torch.abs(dw2_0.cpu() - dw2_cpu[:hidden, :]))
        ),
        "dw2_1": float(
            torch.max(torch.abs(dw2_1.cpu() - dw2_cpu[hidden:, :]))
        ),
    }
    print(f"full_grad_diffs={full_diffs}")
    print(f"tp_pp_shard_diffs={shard_diffs}")
    ok = (
        all(value <= 1e-10 for value in full_diffs.values())
        and all(value <= 1e-10 for value in shard_diffs.values())
    )
    if not ok:
        print("SINGLE GPU SMOKE FAILED")
        return 1
    print("SINGLE GPU SMOKE PASSED")
    print("note: 4-rank TP=2/PP=2 NCCL still needs 4 GPUs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
