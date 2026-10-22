from __future__ import annotations

from copy import deepcopy
from math import prod

import numpy as np


def valid_plan() -> dict:
    return {
        "version": "1",
        "migration_id": "toy-4to2",
        "strategy": "direct",
        "full_gather": False,
        "total_bytes": 128,
        "theoretical_min_bytes": 128,
        "nodes": [
            {
                "id": "n0",
                "tensor": "w",
                "target_rank": 0,
                "op": "copy",
                "target_offset": [0, 0],
                "size": [4, 4],
                "inputs": [
                    {
                        "source_rank": 0,
                        "source_offset": [0, 0],
                        "checksum": "sha256:" + "0" * 64,
                    }
                ],
            },
            {
                "id": "n1",
                "tensor": "w",
                "target_rank": 1,
                "op": "copy",
                "target_offset": [4, 0],
                "size": [4, 4],
                "inputs": [
                    {
                        "source_rank": 1,
                        "source_offset": [4, 0],
                        "checksum": "sha256:" + "1" * 64,
                    }
                ],
            },
        ],
    }


def check_plan(plan: dict, spec: dict) -> list[str]:
    errors: list[str] = []
    tensor = spec["checkpoint"]["tensors"][0]
    shape = tuple(tensor["shape"])
    source_world = int(spec["source"]["mesh"]["world_size"])
    target_world = int(spec["target"]["mesh"]["world_size"])

    if plan.get("full_gather") is not False:
        errors.append("plan must not full-gather")
    if plan.get("strategy") not in {"direct", "relay"}:
        errors.append("invalid strategy")
    if not plan.get("nodes"):
        errors.append("plan has no nodes")
        return errors

    covered: set[tuple[int, ...]] = set()
    total_bytes = 0
    for node in plan["nodes"]:
        if not (0 <= node["target_rank"] < target_world):
            errors.append(f"{node.get('id')}: target rank out of range")
        if node.get("op") == "copy" and len(node.get("inputs", [])) != 1:
            errors.append(f"{node.get('id')}: copy requires one input")
        for source_input in node.get("inputs", []):
            checksum = source_input.get("checksum", "")
            if (
                not checksum.startswith("sha256:")
                or len(checksum) != len("sha256:") + 64
            ):
                errors.append(f"{node.get('id')}: invalid checksum")
            if not (0 <= source_input["source_rank"] < source_world):
                errors.append(f"{node.get('id')}: source rank out of range")

        target_offset = tuple(node["target_offset"])
        size = tuple(node["size"])
        if len(target_offset) != len(shape) or len(size) != len(shape):
            errors.append(f"{node.get('id')}: rank mismatch")
            continue
        for dim, (offset, length) in enumerate(zip(target_offset, size)):
            if offset + length > shape[dim]:
                errors.append(f"{node.get('id')}: target range out of bounds")
        for index in np.ndindex(*size):
            cell = tuple(offset + step for offset, step in zip(target_offset, index))
            if cell in covered:
                errors.append(f"{node.get('id')}: overlapping target cell {cell}")
            covered.add(cell)
        total_bytes += 4 * prod(size) * len(node.get("inputs", []))

    expected = set(np.ndindex(*shape))
    if covered != expected:
        errors.append("target coverage is incomplete")
    if total_bytes != plan.get("total_bytes"):
        errors.append("total_bytes does not match node sizes")
    if plan.get("theoretical_min_bytes", 0) > total_bytes:
        errors.append("theoretical minimum exceeds plan bytes")
    return errors


def drop_chunk(plan: dict) -> None:
    plan["nodes"].pop()


def duplicate_chunk(plan: dict) -> None:
    plan["nodes"].append(deepcopy(plan["nodes"][0]))


def shift_offset(plan: dict) -> None:
    plan["nodes"][1]["target_offset"][0] += 1


def wrong_target_rank(plan: dict) -> None:
    plan["nodes"][0]["target_rank"] = 999


def wrong_source_rank(plan: dict) -> None:
    plan["nodes"][0]["inputs"][0]["source_rank"] = 999


def drop_checksum(plan: dict) -> None:
    plan["nodes"][0]["inputs"][0]["checksum"] = ""


def mark_full_gather(plan: dict) -> None:
    plan["full_gather"] = True
