from __future__ import annotations

import pytest

from shardferry.reports import ReportError, attach_proof, summarize_reports


def _report(rank: int, chunks: int, bytes_moved: int) -> dict:
    return {
        "version": "1",
        "plan_id": "sha256:" + "a" * 64,
        "target_rank": rank,
        "status": "success",
        "meshes": {
            "source": {
                "shape": [2],
                "world_size": 2,
                "placements": ["Shard(0)"],
            },
            "target": {
                "shape": [3],
                "world_size": 3,
                "placements": ["Shard(0)"],
            },
        },
        "tensors": [
            {
                "name": "w",
                "shape": [8, 4],
                "dtype": "float32",
                "chunks": chunks,
            }
        ],
        "chunks": chunks,
        "bytes_planned": bytes_moved,
        "bytes_moved": bytes_moved,
        "duration_seconds": 1.0,
        "retries": 0,
        "verification": {
            "passed": True,
            "method": "bitwise",
            "details": "ok",
        },
    }


def test_summarize_per_rank_reports() -> None:
    summary = summarize_reports([_report(0, 2, 32), _report(1, 3, 48)])
    assert summary["target_ranks"] == [0, 1]
    assert summary["chunks"] == 5
    assert summary["bytes_planned"] == 80
    assert summary["tensors"][0]["chunks"] == 5
    assert summary["verification"]["passed"] is True


def test_attach_proof_to_report() -> None:
    proof_root = "sha256:" + "b" * 64
    result = attach_proof(
        _report(0, 2, 32),
        "s3://artifacts/proofs/rank0.json",
        {"root": proof_root},
    )
    assert result["proof_ref"].startswith("s3://")
    assert result["proof_hash"] == proof_root


def test_summary_keeps_shared_proof_attachment() -> None:
    proof_ref = "s3://artifacts/proofs/job.json"
    proof_hash = "sha256:" + "b" * 64
    reports = [
        attach_proof(_report(0, 2, 32), proof_ref, {"root": proof_hash}),
        attach_proof(_report(1, 3, 48), proof_ref, {"root": proof_hash}),
    ]
    summary = summarize_reports(reports)
    assert summary["proof_ref"] == proof_ref
    assert summary["proof_hash"] == proof_hash


def test_summarize_rejects_duplicate_target_rank() -> None:
    with pytest.raises(ReportError):
        summarize_reports([_report(0, 2, 32), _report(0, 3, 48)])


def _expected_rank(
    chunks: int,
    bytes_planned: int,
    tensor_chunks: int | None = None,
) -> dict:
    return {
        "chunks": chunks,
        "bytes_planned": bytes_planned,
        "tensors": {"w": chunks if tensor_chunks is None else tensor_chunks},
    }


def test_summarize_rejects_missing_expected_target_rank() -> None:
    with pytest.raises(ReportError, match="missing target ranks: 2"):
        summarize_reports(
            [_report(0, 2, 32), _report(1, 3, 48)],
            expected_by_rank={
                0: _expected_rank(2, 32),
                1: _expected_rank(3, 48),
                2: _expected_rank(4, 64),
            },
        )


def test_summarize_rejects_unexpected_target_rank() -> None:
    with pytest.raises(ReportError, match="unexpected target ranks: 2"):
        summarize_reports(
            [
                _report(0, 2, 32),
                _report(1, 3, 48),
                _report(2, 4, 64),
            ],
            expected_by_rank={
                0: _expected_rank(2, 32),
                1: _expected_rank(3, 48),
            },
        )


def test_summarize_rejects_rank_work_mismatch() -> None:
    with pytest.raises(ReportError, match="target rank 1 chunks"):
        summarize_reports(
            [_report(0, 2, 32), _report(1, 3, 48)],
            expected_by_rank={
                0: _expected_rank(2, 32),
                1: _expected_rank(4, 48),
            },
        )


def test_summarize_rejects_failed_rank_in_complete_set() -> None:
    failed = _report(1, 3, 48)
    failed["status"] = "failed"
    with pytest.raises(ReportError, match="target rank 1 status is failed"):
        summarize_reports(
            [_report(0, 2, 32), failed],
            expected_by_rank={
                0: _expected_rank(2, 32),
                1: _expected_rank(3, 48),
            },
        )


def test_summarize_accepts_complete_expected_rank_set() -> None:
    summary = summarize_reports(
        [_report(0, 2, 32), _report(1, 3, 48)],
        expected_by_rank={
            0: _expected_rank(2, 32),
            1: _expected_rank(3, 48),
        },
    )
    assert summary["status"] == "success"
    assert summary["target_ranks"] == [0, 1]
