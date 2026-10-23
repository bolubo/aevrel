from __future__ import annotations

import pytest

from aevrel.reports import ReportError, summarize_reports


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


def test_summarize_rejects_duplicate_target_rank() -> None:
    with pytest.raises(ReportError):
        summarize_reports([_report(0, 2, 32), _report(0, 3, 48)])
