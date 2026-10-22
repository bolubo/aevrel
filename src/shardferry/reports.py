from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .executor import REPORT_SCHEMA_PATH


class ReportError(ValueError):
    """Raised when per-rank reports cannot be combined."""


def attach_proof(
    report: dict[str, Any],
    proof_ref: str,
    proof: dict[str, Any],
) -> dict[str, Any]:
    """Attach an immutable Merkle proof reference to one report."""
    if not proof_ref:
        raise ReportError("proof_ref must not be empty")
    root = proof.get("root")
    if not isinstance(root, str) or not root.startswith("sha256:"):
        raise ReportError("proof has no valid root")
    result = dict(report)
    result["proof_ref"] = proof_ref
    result["proof_hash"] = root
    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(result))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ReportError(f"invalid report with proof: {details}")
    return result


def summarize_reports(
    reports: list[dict[str, Any]],
    *,
    expected_by_rank: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not reports:
        raise ReportError("at least one report is required")
    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    for report in reports:
        errors = list(validator.iter_errors(report))
        if errors:
            details = "; ".join(error.message for error in errors)
            raise ReportError(f"invalid report: {details}")

    if expected_by_rank is not None:
        _validate_expected_ranks(reports, expected_by_rank)

    plan_id = reports[0]["plan_id"]
    meshes = reports[0]["meshes"]
    ranks: set[int] = set()
    tensors: dict[str, dict[str, Any]] = {}
    all_success = True
    all_verified = True

    for report in reports:
        if report["plan_id"] != plan_id:
            raise ReportError("reports have different plan ids")
        if report["meshes"] != meshes:
            raise ReportError("reports have different meshes")
        rank = report.get("target_rank")
        if rank is not None:
            if rank in ranks:
                raise ReportError(f"duplicate target rank: {rank}")
            ranks.add(rank)
        all_success = all_success and report["status"] == "success"
        all_verified = all_verified and report["verification"]["passed"]
        for tensor in report["tensors"]:
            current = tensors.setdefault(
                tensor["name"],
                {
                    "name": tensor["name"],
                    "shape": tensor["shape"],
                    "dtype": tensor["dtype"],
                    "chunks": 0,
                },
            )
            if (
                current["shape"] != tensor["shape"]
                or current["dtype"] != tensor["dtype"]
            ):
                raise ReportError(
                    f"tensor metadata mismatch: {tensor['name']}"
                )
            current["chunks"] += tensor["chunks"]

    result = {
        "version": "1",
        "plan_id": plan_id,
        "status": "success" if all_success else "failed",
        "meshes": meshes,
        "tensors": list(tensors.values()),
        "chunks": sum(report["chunks"] for report in reports),
        "bytes_planned": sum(report["bytes_planned"] for report in reports),
        "bytes_moved": sum(report["bytes_moved"] for report in reports),
        "duration_seconds": max(
            report["duration_seconds"] for report in reports
        ),
        "retries": sum(report["retries"] for report in reports),
        "target_ranks": sorted(ranks),
        "verification": {
            "passed": all_verified,
            "method": (
                reports[0]["verification"]["method"]
                if all(
                    report["verification"]["method"]
                    == reports[0]["verification"]["method"]
                    for report in reports
                )
                else "bitwise"
            ),
            "details": f"{len(reports)} rank report(s) aggregated",
        },
    }
    proof_refs = {
        report["proof_ref"]
        for report in reports
        if isinstance(report.get("proof_ref"), str)
    }
    proof_hashes = {
        report["proof_hash"]
        for report in reports
        if isinstance(report.get("proof_hash"), str)
    }
    if (
        len(proof_refs) == 1
        and len(proof_hashes) == 1
        and all(
            isinstance(report.get("proof_ref"), str)
            and isinstance(report.get("proof_hash"), str)
            for report in reports
        )
    ):
        result["proof_ref"] = next(iter(proof_refs))
        result["proof_hash"] = next(iter(proof_hashes))
    return result


def _validate_expected_ranks(
    reports: list[dict[str, Any]],
    expected_by_rank: dict[int, dict[str, Any]],
) -> None:
    reports_by_rank: dict[int, dict[str, Any]] = {}
    missing_rank_reports = []
    for report in reports:
        rank = report.get("target_rank")
        if rank is None:
            missing_rank_reports.append(report["plan_id"])
            continue
        reports_by_rank[rank] = report
    if missing_rank_reports:
        raise ReportError("complete summary requires target_rank on every report")

    expected_ranks = set(expected_by_rank)
    actual_ranks = set(reports_by_rank)
    missing = sorted(expected_ranks - actual_ranks)
    unexpected = sorted(actual_ranks - expected_ranks)
    if missing:
        raise ReportError(
            "missing target ranks: " + ", ".join(str(rank) for rank in missing)
        )
    if unexpected:
        raise ReportError(
            "unexpected target ranks: "
            + ", ".join(str(rank) for rank in unexpected)
        )

    for rank in sorted(expected_ranks):
        report = reports_by_rank[rank]
        expected = expected_by_rank[rank]
        if report["status"] != "success":
            raise ReportError(
                f"target rank {rank} status is {report['status']}"
            )
        if not report["verification"]["passed"]:
            raise ReportError(f"target rank {rank} verification failed")
        if report["chunks"] != expected["chunks"]:
            raise ReportError(
                f"target rank {rank} chunks: "
                f"{report['chunks']} != {expected['chunks']}"
            )
        if report["bytes_planned"] != expected["bytes_planned"]:
            raise ReportError(
                f"target rank {rank} bytes_planned: "
                f"{report['bytes_planned']} != {expected['bytes_planned']}"
            )
        tensor_chunks: dict[str, int] = {}
        for tensor in report["tensors"]:
            if tensor["name"] in tensor_chunks:
                raise ReportError(
                    f"target rank {rank} has duplicate tensor: "
                    f"{tensor['name']}"
                )
            tensor_chunks[tensor["name"]] = tensor["chunks"]
        if tensor_chunks != expected["tensors"]:
            raise ReportError(
                f"target rank {rank} tensor chunks: "
                f"{tensor_chunks} != {expected['tensors']}"
            )


def load_reports(directory: str | Path) -> list[dict[str, Any]]:
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ReportError(f"report directory not found: {root}")
    reports = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReportError(f"could not read report {path}: {exc}") from exc
        if isinstance(payload, dict) and "plan_id" in payload:
            reports.append(payload)
    if not reports:
        raise ReportError(f"no report JSON files found in: {root}")
    return reports
