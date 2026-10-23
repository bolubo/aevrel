from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .resources import resource_dir

SCHEMA_DIR = resource_dir("v1")
CONTRACT_VERSION = "1"
ERROR_CODES = frozenset(
    {
        "INVALID_SPEC",
        "UNSUPPORTED_FORMAT",
        "VERSION_MISMATCH",
        "SOURCE_UNREACHABLE",
        "SOURCE_NOT_FOUND",
        "INVENTORY_INCOMPLETE",
        "PLAN_INVALID",
        "TARGET_CONFLICT",
        "TARGET_LOCKED",
        "DISK_FULL",
        "PERMISSION_DENIED",
        "NETWORK_TRANSIENT",
        "CHECKSUM_MISMATCH",
        "VERIFY_FAILED",
        "COMMIT_INCOMPLETE",
        "TIMEOUT",
        "CANCELLED",
        "INTERNAL",
    }
)


def contract_version() -> str:
    return CONTRACT_VERSION


def validate_error(payload: dict[str, Any]) -> None:
    _validate("error", payload)


def validate_capabilities(payload: dict[str, Any]) -> None:
    _validate("capabilities", payload)


def validate_inventory(payload: dict[str, Any]) -> None:
    _validate("inventory", payload)
    for chunk in payload["chunks"]:
        if len(chunk["tensor_offset"]) != len(chunk["tensor_size"]):
            raise ValueError("inventory chunk offset/size rank mismatch")
        for start, end in chunk["byte_ranges"]:
            if start > end:
                raise ValueError("inventory chunk byte range is reversed")


def validate_ranks(payload: dict[str, Any]) -> None:
    _validate("ranks", payload)


def validate_task(payload: dict[str, Any]) -> None:
    _validate("task", payload)


def _validate(name: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"{name} contract must be an object")
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise ValueError(f"contract schema not found: {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError(
            f"{name} contract validation failed: {errors[0].message}"
        )
