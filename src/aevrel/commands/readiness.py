"""User-facing checkpoint readiness command."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

from ..local import LocalError
from ..local.dcp import DCPStateProvider
from ..local.models import CheckpointRecord, DrillOutcome
from ..local.readiness import assess_readiness
from ..verification.target_report import (
    TargetReportError,
    load_target_ready_report,
)
from ..theme import print_section, print_result_block, status_label


def _kv(label: str, value: object, width: int = 12) -> None:
    print(f"  {label:<{width}} {value}")


def run_readiness(args: Namespace) -> int:
    provider = DCPStateProvider()
    try:
        record = provider.inventory(
            args.checkpoint,
            namespace=args.namespace,
            job_uid=args.job_uid,
            version=args.version,
            step=args.step,
        )
    except LocalError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "failed", "error": exc.to_dict()}))
        else:
            print(f"aevrel readiness: {exc.code}: {exc}", file=sys.stderr)
            print(f"RESULT: FAILED ({exc.code})", file=sys.stderr)
        return 2

    drill = None
    if args.drill_report:
        try:
            drill = _load_drill_outcome(args.drill_report, record)
        except LocalError as exc:
            if getattr(args, "json", False):
                print(json.dumps({"status": "failed", "error": exc.to_dict()}))
            else:
                print(f"aevrel readiness: {exc.code}: {exc}", file=sys.stderr)
                print(f"RESULT: FAILED ({exc.code})", file=sys.stderr)
            return 2

    readiness = assess_readiness(
        record,
        max_age_seconds=args.max_age_seconds,
        target_world_size=args.target_world,
        drill=drill,
    )
    payload = {
        "record": record.to_dict(),
        "readiness": readiness.to_dict(),
    }
    if drill is not None:
        payload["drill"] = drill.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_section("READINESS")
        _kv("checkpoint", record.checkpoint)
        _kv("provider", record.provider)
        _kv("version", record.version)
        _kv("ranks", len(record.ranks))
        _kv("complete", record.complete)
        _kv("fresh", readiness.fresh)
        _kv(
            "loadable",
            "unknown" if readiness.loadable is None else readiness.loadable,
        )
        _kv(
            "movable",
            "unknown" if readiness.movable is None else readiness.movable,
        )
        _kv("status", status_label(readiness.status.value))
        if drill is not None:
            _kv("drill", f"{drill.phase} ({drill.proof_ref})")
            if drill.detail:
                print(f"  {drill.detail}")
        for reason in readiness.reasons:
            print(f"  {reason.code}: {reason.detail}")
        if readiness.status.value == "READY":
            print_result_block(True)
        else:
            print("RESULT: NOT READY")
            print(
                "NEXT: run a restore drill / target load before treating "
                "this checkpoint as recoverable"
            )

    if args.strict and readiness.status.value != "READY":
        return 2
    return 0


def _load_drill_outcome(
    paths: list[str],
    record: CheckpointRecord,
) -> DrillOutcome:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            files.append(path)
        else:
            raise LocalError(
                "DRILL_REPORT_INVALID",
                f"drill report path not found: {path}",
            )
    if not files:
        raise LocalError(
            "DRILL_REPORT_INVALID",
            "drill report path contains no JSON reports",
        )

    reports: list[dict] = []
    seen: dict[int, Path] = {}
    for path in files:
        try:
            payload = load_target_ready_report(path)
        except (OSError, TargetReportError, ValueError) as exc:
            raise LocalError(
                "DRILL_REPORT_INVALID",
                f"could not read drill report {path}: {exc}",
            ) from exc
        if payload["version"] != record.version:
            raise LocalError(
                "DRILL_REPORT_INVALID",
                f"{path}: report version {payload['version']!r} "
                f"does not match checkpoint {record.version!r}",
            )
        if payload["migrationUID"] != record.job_uid:
            raise LocalError(
                "DRILL_REPORT_INVALID",
                f"{path}: report migrationUID {payload['migrationUID']!r} "
                f"does not match checkpoint {record.job_uid!r}",
            )
        rank = int(payload["rank"])
        if rank in seen:
            raise LocalError(
                "DRILL_REPORT_INVALID",
                f"duplicate drill report for rank {rank}",
            )
        seen[rank] = path
        reports.append(payload)

    expected = {rank.rank for rank in record.ranks}
    missing = sorted(expected - set(seen))
    extra = sorted(set(seen) - expected)
    failed = [
        payload
        for payload in reports
        if not payload["ok"]
        or not payload["modelLoaded"]
        or not payload["optimizerLoaded"]
    ]
    proof_ref = ",".join(str(path) for path in files)
    steps_ran = min(int(payload["stepsRan"]) for payload in reports)
    details = []
    if failed:
        details.append(
            "target load failed for ranks "
            + ",".join(str(payload["rank"]) for payload in failed)
        )
    if missing:
        details.append(
            "missing drill reports for ranks "
            + ",".join(str(rank) for rank in missing)
        )
    if extra:
        details.append(
            "unexpected drill reports for ranks "
            + ",".join(str(rank) for rank in extra)
        )
    if details:
        return DrillOutcome(
            phase="Failed",
            succeeded=False,
            steps_ran=steps_ran,
            proof_ref=proof_ref,
            detail="; ".join(details),
        )
    return DrillOutcome(
        phase="Succeeded",
        succeeded=True,
        steps_ran=steps_ran,
        proof_ref=proof_ref,
    )
