from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from shardferry.contracts import (
    ERROR_CODES,
    validate_capabilities,
    validate_error,
    validate_inventory,
    validate_ranks,
    validate_task,
)

ROOT = Path(__file__).resolve().parents[2]


def test_error_contract_accepts_known_code() -> None:
    validate_error(
        {
            "version": "1",
            "code": "DISK_FULL",
            "category": "target",
            "retryable": False,
            "phase": "preparing",
            "rank": 7,
            "details": "/local/checkpoints is full",
        }
    )
    assert "DISK_FULL" in ERROR_CODES


def test_capabilities_contract_accepts_agent_features() -> None:
    validate_capabilities(
        {
            "version": "1",
            "apiVersion": "shardferry.io/v1",
            "agentVersion": "1.0.0",
            "roles": ["source", "target"],
            "features": ["dcp", "range-read", "bitwise-verify"],
        }
    )


def test_inventory_contract_accepts_dcp_chunk() -> None:
    validate_inventory(
        {
            "version": "1",
            "rank": 0,
            "format": "dcp",
            "metadata_hash": "sha256:" + "a" * 64,
            "tensors": [
                {"name": "w", "shape": [4, 4], "dtype": "float32"}
            ],
            "chunks": [
                {
                    "tensor": "w",
                    "rank": 0,
                    "file": "job42/__0_0.distcp",
                    "tensor_offset": [0, 0],
                    "tensor_size": [4, 4],
                    "byte_ranges": [[0, 64]],
                    "checksum": "sha256:" + "b" * 64,
                }
            ],
            "agent_version": "1.0.0",
            "torch_version": "2.14.1+cu128",
        }
    )


def test_rank_discovery_contract_accepts_local_ranks() -> None:
    validate_ranks({"version": "1", "ranks": [0, 2, 7]})


def test_task_contract_accepts_target_rank() -> None:
    validate_task(
        {
            "version": "1",
            "migration_uid": "m-123",
            "rank": 3,
            "task_hash": "sha256:" + "c" * 64,
            "target_root": "/local/checkpoints/rank3",
            "spec_ref": "spec.yaml",
            "plan_ref": "plan.json",
            "plan_slice": {"nodes": [], "bytes": 0},
            "state": "created",
        }
    )


def test_all_v1_schemas_are_valid() -> None:
    paths = sorted((ROOT / "spec" / "v1").glob("*.schema.json"))
    assert paths
    for path in paths:
        Draft202012Validator.check_schema(
            json.loads(path.read_text(encoding="utf-8"))
        )
