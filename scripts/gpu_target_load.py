from __future__ import annotations

import argparse
import os

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
import torch.nn as nn
from torch.distributed.checkpoint.state_dict import (
    StateDictOptions,
    get_state_dict,
    set_state_dict,
)
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP


class TinyModel(nn.Module):
    def __init__(self, bulk_dim: int = 0) -> None:
        super().__init__()
        self.fc = nn.Linear(4, 8)
        self.bulk = nn.Linear(bulk_dim, bulk_dim) if bulk_dim else None

    def forward(self, value: torch.Tensor) -> torch.Tensor:
        output = self.fc(value)
        if self.bulk is not None:
            bulk_input = output.new_zeros(
                (output.shape[0], self.bulk.in_features)
            )
            output = output + self.bulk(bulk_input)[:, : output.shape[1]]
        return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--backend", default="nccl")
    parser.add_argument("--dtype", choices=["float32", "bfloat16"], default="float32")
    parser.add_argument("--bulk-dim", type=int, default=0)
    parser.add_argument("--atol", type=float, default=0.0)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("gpu_target_load requires CUDA")

    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    dist.init_process_group(backend=args.backend)
    try:
        torch.manual_seed(0)
        dtype = torch.float32 if args.dtype == "float32" else torch.bfloat16
        model = FSDP(
            TinyModel(args.bulk_dim).to(device=device, dtype=dtype),
            device_id=device,
        )
        optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
        options = StateDictOptions(full_state_dict=False, cpu_offload=True)
        model_state, optim_state = get_state_dict(
            model,
            optimizer,
            options=options,
        )
        dcp.load(
            {"model": model_state, "optimizer": optim_state},
            checkpoint_id=args.ckpt,
        )
        set_state_dict(
            model,
            optimizer,
            model_state_dict=model_state,
            optim_state_dict=optim_state,
            options=options,
        )

        fixture = torch.load(args.fixture, map_location="cpu", weights_only=False)
        value = fixture["input"].to(device=device, dtype=dtype)
        expected = fixture["output"].to(device=device, dtype=dtype)
        model.eval()
        with torch.no_grad():
            actual = model(value)
        if args.atol == 0.0:
            matched = torch.equal(actual, expected)
        else:
            matched = torch.allclose(actual, expected, atol=args.atol, rtol=0)
        if not matched:
            max_abs_diff = (actual.float() - expected.float()).abs().max()
            print(
                f"rank={dist.get_rank()} max_abs_diff={max_abs_diff.item():.8g} "
                f"actual={actual.flatten()[:4].tolist()} "
                f"expected={expected.flatten()[:4].tolist()}"
            )
            raise RuntimeError(
                f"model output mismatch on rank {dist.get_rank()}"
            )
        print(
            f"rank={dist.get_rank()} world={dist.get_world_size()} "
            f"output_ok=True optimizer_state_loaded=True"
        )
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
