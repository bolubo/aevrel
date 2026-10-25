from __future__ import annotations

import argparse
import hashlib
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
    set_state_dict,
)
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP

from aevrel.state import build_manifest
from aevrel.verification import ResumeStateEvidence, write_target_ready_report


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


def _state_digest(state: dict[str, object]) -> str:
    """Digest of the state dict a target actually loaded."""
    hasher = hashlib.sha256()
    for key in sorted(state):
        value = state[key]
        hasher.update(key.encode("utf-8"))
        if isinstance(value, torch.Tensor):
            flattened = value.detach().to("cpu").contiguous().reshape(-1)
            hasher.update(flattened.view(torch.uint8).numpy().tobytes())
        else:
            hasher.update(repr(value).encode("utf-8"))
    return "sha256:" + hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--migration-uid", required=True)
    parser.add_argument("--steps", type=int, default=10)
    parser.add_argument("--bulk-dim", type=int, default=0)
    parser.add_argument("--dtype", choices=["float32", "bfloat16"], default="float32")
    parser.add_argument("--atol", type=float, default=0.0)
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()
    if args.steps < 1:
        raise SystemExit("--steps must be positive")
    if not torch.cuda.is_available():
        raise SystemExit("gpu_single_target_ready requires CUDA")

    os.environ.setdefault("RANK", "0")
    os.environ.setdefault("WORLD_SIZE", "1")
    os.environ.setdefault("LOCAL_RANK", "0")
    torch.cuda.set_device(0)
    device = torch.device("cuda", 0)
    dist.init_process_group(backend="nccl", rank=0, world_size=1)
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

        fixture = torch.load(
            args.fixture,
            map_location="cpu",
            weights_only=False,
        )
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
            raise RuntimeError("target model output mismatch")

        model.train()
        for _ in range(args.steps):
            optimizer.zero_grad(set_to_none=True)
            loss = model(value).float().pow(2).mean()
            loss.backward()
            optimizer.step()
        torch.cuda.synchronize()

        metadata_path = Path(args.ckpt).expanduser() / ".metadata"
        if not metadata_path.is_file():
            raise SystemExit(f"DCP metadata not found: {metadata_path}")
        manifest = build_manifest(
            checkpoint_uid=args.migration_uid,
            version=args.version,
            metadata_digest=(
                "sha256:"
                + hashlib.sha256(metadata_path.read_bytes()).hexdigest()
            ),
            world_size=1,
            present={"model": "dcp", "optimizer": "dcp"},
            digests={
                "model": _state_digest(model_state),
                "optimizer": _state_digest(optim_state),
            },
            placements=("node/rank-0",),
        )
        payload = write_target_ready_report(
            model_loaded=True,
            optimizer_loaded=True,
            steps_ran=args.steps,
            migration_uid=args.migration_uid,
            version=args.version,
            rank=0,
            resume_state=ResumeStateEvidence.from_manifest(manifest),
            path=args.report_path,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
