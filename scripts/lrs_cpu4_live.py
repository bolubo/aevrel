#!/usr/bin/env python3
"""CPU 4-process gloo training with a 4 -> 3 -> 4 live rank split."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import tempfile
from pathlib import Path
from queue import Empty

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.distributed as dist  # noqa: E402
from torch import nn  # noqa: E402

from aevrel.lrs.handoff import (  # noqa: E402
    HandoffServer,
    local_ip,
    send_state,
)
from aevrel.lrs.backends import ensure_gloo_backend  # noqa: E402
from aevrel.lrs.sampler import RankSampler  # noqa: E402
from aevrel.lrs.state_closure import (  # noqa: E402
    capture_state,
    restore_state,
)


WORLD = 4
S = int(os.environ.get("LRS_CPU4_S", "5"))
CUTOVER = int(os.environ.get("LRS_CPU4_CUTOVER", "10"))
STEPS = int(os.environ.get("LRS_CPU4_STEPS", "20"))
BATCH_PER_RANK = int(os.environ.get("LRS_CPU4_BATCH_PER_RANK", "4"))
PER_RANK_DATA = int(os.environ.get("LRS_CPU4_PER_RANK_DATA", "32"))
GLOBAL_BATCH = WORLD * BATCH_PER_RANK
PARAM_DIM = 8
DATA_SIZE = WORLD * PER_RANK_DATA

if not (0 < S < CUTOVER < STEPS):
    raise ValueError(
        "CPU4 requires 0 < LRS_CPU4_S < LRS_CPU4_CUTOVER < LRS_CPU4_STEPS"
    )
if BATCH_PER_RANK < 1 or PER_RANK_DATA < BATCH_PER_RANK:
    raise ValueError("invalid CPU4 batch or per-rank data size")


def _init(path: str, world: int, rank: int) -> None:
    if "GLOO_SOCKET_IFNAME" not in os.environ:
        try:
            with open("/proc/net/route", encoding="ascii") as route_file:
                next(route_file)
                for line in route_file:
                    fields = line.split()
                    if fields[1] == "00000000" and int(fields[3], 16) & 0x2:
                        os.environ["GLOO_SOCKET_IFNAME"] = fields[0]
                        break
        except (OSError, ValueError, StopIteration):
            pass
    ensure_gloo_backend()
    store = dist.FileStore(path, world)
    dist.init_process_group(
        "gloo",
        store=store,
        rank=rank,
        world_size=world,
    )
    dist.barrier()


def _dataset():
    generator = torch.Generator().manual_seed(999)
    x = torch.randn(DATA_SIZE, PARAM_DIM, generator=generator).double()
    y = torch.randn(DATA_SIZE, 1, generator=generator).double()
    return x, y


def _make():
    torch.manual_seed(123)
    model = nn.Linear(PARAM_DIM, 1).double()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.01)
    return model, optimizer


def _sampler(rank: int) -> RankSampler:
    return RankSampler(
        rank=rank,
        seed=1000 + rank,
        start=rank * PER_RANK_DATA,
        end=(rank + 1) * PER_RANK_DATA,
        batch_size=BATCH_PER_RANK,
    )


def _params(model) -> list[np.ndarray]:
    return [
        parameter.detach().cpu().numpy().copy()
        for parameter in model.parameters()
    ]


def _train(
    model,
    optimizer,
    x,
    y,
    sampler: RankSampler,
    rank: int,
    rank1_sampler: RankSampler | None,
    start_step: int,
    end_step: int,
    redistribute: bool,
) -> list[float]:
    losses = []
    for _ in range(start_step, end_step):
        own = list(sampler.draw())
        extra: list[int] = []
        if redistribute and rank1_sampler is not None:
            missing = list(rank1_sampler.draw())
            if rank == 0:
                extra = missing[:2]
            elif rank == 2:
                extra = missing[2:3]
            elif rank == 3:
                extra = missing[3:4]
        indices = own + extra
        xb = x[indices]
        yb = y[indices]
        optimizer.zero_grad()
        squared = ((model(xb) - yb) ** 2).sum()
        (squared / GLOBAL_BATCH).backward()
        for parameter in model.parameters():
            if parameter.grad is not None:
                dist.all_reduce(parameter.grad, op=dist.ReduceOp.SUM)
        optimizer.step()
        loss_tensor = torch.tensor(
            [float(squared.detach())],
            dtype=torch.float64,
        )
        dist.all_reduce(loss_tensor, op=dist.ReduceOp.SUM)
        losses.append(float(loss_tensor.item() / GLOBAL_BATCH))
    return losses


def _baseline_rank(rank: int, path: str, queue) -> None:
    try:
        _init(path, WORLD, rank)
        x, y = _dataset()
        model, optimizer = _make()
        sampler = _sampler(rank)
        losses = _train(
            model,
            optimizer,
            x,
            y,
            sampler,
            rank,
            None,
            0,
            STEPS,
            False,
        )
        queue.put(
            (
                "baseline",
                rank,
                {
                    "theta": _params(model),
                    "loss": losses[-1],
                    "sampler": sampler.capture(),
                },
            )
        )
        dist.destroy_process_group()
    except BaseException as exc:
        queue.put(("error", f"baseline rank {rank}: {exc!r}"))
        raise


def _live_rank(
    rank: int,
    paths: tuple[str, str, str],
    target_port: int,
    phase1,
    phase2,
    queue,
) -> None:
    try:
        x, y = _dataset()
        model, optimizer = _make()
        sampler = _sampler(rank)

        _init(paths[0], WORLD, rank)
        losses0 = _train(
            model,
            optimizer,
            x,
            y,
            sampler,
            rank,
            None,
            0,
            S,
            False,
        )
        if rank == 1:
            queue.put(("rank1_done", sampler.capture()))
            dist.destroy_process_group()
            return

        phase1.wait(timeout=120.0)
        dist.destroy_process_group()

        group_rank = {0: 0, 2: 1, 3: 2}[rank]
        _init(paths[1], 3, group_rank)
        rank1_sampler = _sampler(1)
        for _ in range(S):
            rank1_sampler.draw()
        losses1 = _train(
            model,
            optimizer,
            x,
            y,
            sampler,
            rank,
            rank1_sampler,
            S,
            CUTOVER,
            True,
        )
        if rank == 0:
            state = capture_state(
                model,
                optimizer,
                step=CUTOVER,
                rank_sampler=rank1_sampler.capture(),
                consumed_samples=CUTOVER * GLOBAL_BATCH,
            )
            send_state(local_ip(), target_port, state)
            queue.put(("state_sent", True))

        phase2.wait(timeout=120.0)
        dist.destroy_process_group()

        _init(paths[2], WORLD, rank)
        losses2 = _train(
            model,
            optimizer,
            x,
            y,
            sampler,
            rank,
            None,
            CUTOVER,
            STEPS,
            False,
        )
        queue.put(
            (
                "live_rank",
                rank,
                {
                    "theta": _params(model),
                    "loss": losses2[-1],
                    "sampler": sampler.capture(),
                },
            )
        )
        dist.destroy_process_group()
    except BaseException as exc:
        queue.put(("error", f"live rank {rank}: {exc!r}"))
        raise


def _target(
    path: str,
    phase2,
    queue,
) -> None:
    server = None
    try:
        server = HandoffServer(port=0)
        queue.put(("target_port", server.port))
        state = server.receive(timeout=300.0)
        queue.put(("target_received", True))
        phase2.wait(timeout=120.0)
        _init(path, WORLD, 1)
        x, y = _dataset()
        model, optimizer = _make()
        sampler = _sampler(1)
        restore_state(
            state,
            model,
            optimizer,
            rank_sampler=sampler,
        )
        losses = _train(
            model,
            optimizer,
            x,
            y,
            sampler,
            1,
            None,
            CUTOVER,
            STEPS,
            False,
        )
        queue.put(
            (
                "live_target",
                {
                    "theta": _params(model),
                    "loss": losses[-1],
                    "sampler": sampler.capture(),
                },
            )
        )
        dist.destroy_process_group()
    except BaseException as exc:
        queue.put(("error", f"target: {exc!r}"))
        raise
    finally:
        if server is not None:
            server.close()


def _wait_for(queue, wanted: str):
    while True:
        kind, payload = queue.get(timeout=300.0)
        if kind == "error":
            raise RuntimeError(str(payload))
        if kind == wanted:
            return payload


def main() -> int:
    context = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="lrs-cpu4-") as temp:
        base = Path(temp)
        baseline_queue = context.Queue()
        baseline = [
            context.Process(
                target=_baseline_rank,
                args=(rank, str(base / "baseline"), baseline_queue),
            )
            for rank in range(WORLD)
        ]
        for process in baseline:
            process.start()
        for process in baseline:
            process.join(timeout=300.0)
        baseline_results = {}
        while True:
            try:
                item = baseline_queue.get(timeout=5.0)
            except Empty:
                break
            kind = item[0]
            if kind == "error":
                raise RuntimeError(str(item[1]))
            _, rank, payload = item
            baseline_results[rank] = payload
        if len(baseline_results) != WORLD:
            raise RuntimeError("baseline did not produce all ranks")

        live_queue = context.Queue()
        phase1 = context.Event()
        phase2 = context.Event()
        paths = (
            str(base / "gen0"),
            str(base / "gen1"),
            str(base / "gen2"),
        )
        target = context.Process(
            target=_target,
            args=(paths[2], phase2, live_queue),
        )
        target.start()
        target_port = _wait_for(live_queue, "target_port")
        live = [
            context.Process(
                target=_live_rank,
                args=(
                    rank,
                    paths,
                    target_port,
                    phase1,
                    phase2,
                    live_queue,
                ),
            )
            for rank in range(WORLD)
        ]
        for process in live:
            process.start()
        _wait_for(live_queue, "rank1_done")
        phase1.set()
        _wait_for(live_queue, "target_received")
        phase2.set()
        for process in live:
            process.join(timeout=300.0)
        target.join(timeout=300.0)
        if any(process.exitcode != 0 for process in live):
            raise RuntimeError("a live rank failed")
        if target.exitcode != 0:
            raise RuntimeError("target failed")

        live_results = {}
        target_result = None
        while True:
            try:
                item = live_queue.get(timeout=10.0)
            except Empty:
                break
            kind = item[0]
            if kind == "error":
                raise RuntimeError(str(item[1]))
            if kind == "live_rank":
                live_results[item[1]] = item[2]
            elif kind == "live_target":
                target_result = item[1]
        live_ranks = {0, 2, 3}
        if set(live_results) != live_ranks or target_result is None:
            raise RuntimeError(
                "live split did not produce all results: "
                f"ranks={sorted(live_results)} "
                f"target={target_result is not None} "
                f"rank_exit={[process.exitcode for process in live]} "
                f"target_exit={target.exitcode}"
            )

        parameter_diff = max(
            float(np.max(np.abs(left - right)))
            for rank in live_ranks
            for left, right in zip(
                baseline_results[rank]["theta"],
                live_results[rank]["theta"],
            )
        )
        target_diff = max(
            float(np.max(np.abs(left - right)))
            for left, right in zip(
                baseline_results[1]["theta"],
                target_result["theta"],
            )
        )
        loss_diff = max(
            max(
                abs(
                    baseline_results[rank]["loss"]
                    - live_results[rank]["loss"]
                )
                for rank in live_ranks
            ),
            abs(
                baseline_results[1]["loss"]
                - target_result["loss"]
            ),
        )
        sampler_match = (
            baseline_results[1]["sampler"]
            == target_result["sampler"]
        )
        checks = {
            "4->3->4 parameter trajectory": parameter_diff <= 1e-6,
            "target parameter trajectory": target_diff <= 1e-6,
            "loss trajectory": loss_diff <= 1e-6,
            "rank1 sampler": sampler_match,
            "all ranks returned": (
                set(live_results) == live_ranks
                and target_result is not None
            ),
        }
        evidence_dir = Path(
            os.environ.get(
                "LRS_CPU4_EVIDENCE",
                str(ROOT / "evidence" / "lrs" / "2026-10-22-cpu4"),
            )
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = evidence_dir / "live-split.json"
        evidence.write_text(
            json.dumps(
                {
                    "schema": "aevrel.lrs.cpu4.live.proof.v1",
                    "checks": checks,
                    "parameter_max_abs_delta": parameter_diff,
                    "target_max_abs_delta": target_diff,
                    "loss_max_abs_delta": loss_diff,
                    "baseline_loss": baseline_results[0]["loss"],
                    "live_loss": live_results[0]["loss"],
                    "topology": "4 -> 3 -> 4",
                    "cutover_step": CUTOVER,
                    "steps": STEPS,
                    "rank1_source": "phase0 exits; target process restores state",
                    "live_ranks": sorted(live_ranks),
                    "target_rank": 1,
                    "collective": "gloo",
                    "collective_transition": "destroy/reinit",
                    "global_stop": True,
                    "steps_lost": 0,
                    "live_collective": False,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("LRS CPU4 · real gloo · 4 -> 3 -> 4")
        print(
            f"param_diff={parameter_diff:.3e} "
            f"target_diff={target_diff:.3e} "
            f"loss_diff={loss_diff:.3e} "
            f"sampler={sampler_match}"
        )
        print(f"evidence {evidence}")
        if not all(checks.values()):
            print("CPU4 FAILED")
            return 1
        print("CPU4 PASSED")
        return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
