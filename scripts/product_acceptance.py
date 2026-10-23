#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = dict(__import__("os").environ)
    process_env.update(env or {})
    result = subprocess.run(
        command,
        cwd=cwd,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def make_dcp(root: Path) -> Path:
    from shardferry.dcp import export_dcp
    from shardferry.executor import apply
    from shardferry.planning import plan
    from tests.fixtures.generated import (
        generate_sharded_spec,
        materialize_source_files,
    )

    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, root)
    apply(plan(spec), spec)
    checkpoint = root / "source.dcp"
    export_dcp(spec, checkpoint)
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--skip-static", action="store_true")
    args = parser.parse_args()
    root = (
        Path(args.work_dir).resolve()
        if args.work_dir
        else Path(tempfile.mkdtemp(prefix="shardferry-product-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, object] = {"work_dir": str(root)}
    env = {"PYTHONPATH": str(ROOT / "src")}
    try:
        demo_dir = root / "demo"
        run(
            [sys.executable, "-m", "shardferry", "demo", "--work-dir", str(demo_dir)],
            env=env,
        )
        results["demo"] = {"ok": True, "work_dir": str(demo_dir)}

        try:
            checkpoint = make_dcp(root / "dcp")
        except ImportError as exc:
            results["dcp"] = {"ok": False, "skipped": True, "reason": str(exc)}
        else:
            output = root / "dcp" / "target.dcp"
            work = root / "dcp" / "work"
            migrate = run(
                [
                    sys.executable,
                    "-m",
                    "shardferry",
                    "migrate",
                    "--checkpoint",
                    str(checkpoint),
                    "--target-world",
                    "3",
                    "--output",
                    str(output),
                    "--work-dir",
                    str(work),
                    "--json",
                ],
                env=env,
            )
            payload = json.loads(migrate.stdout)
            results["dcp"] = {
                "ok": payload["status"] == "success",
                "output": payload["output"],
                "verification": payload["verification"],
            }
            target_load = run(
                [
                    sys.executable,
                    "-m",
                    "shardferry.commands.target_load",
                    "--checkpoint",
                    payload["output"],
                    "--json",
                ],
                env=env,
            )
            load_payload = json.loads(target_load.stdout)
            results["target_load"] = {
                "ok": load_payload["status"] == "ok",
                "tensors": load_payload["tensors"],
                "duration_seconds": load_payload["duration_seconds"],
            }

        if not args.skip_static:
            release = run(
                [sys.executable, str(ROOT / "scripts" / "check_release.py"), "--json"]
            )
            results["release"] = {
                "ok": json.loads(release.stdout)["status"] == "ok",
            }
            support_help = run(
                [
                    sys.executable,
                    "-m",
                    "shardferry.commands.support_bundle",
                    "--help",
                ],
                env=env,
            )
            results["support_bundle"] = {
                "ok": "support bundle" in support_help.stdout.lower(),
            }
            run(["go", "test", "./cmd/ctl", "./internal/controller"])
            run(["helm", "lint", "deploy/helm/shardferry", "--set", "tls.enabled=false"])
            ctl = run(
                [
                    "go",
                    "run",
                    "./cmd/ctl",
                    "migrate",
                    "-f",
                    "examples/k8s/migration.yaml",
                    "--dry-run",
                ]
            )
            results["shardferryctl"] = {
                "ok": "agentSelector" in ctl.stdout,
                "dry_run": True,
            }
        results["ok"] = all(
            bool(value.get("ok"))
            for key, value in results.items()
            if key != "work_dir"
        )
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0 if results["ok"] else 2
    finally:
        if not args.work_dir:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
