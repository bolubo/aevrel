"""User-facing checkpoint readiness command."""

from __future__ import annotations

import json
import sys
from argparse import Namespace

from ..local import LocalError
from ..local.dcp import DCPStateProvider
from ..local.readiness import assess_readiness


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
            print(f"readiness: {exc.code}: {exc}", file=sys.stderr)
        return 2

    readiness = assess_readiness(
        record,
        max_age_seconds=args.max_age_seconds,
        target_world_size=args.target_world,
    )
    payload = {
        "record": record.to_dict(),
        "readiness": readiness.to_dict(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"checkpoint: {record.checkpoint}")
        print(f"provider:   {record.provider}")
        print(f"version:    {record.version}")
        print(f"ranks:      {len(record.ranks)}")
        print(f"complete:   {record.complete}")
        print(f"fresh:      {readiness.fresh}")
        print(
            "loadable:   "
            + (
                "unknown"
                if readiness.loadable is None
                else str(readiness.loadable)
            )
        )
        print(
            "movable:    "
            + (
                "unknown"
                if readiness.movable is None
                else str(readiness.movable)
            )
        )
        print(f"status:     {readiness.status.value}")
        for reason in readiness.reasons:
            print(f"  {reason.code}: {reason.detail}")
        if readiness.status.value != "READY":
            print(
                "next: run a restore drill / target load before treating "
                "this checkpoint as recoverable"
            )

    if args.strict and readiness.status.value != "READY":
        return 2
    return 0
