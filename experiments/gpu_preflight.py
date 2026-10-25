#!/usr/bin/env python3
"""Read-only GPU environment check before renting/running the matrix."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _probe_torch() -> dict:
    code = """
import json, torch
print(json.dumps({
  "torch": torch.__version__,
  "cuda": torch.version.cuda,
  "available": torch.cuda.is_available(),
  "devices": torch.cuda.device_count(),
  "device_names": [
    torch.cuda.get_device_name(index)
    for index in range(torch.cuda.device_count())
  ],
  "bf16": bool(getattr(torch.cuda, "is_bf16_supported", lambda: False)()),
  "nccl": bool(torch.distributed.is_nccl_available()),
  "cudnn": torch.backends.cudnn.version(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip() or "torch probe failed"}
    return json.loads(result.stdout)


def _probe_aevrel() -> str | None:
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(REPO / "src")
        + os.pathsep
        + env.get("PYTHONPATH", "")
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", "import aevrel; print(aevrel.__version__)"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _nvidia_smi() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check GPU acceptance prerequisites.")
    parser.add_argument("--min-gpus", type=int, default=4)
    parser.add_argument("--min-disk-gb", type=float, default=20.0)
    parser.add_argument("--work-dir", default="/tmp")
    parser.add_argument("--require-bf16", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    checks: list[str] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(f"python: {sys.version.split()[0]}")
    if not python_ok:
        checks.append("need Python 3.11+")
    probe = _probe_torch()
    checks.append("torch import")
    if probe.get("error"):
        checks.append("torch probe: " + probe["error"])
    checks.append("cuda available" if probe.get("available") else "cuda unavailable")
    checks.append(f"gpu count: {probe.get('devices', 0)}")
    if int(probe.get("devices", 0)) < args.min_gpus:
        checks.append(f"need at least {args.min_gpus} GPUs")
    checks.append("bf16 supported" if probe.get("bf16") else "bf16 unsupported")
    checks.append("nccl available" if probe.get("nccl") else "nccl unavailable")
    checks.append("torchrun available" if shutil.which("torchrun") else "torchrun missing")
    aevrel_version = _probe_aevrel()
    checks.append("aevrel import " + (aevrel_version or "failed"))
    checks.append(
        "source script available"
        if (REPO / "scripts/gpu_source_save.py").is_file()
        else "source script missing"
    )
    checks.append(
        "target script available"
        if (REPO / "scripts/gpu_target_load.py").is_file()
        else "target script missing"
    )
    work = Path(args.work_dir).expanduser().resolve()
    free = shutil.disk_usage(work).free
    checks.append(f"disk free: {free / 1024**3:.1f} GiB")
    if free < args.min_disk_gb * 1024**3:
        checks.append(f"need at least {args.min_disk_gb} GiB free")
    report = {
        "ok": (
            not probe.get("error")
            and python_ok
            and probe.get("available")
            and int(probe.get("devices", 0)) >= args.min_gpus
            and bool(probe.get("nccl"))
            and (not args.require_bf16 or bool(probe.get("bf16")))
            and shutil.which("torchrun") is not None
            and aevrel_version is not None
            and free >= args.min_disk_gb * 1024**3
        ),
        "probe": probe,
        "nvidia_smi": _nvidia_smi(),
        "checks": checks,
        "work_dir": str(work),
    }
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(check)
        print("GPU preflight ok" if report["ok"] else "GPU preflight blocked")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
