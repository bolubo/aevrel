from __future__ import annotations

from shardferry.acceptance import check_report, thresholds_for_size


def test_acceptance_passes_p1_small_report() -> None:
    report = {
        "size_mb": 128,
        "mode": "cdp",
        "wall_seconds": 2.0,
        "max_rss_mb": 50.0,
        "versions": [
            {
                "version_id": "step-1000",
                "plan_seconds": 0.02,
                "shadow_lag": 0,
                "target_storage_ratio": 1.0,
            }
        ],
        "cutover": {"paused_seconds": 0.05},
    }
    assert check_report(report) == []


def test_acceptance_rejects_lag_and_bad_rto() -> None:
    report = {
        "size_mb": 1024,
        "mode": "cdp",
        "wall_seconds": 60.0,
        "max_rss_mb": 250.0,
        "versions": [
            {
                "version_id": "step-1000",
                "plan_seconds": 0.02,
                "shadow_lag": 2,
                "target_storage_ratio": 2.0,
            }
        ],
        "cutover": {"paused_seconds": 2.0},
    }
    failures = check_report(report, thresholds=thresholds_for_size(1024))
    assert any("wall_seconds" in item for item in failures)
    assert any("max_rss_mb" in item for item in failures)
    assert any("cutover pause" in item for item in failures)
    assert any("shadow_lag" in item for item in failures)
    assert any("target_storage_ratio" in item for item in failures)


def test_plan_mode_only_checks_plan_budget() -> None:
    report = {
        "size_mb": 10240,
        "mode": "plan",
        "versions": [
            {
                "version_id": "step-1000",
                "plan_seconds": 0.02,
            }
        ],
    }
    assert check_report(report) == []
