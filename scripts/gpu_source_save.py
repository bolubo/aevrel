from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
import torch.nn as nn
from torch.distributed.checkpoint.state_dict import (
    StateDictOptions,
    get_state_dict,
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
    parser.add_argument("--out", required=True)
    parser.add_argument("--fixture", required=False)
    parser.add_argument("--backend", default="nccl")
    parser.add_argument("--dtype", choices=["float32", "bfloat16"], default="float32")
    parser.add_argument("--bulk-dim", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if not torch.cuda.is_available():
        raise SystemExit("gpu_source_save requires CUDA")

    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    dist.init_process_group(backend=args.backend)
    try:
        torch.manual_seed(args.seed)
        dtype = torch.float32 if args.dtype == "float32" else torch.bfloat16
        model = FSDP(
            TinyModel(args.bulk_dim).to(device=device, dtype=dtype),
            device_id=device,
        )
        optimizer = torch.optim.AdamW(model.parameters(), lr=0.01)
        value = torch.arange(16, dtype=dtype, device=device).reshape(4, 4) / 10
        model.train()
        loss = model(value).float().pow(2).mean()
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()

        options = StateDictOptions(full_state_dict=False, cpu_offload=True)
        model_state, optim_state = get_state_dict(
            model,
            optimizer,
            options=options,
        )
        dcp.save(
            state_dict={"model": model_state, "optimizer": optim_state},
            checkpoint_id=args.out,
        )

        model.eval()
        with torch.no_grad():
            output = model(value)
        if dist.get_rank() == 0:
            fixture = (
                Path(args.fixture)
                if args.fixture
                else Path(str(args.out) + ".fixture.pt")
            )
            fixture.parent.mkdir(parents=True, exist_ok=True)
            torch.save(
                {
                    "input": value.detach().cpu(),
                    "output": output.detach().cpu(),
                    "dtype": args.dtype,
                },
                fixture,
            )
            metadata = {
                "dtype": args.dtype,
                "backend": args.backend,
                "world_size": dist.get_world_size(),
                "torch": torch.__version__,
                "cuda": torch.version.cuda,
            }
            Path(str(args.out) + ".json").write_text(
                json.dumps(metadata, indent=2),
                encoding="utf-8",
            )
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
