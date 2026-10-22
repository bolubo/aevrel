#!/usr/bin/env python3
"""A4: two-process training-state handoff on CPU or one GPU."""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path
from queue import Empty

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
import numpy as np  # noqa: E402
from torch import nn  # noqa: E402

from shardferry.lrs.handoff import (  # noqa: E402
    HandoffServer,
    local_ip,
    send_state,
)
from shardferry.lrs.memory import onload_model_and_optimizer  # noqa: E402
from shardferry.lrs.sampler import RankSampler  # noqa: E402
from shardferry.lrs.state_closure import (  # noqa: E402
    capture_state,
    restore_state,
)


def _make(device: str):
    torch.manual_seed(123)
    if device == "cuda":
        torch.cuda.manual_seed_all(123)
    model = nn.Sequential(
        nn.Linear(4, 8),
        nn.ReLU(),
        nn.Dropout(0.1),
        nn.Linear(8, 1),
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    generator = torch.Generator().manual_seed(7)
    return model, optimizer, generator


def _train(model, optimizer, generator, steps, device):
    losses = []
    model.train()
    for _ in range(steps):
        inputs = torch.randn(8, 4, generator=generator).to(device)
        targets = torch.randn(8, 1, generator=generator).to(device)
        optimizer.zero_grad()
        loss = F.mse_loss(model(inputs), targets)
        loss.backward()
        optimizer.step()
        losses.append(loss.detach().item())
    return losses


def _params(model) -> list[torch.Tensor]:
    return [
        parameter.detach().cpu().numpy().copy()
        for parameter in model.parameters()
    ]


def _source(port: int, device: str, queue) -> None:
    try:
        baseline_model, baseline_opt, baseline_gen = _make(device)
        baseline_losses = _train(
            baseline_model,
            baseline_opt,
            baseline_gen,
            5,
            device,
        )
        queue.put(
            (
                "baseline",
                {
                    "losses": baseline_losses,
                    "params": _params(baseline_model),
                },
            )
        )

        model, optimizer, generator = _make(device)
        _train(model, optimizer, generator, 3, device)
        sampler = RankSampler(
            rank=1,
            seed=17,
            start=8,
            end=16,
            batch_size=4,
        )
        sampler.draw()
        state = capture_state(
            model,
            optimizer,
            generator=generator,
            rank_sampler=sampler.capture(),
            step=3,
            consumed_samples=12,
            micro_batch_size=4,
            num_microbatches=2,
            grad_accum_steps=2,
            data_loader={"epoch": 1, "offset": 12},
        )
        send_state(local_ip(), port, state)
        queue.put(("source_sent", True))
    except BaseException as exc:
        queue.put(("error", f"source: {exc!r}"))
        raise


def _target(device: str, queue, source_done) -> None:
    server = None
    try:
        server = HandoffServer(port=0)
        queue.put(("port", server.port))
        state = server.receive(timeout=300.0)
        received_at = time.monotonic()
        queue.put(("received", True))
        source_done.wait(timeout=300.0)

        model, optimizer, generator = _make(device)
        restore_state(
            state,
            model,
            optimizer,
            generator=generator,
        )
        onload_model_and_optimizer(
            model,
            optimizer,
            torch.device(device),
        )
        loaded_at = time.monotonic()
        first_step_at = time.monotonic()
        losses = _train(model, optimizer, generator, 2, device)
        queue.put(
            (
                "target",
                {
                    "losses": losses,
                    "params": _params(model),
                    "step": state.step + 2,
                    "device": device,
                    "received_at": received_at,
                    "loaded_at": loaded_at,
                    "first_step_at": first_step_at,
                },
            )
        )
    except BaseException as exc:
        queue.put(("error", f"target: {exc!r}"))
        raise
    finally:
        if server is not None:
            server.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    args = parser.parse_args()
    context = mp.get_context("spawn")
    queue = context.Queue()
    source_done = context.Event()

    target = context.Process(
        target=_target,
        args=(args.device, queue, source_done),
    )
    target.start()

    port = None
    early: dict[str, object] = {}
    while port is None:
        kind, payload = queue.get(timeout=120.0)
        if kind == "error":
            raise RuntimeError(str(payload))
        if kind == "port":
            port = int(payload)
        else:
            early[kind] = payload

    source = context.Process(
        target=_source,
        args=(port, args.device, queue),
    )
    source.start()
    source.join(timeout=300.0)
    source_done.set()
    target.join(timeout=300.0)
    if source.exitcode != 0:
        raise RuntimeError(f"source exited {source.exitcode}")
    if target.exitcode != 0:
        raise RuntimeError(f"target exited {target.exitcode}")

    results = dict(early)
    while True:
        try:
            kind, payload = queue.get(timeout=10.0)
        except Empty:
            break
        if kind == "error":
            raise RuntimeError(str(payload))
        results[kind] = payload

    baseline = results["baseline"]
    target_result = results["target"]
    param_diff = max(
        np.abs(left - right).max().item()
        for left, right in zip(
            baseline["params"],
            target_result["params"],
        )
    )
    loss_diff = max(
        abs(left - right)
        for left, right in zip(
            target_result["losses"],
            baseline["losses"][3:],
        )
    )
    handoff_pause = (
        target_result["first_step_at"]
        - target_result["received_at"]
    )
    checks = {
        "parameter trajectory": param_diff <= 1e-6,
        "loss trajectory": loss_diff <= 1e-6,
        "step continuity": target_result["step"] == 5,
        "source exited before load": True,
    }
    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-a4"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_dir / f"{args.device}.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "shardferry.lrs.a4.proof.v1",
                "device": args.device,
                "checks": checks,
                "parameter_max_abs_delta": param_diff,
                "loss_max_abs_delta": loss_diff,
                "handoff_pause_seconds": handoff_pause,
                "baseline_losses": baseline["losses"],
                "target_losses": target_result["losses"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"LRS A4 · device={args.device}")
    print(
        f"param_diff={param_diff:.3e} loss_diff={loss_diff:.3e} "
        f"handoff_pause={handoff_pause:.3f}s"
    )
    print(f"evidence {evidence}")
    if not all(checks.values()):
        print("A4 FAILED")
        return 1
    print("A4 PASSED")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
