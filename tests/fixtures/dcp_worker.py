from __future__ import annotations

import argparse
from pathlib import Path

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor import DTensor, Shard


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--shape", default="8,4")
    parser.add_argument("--multi", action="store_true")
    args = parser.parse_args()

    dist.init_process_group("gloo")
    try:
        rank = dist.get_rank()
        world = dist.get_world_size()
        shape = tuple(int(value) for value in args.shape.split(","))
        total = 1
        for dim in shape:
            total *= dim
        mesh = init_device_mesh("cpu", (world,))
        values = {
            "w": torch.arange(total, dtype=torch.float32).reshape(shape),
        }
        if args.multi:
            values["b"] = torch.arange(shape[0], dtype=torch.float32)
            values["optimizer.state.exp_avg"] = (
                torch.arange(total, dtype=torch.float32).reshape(shape) + 1000
            )
        state = {}
        for name, global_tensor in values.items():
            local = torch.tensor_split(global_tensor, world, dim=0)[rank].contiguous()
            state[name] = DTensor.from_local(
                local,
                mesh,
                [Shard(0)],
                run_check=False,
                shape=torch.Size(global_tensor.shape),
                stride=global_tensor.stride(),
            )
        dcp.save(
            state_dict=state,
            checkpoint_id=Path(args.out),
        )
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
