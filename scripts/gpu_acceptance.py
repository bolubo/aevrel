#!/usr/bin/env python3
"""Run the one-shot 4-GPU -> 2-GPU acceptance flow.

This script intentionally does not hide the individual steps: it invokes the
existing source-save, shardferry migrate, and target-load commands, keeps
their logs, and writes one JSON report. It must be run only on a machine where
the owner has approved the GPU spend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]


@dataclass
class Step:
    name: str
    argv: list[str]
    returncode: int | None = None
    duration_seconds: float | None = None
    stdout: str | None = None
    stderr: str | None = None


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _run(
    step: Step,
    log_dir: Path,
    *,
    timeout: int,
    dry_run: bool,
) -> Step:
    print("+ " + " ".join(step.argv), flush=True)
    if dry_run:
        step.returncode = 0
        step.duration_seconds = 0.0
        return step
    stdout_path = log_dir / f"{step.name}.stdout.log"
    stderr_path = log_dir / f"{step.name}.stderr.log"
    started = time.perf_counter()
    with stdout_path.open("w", encoding="utf-8") as stdout, (
        stderr_path.open("w", encoding="utf-8")
    ) as stderr:
        completed = subprocess.run(
            step.argv,
            cwd=REPO,
            stdout=stdout,
            stderr=stderr,
            check=False,
            timeout=timeout,
        )
    step.duration_seconds = round(time.perf_counter() - started, 6)
    step.returncode = completed.returncode
    step.stdout = str(stdout_path.relative_to(log_dir.parent))
    step.stderr = str(stderr_path.relative_to(log_dir.parent))
    return step


def _torchrun_prefix() -> list[str]:
    if shutil.which("torchrun"):
        return ["torchrun"]
    return [sys.executable, "-m", "torch.distributed.run"]


def _check_cuda() -> dict[str, Any]:
    import importlib.util

    if importlib.util.find_spec("torch") is None:
        raise RuntimeError("torch is not installed")
    code = (
        "import json, torch; "
        "print(json.dumps({'torch': torch.__version__, "
        "'cuda': torch.version.cuda, "
        "'available': torch.cuda.is_available(), "
        "'devices': torch.cuda.device_count()}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "torch probe failed")
    return json.loads(completed.stdout.strip())


def _nvidia_smi() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    completed = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one 4->2 GPU acceptance flow: source FSDP DCP save, "
            "shardferry migration, and target FSDP load."
        )
    )
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--source-gpus", type=int, default=4)
    parser.add_argument("--target-gpus", type=int, default=2)
    parser.add_argument("--dtype", choices=["float32", "bfloat16"], default="float32")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.source_gpus < 1 or args.target_gpus < 1:
        print("gpu-acceptance: GPU counts must be positive", file=sys.stderr)
        return 2
    work_dir = (
        Path(args.work_dir).expanduser().resolve()
        if args.work_dir
        else Path.cwd() / "shardferry-gpu-acceptance"
    )
    if not args.dry_run and work_dir.exists() and any(work_dir.iterdir()):
        print(
            f"gpu-acceptance: work dir is not empty: {work_dir}",
            file=sys.stderr,
        )
        return 2
    work_dir.mkdir(parents=True, exist_ok=True)
    log_dir = work_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    source = work_dir / "source.dcp"
    target = work_dir / "target.dcp"
    fixture = work_dir / "fixture.pt"
    report_path = work_dir / "gpu-acceptance.json"
    source_script = REPO / "scripts" / "gpu_source_save.py"
    target_script = REPO / "scripts" / "gpu_target_load.py"
    torchrun = _torchrun_prefix()

    steps = [
        Step(
            "source_save",
            [
                *torchrun,
                "--standalone",
                f"--nproc_per_node={args.source_gpus}",
                str(source_script),
                "--out",
                str(source),
                "--fixture",
                str(fixture),
                "--dtype",
                args.dtype,
            ],
        ),
        Step(
            "migrate",
            [
                sys.executable,
                "-m",
                "shardferry",
                "migrate",
                "--checkpoint",
                str(source),
                "--target-world",
                str(args.target_gpus),
                "--output",
                str(target),
            ],
        ),
        Step(
            "target_load",
            [
                *torchrun,
                "--standalone",
                f"--nproc_per_node={args.target_gpus}",
                str(target_script),
                "--ckpt",
                str(target),
                "--fixture",
                str(fixture),
                "--dtype",
                args.dtype,
            ],
        ),
    ]

    report: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "work_dir": str(work_dir),
        "source_gpus": args.source_gpus,
        "target_gpus": args.target_gpus,
        "dtype": args.dtype,
        "nvidia_smi": _nvidia_smi(),
        "steps": [asdict(step) for step in steps],
        "source_digest_before": None,
        "source_digest_after": None,
        "source_unchanged": None,
    }

    if args.dry_run:
        for step in steps:
            _run(step, log_dir, timeout=args.timeout, dry_run=True)
        report["status"] = "dry-run"
        report["steps"] = [asdict(step) for step in steps]
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(report_path)
        return 0

    try:
        cuda = _check_cuda()
        report["cuda_probe"] = cuda
        if not cuda.get("available"):
            raise RuntimeError("CUDA is not available")
        if int(cuda.get("devices", 0)) < args.source_gpus:
            raise RuntimeError(
                f"need {args.source_gpus} GPUs, found {cuda.get('devices', 0)}"
            )
        if not source_script.is_file() or not target_script.is_file():
            raise RuntimeError("GPU source/target scripts are missing")
        if not args.dry_run:
            # source does not exist yet; the digest is recorded after save below.
            pass
        for index, step in enumerate(steps):
            step = _run(step, log_dir, timeout=args.timeout, dry_run=False)
            steps[index] = step
            if step.returncode != 0:
                raise RuntimeError(f"step {step.name} failed; see {work_dir}")
            if step.name == "source_save":
                if not source.is_dir():
                    raise RuntimeError(f"source DCP not created: {source}")
                report["source_digest_before"] = _tree_digest(source)
            if step.name == "migrate":
                report["source_digest_after"] = _tree_digest(source)
                report["source_unchanged"] = (
                    report["source_digest_before"]
                    == report["source_digest_after"]
                )
                if not report["source_unchanged"]:
                    raise RuntimeError("source DCP changed during migration")
        report["status"] = "passed"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["steps"] = [asdict(step) for step in steps]
        report["target_files"] = sorted(
            str(path.relative_to(target))
            for path in target.rglob("*")
            if path.is_file()
        ) if target.is_dir() else []
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["steps"] = [asdict(step) for step in steps]
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"gpu-acceptance failed: {exc}", file=sys.stderr)
        print(f"report: {report_path}", file=sys.stderr)
        return 2

    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
