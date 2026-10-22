"""Compatibility helpers for torch builds without auto-registered c10d backends."""

from __future__ import annotations

import torch.distributed as dist


def _register(name: str, creator, devices: str) -> None:
    if name.upper() not in dist.Backend._plugins:
        dist.Backend.register_backend(
            name,
            creator,
            extended_api=True,
            devices=devices,
        )


def ensure_gloo_backend() -> None:
    """Register gloo when the installed torch wheel omitted its entry point."""

    def creator(opts, backend_options):
        return dist.ProcessGroupGloo(
            opts.store,
            opts.group_rank,
            opts.group_size,
            opts.timeout,
        )

    _register("gloo", creator, "cpu")


def ensure_nccl_backend() -> None:
    """Register nccl when the installed torch wheel omitted its entry point."""

    def creator(opts, backend_options):
        return dist.ProcessGroupNCCL(
            opts.store,
            opts.group_rank,
            opts.group_size,
            opts.timeout,
        )

    _register("nccl", creator, "cuda")
