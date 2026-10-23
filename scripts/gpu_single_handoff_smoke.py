from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from shardferry.cdp import VersionMarker, write_marker
from shardferry.cutover_service import CutoverService
from shardferry.handoff import CommandPauseHook
from shardferry.verification import CommandTargetReady


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dcp", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    root = Path(args.work_dir).resolve()
    if root.exists():
        raise SystemExit(f"work directory already exists: {root}")
    source = root / "source"
    target = root / "target"
    source.mkdir(parents=True)
    first = source / "step-100"
    shutil.copytree(Path(args.source_dcp), first)
    write_marker(
        first,
        VersionMarker(
            version="1",
            version_id="step-100",
            job_uid="gpu-single",
            step=100,
            parent_id=None,
            metadata_hash=sha256_file(first / ".metadata"),
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )
    report = Path(args.report).resolve() if args.report else root / "target-report.json"
    scripts = Path(__file__).resolve().parent
    pause = CommandPauseHook(
        [
            args.python,
            str(scripts / "gpu_single_finalize_checkpoint.py"),
            "--source-root",
            str(source),
            "--parent",
            "step-100",
            "--version-id",
            "step-200",
        ],
        ["/bin/true"],
    )
    ready = CommandTargetReady(
        [
            args.python,
            str(scripts / "gpu_single_target_ready.py"),
            "--ckpt",
            str(target / "shadow" / "{version_id}"),
            "--fixture",
            str(Path(args.fixture).resolve()),
            "--version",
            "{version_id}",
            "--migration-uid",
            "gpu-single",
            "--steps",
            str(args.steps),
            "--report-path",
            str(report),
        ]
    )
    service = CutoverService(source, target, pause_hook=pause, target_ready=ready)
    warm = service.follow_once()
    before = service.status()
    cutover = service.cutover(None)
    after = service.status()
    print(json.dumps({
        "warm": warm,
        "before_cutover": before,
        "cutover": cutover,
        "after": after,
        "target_report": json.loads(report.read_text(encoding="utf-8")),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
