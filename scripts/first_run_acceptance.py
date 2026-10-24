#!/usr/bin/env python3
"""Accept the documented clean first-run DCP path."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
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


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aevrel-first-run-") as temporary:
        work = Path(temporary) / "demo"
        demo = run(
            [
                sys.executable,
                "-m",
                "aevrel",
                "demo",
                "--dcp",
                "--work-dir",
                str(work),
                "--json",
            ]
        )
        payload = json.loads(demo.stdout)
        dcp = payload["dcp"]
        assert payload["verification"]["passed"] is True
        assert dcp["source_readiness"]["status"] == "READY"
        assert dcp["target_readiness"]["status"] == "READY"
        assert dcp["target_load"] == "ok"

        source = Path(dcp["source"]).parent / "source"
        before = tree_digest(source)
        readiness = run(
            [
                sys.executable,
                "-m",
                "aevrel",
                "readiness",
                "--checkpoint",
                dcp["source"],
                "--target-world",
                "3",
                "--json",
            ]
        )
        readiness_payload = json.loads(readiness.stdout)
        assert readiness_payload["readiness"]["status"] in {
            "READY",
            "UNVERIFIED",
        }
        assert tree_digest(source) == before

        print(
            json.dumps(
                {
                    "status": "ok",
                    "source_readiness": dcp["source_readiness"]["status"],
                    "target_readiness": dcp["target_readiness"]["status"],
                    "target_load": dcp["target_load"],
                    "source_unchanged": True,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"first-run acceptance failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
