from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA_DIR = Path(__file__).resolve().parents[2] / "spec" / "v1"


def _schema(name: str) -> dict:
    path = SCHEMA_DIR / f"{name}.schema.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(payload)
    return payload


def test_control_schemas_accept_minimal_objects() -> None:
    record = {
        "checkpoint": "/tmp/source.dcp",
        "namespace": "default",
        "job_uid": "job-42",
        "version": "step-100",
        "step": 100,
        "provider": "dcp",
        "format_version": "1.0.0",
        "metadata_digest": "sha256:abc",
        "ranks": [
            {
                "rank": 0,
                "shards": [
                    {
                        "name": "model.weight",
                        "relative_path": "__0_0.distcp",
                        "rank": 0,
                        "offset": 0,
                        "length": 16,
                        "size_bytes": 16,
                    }
                ],
                "bytes": 16,
            }
        ],
        "observed_at": "2026-10-22T00:00:00+00:00",
        "latest_mtime": "2026-10-22T00:00:00+00:00",
        "complete": True,
        "reasons": [],
    }
    Draft202012Validator(_schema("checkpoint-record")).validate(record)

    readiness = {
        "status": "UNVERIFIED",
        "complete": True,
        "fresh": True,
        "loadable": None,
        "movable": True,
        "reasons": [
            {"code": "DRILL_MISSING", "detail": "no restore drill"}
        ],
        "observed_at": "2026-10-22T00:00:00+00:00",
        "target_world_size": 4,
        "estimated_rto_seconds": 0,
        "last_drill": None,
    }
    Draft202012Validator(_schema("readiness")).validate(readiness)


def test_control_schemas_accept_lifecycle_objects() -> None:
    Draft202012Validator(_schema("restore-drill")).validate(
        {
            "record_version": "step-100",
            "job_name": "target-drill",
            "version": "step-100",
            "target_world_size": 4,
            "steps": 10,
            "phase": "Succeeded",
            "started_at": "2026-10-22T00:00:00+00:00",
            "finished_at": "2026-10-22T00:01:00+00:00",
            "proof_ref": "proof://drill/1",
            "reasons": [],
        }
    )
    Draft202012Validator(_schema("rescue-decision")).validate(
        {
            "event": "preempted",
            "action": "migrate",
            "reason": "fresh checkpoint and target ready",
            "target": "cluster-b",
            "proof_ref": "proof://decision/1",
            "decided_at": "2026-10-22T00:00:00+00:00",
        }
    )
