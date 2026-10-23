#!/usr/bin/env python3
"""Local rescue demo: interrupt a real DCP migration, then resume it."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from shardferry.dcp import export_dcp, migrate_dcp  # noqa: E402
from shardferry.executor import apply  # noqa: E402
from shardferry.planning import plan  # noqa: E402
from tests.fixtures.generated import (  # noqa: E402
    generate_sharded_spec,
    materialize_source_files,
)


class InjectedInterruption(KeyboardInterrupt):
    """A deterministic user-interruption fault for the local demo."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
    return "sha256:" + digest.hexdigest()


def _make_source(root: Path, dim: int) -> Path:
    fixture = root / "fixture"
    spec, arrays, _ = generate_sharded_spec(
        (dim, dim),
        (4,),
        ("Shard(0)",),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, fixture)
    apply(plan(spec), spec)
    source = root / "source.dcp"
    export_dcp(spec, source)
    shutil.rmtree(fixture)
    return source


def _find_journal(work: Path) -> Path:
    journals = sorted(work.rglob("journal.json"))
    if not journals:
        raise RuntimeError("migration produced no journal")
    return journals[0]


def _read_journal(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _timeline_html(events: list[dict[str, Any]]) -> str:
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(event['at'])}</td>"
        f"<td>{html.escape(event['attempt'])}</td>"
        f"<td>{html.escape(event['phase'])}</td>"
        f"<td>{html.escape(event['detail'])}</td>"
        "</tr>"
        for event in events
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>shardferry rescue timeline</title>
<style>
body {{ font: 14px/1.5 system-ui, sans-serif; margin: 32px; color: #18212b; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 1px solid #d8dee7; padding: 8px; text-align: left; }}
th {{ background: #f4f7fa; }}
.ok {{ color: #18794e; font-weight: 600; }}
</style>
<h1>shardferry rescue timeline</h1>
<p class="ok">interruption → journal preserved → resume → target verified</p>
<table>
<thead><tr><th>Time</th><th>Attempt</th><th>Phase</th><th>Detail</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interrupt a real local DCP migration and resume it."
    )
    parser.add_argument("--work-dir", default="shardferry-rescue")
    parser.add_argument("--dim", type=int, default=512)
    parser.add_argument("--interrupt-after-nodes", type=int, default=1)
    args = parser.parse_args()
    if args.dim < 16:
        print("rescue-demo: --dim must be >= 16", file=sys.stderr)
        return 2
    if args.interrupt_after_nodes < 1:
        print(
            "rescue-demo: --interrupt-after-nodes must be positive",
            file=sys.stderr,
        )
        return 2

    root = Path(args.work_dir).resolve()
    if root.exists() and any(root.iterdir()):
        print(
            f"rescue-demo: work directory is not empty: {root}",
            file=sys.stderr,
        )
        return 2
    root.mkdir(parents=True, exist_ok=True)
    source = _make_source(root, args.dim)
    work = root / "work"
    output = root / "target.dcp"
    source_before = _tree_digest(source)
    events: list[dict[str, Any]] = []

    def record(attempt: str, phase: str, detail: str) -> None:
        events.append(
            {
                "at": _utc_now(),
                "attempt": attempt,
                "phase": phase,
                "detail": detail,
            }
        )

    interrupted = False

    def interrupt_progress(event: dict[str, Any]) -> None:
        nonlocal interrupted
        if (
            event.get("phase") == "transfer"
            and int(event.get("completed_nodes", 0))
            >= args.interrupt_after_nodes
            and not interrupted
        ):
            interrupted = True
            raise InjectedInterruption(
                "simulated user interruption after "
                f"{event['completed_nodes']} completed nodes"
            )

    record("first", "start", "source DCP indexed; starting migration")
    first_started = time.perf_counter()
    try:
        migrate_dcp(
            source,
            output,
            work,
            target_world_size=3,
            keep_work=True,
            progress=lambda message: record("first", "progress", message),
            progress_event=interrupt_progress,
        )
    except InjectedInterruption as exc:
        first_seconds = time.perf_counter() - first_started
        record("first", "interrupted", str(exc))
    else:
        raise RuntimeError(
            "migration completed before the injected interruption"
        )

    journal = _find_journal(work)
    before_journal = _read_journal(journal)
    completed = len(before_journal.get("completed_nodes", {}))
    if completed < args.interrupt_after_nodes:
        raise RuntimeError(
            f"journal has {completed} completed nodes; "
            f"expected at least {args.interrupt_after_nodes}"
        )
    if before_journal.get("status") != "in_progress":
        raise RuntimeError(
            f"journal status after interruption is {before_journal.get('status')!r}"
        )

    record(
        "resume",
        "start",
        f"same command; journal has {completed} completed node(s)",
    )
    resume_started = time.perf_counter()
    migrate_dcp(
        source,
        output,
        work,
        target_world_size=3,
        keep_work=True,
        progress=lambda message: record("resume", "progress", message),
    )
    resume_seconds = time.perf_counter() - resume_started
    after_journal = _read_journal(journal)
    record("resume", "committed", f"target exported: {output}")

    source_after = _tree_digest(source)
    if source_before != source_after:
        raise RuntimeError("source checkpoint changed during rescue demo")

    target_env = dict(__import__("os").environ)
    target_env["PYTHONPATH"] = str(ROOT / "src")
    load = subprocess.run(
        [
            sys.executable,
            "-m",
            "shardferry.commands.target_load",
            "--checkpoint",
            str(output),
            "--json",
        ],
        cwd=ROOT,
        env=target_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if load.returncode != 0:
        raise RuntimeError(f"target load failed: {load.stderr.strip()}")
    load_result = json.loads(load.stdout)
    if load_result.get("status") != "ok":
        raise RuntimeError(f"target load was not ok: {load_result}")

    proof = {
        "schema": "shardferry.rescue.local.v1",
        "work_dir": str(root),
        "source": str(source),
        "target": str(output),
        "interrupt_after_nodes": args.interrupt_after_nodes,
        "first_attempt_seconds": round(first_seconds, 6),
        "resume_seconds": round(resume_seconds, 6),
        "journal_before": before_journal,
        "journal_after": after_journal,
        "resumed": int(after_journal.get("resumes", 0)) > 0,
        "completed_nodes_before_resume": completed,
        "source_sha256_before": source_before,
        "source_sha256_after": source_after,
        "source_unchanged": True,
        "target_load": load_result,
        "events": events,
    }
    (root / "rescue-proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "timeline.html").write_text(
        _timeline_html(events),
        encoding="utf-8",
    )
    print("Rescue demo complete")
    print(f"  work dir:             {root}")
    print(f"  interrupted after:    {completed} completed node(s)")
    print(f"  first attempt:        {first_seconds:.3f}s")
    print(f"  resume:               {resume_seconds:.3f}s")
    print(f"  resumed journal:      {proof['resumed']}")
    print(f"  source unchanged:     {proof['source_unchanged']}")
    print(f"  target load:          {load_result['status']}")
    print(f"  proof:                {root / 'rescue-proof.json'}")
    print(f"  timeline:             {root / 'timeline.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
