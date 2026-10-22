from __future__ import annotations

import argparse

import torch
import torch.distributed as dist
import torch.distributed.checkpoint as dcp
from torch.distributed.device_mesh import init_device_mesh
from torch.distributed.tensor import DTensor, Shard


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True)
    parser.add_argument("--shape", default="8,4")
    parser.add_argument("--multi", action="store_true")
    parser.add_argument("--model-output", action="store_true")
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
        expected = {
            "w": torch.arange(total, dtype=torch.float32).reshape(shape),
        }
        if args.multi:
            expected["b"] = torch.arange(shape[0], dtype=torch.float32)
            expected["optimizer.state.exp_avg"] = (
                torch.arange(total, dtype=torch.float32).reshape(shape) + 1000
            )
        state = {}
        expected_local = {}
        for name, global_tensor in expected.items():
            local = torch.empty(
                torch.tensor_split(global_tensor, world, dim=0)[rank].shape,
                dtype=global_tensor.dtype,
            )
            expected_local[name] = torch.tensor_split(global_tensor, world, dim=0)[rank]
            state[name] = DTensor.from_local(
                local,
                mesh,
                [Shard(0)],
                run_check=False,
                shape=torch.Size(global_tensor.shape),
                stride=global_tensor.stride(),
            )
        dcp.load(state, checkpoint_id=args.ckpt)
        for name, value in state.items():
            assert torch.equal(value.to_local(), expected_local[name]), (rank, name)
        if args.model_output:
            x = torch.tensor(
                [[0.5, -1.0, 2.0, 0.25], [1.5, 0.0, -0.5, 3.0]],
                dtype=torch.float32,
            )
            actual = x @ state["w"].full_tensor().T + state["b"].full_tensor()
            wanted = x @ expected["w"].T + expected["b"]
            assert torch.equal(actual, wanted), rank
        print(
            f"rank={rank} loaded="
            f"{ {name: tuple(value.to_local().shape) for name, value in state.items()} } "
            f"ok=True"
        )
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
