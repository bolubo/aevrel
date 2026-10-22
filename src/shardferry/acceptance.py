from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PerformanceThresholds:
    max_wall_seconds: float
    max_rss_mb: float
    max_cutover_pause_seconds: float
    max_target_storage_ratio: float = 1.2
    max_plan_seconds: float = 1.0


def thresholds_for_size(size_mb: int) -> PerformanceThresholds:
    if size_mb <= 128:
        return PerformanceThresholds(5.0, 100.0, 0.2)
    if size_mb <= 1024:
        return PerformanceThresholds(30.0, 150.0, 1.0)
    return PerformanceThresholds(300.0, 500.0, 5.0)


def check_report(
    report: dict[str, Any],
    *,
    thresholds: PerformanceThresholds | None = None,
) -> list[str]:
    thresholds = thresholds or thresholds_for_size(int(report.get("size_mb", 1024)))
    failures: list[str] = []
    mode = report.get("mode", "cdp")
    versions = report.get("versions", [])
    if not isinstance(versions, list) or not versions:
        return ["report has no versions"]

    for version in versions:
        version_id = version.get("version_id", "?")
        plan_seconds = float(version.get("plan_seconds", 0.0))
        if plan_seconds > thresholds.max_plan_seconds:
            failures.append(
                f"{version_id}: plan_seconds {plan_seconds:.3f} > "
                f"{thresholds.max_plan_seconds:.3f}"
            )
        if mode == "plan":
            continue
        lag = int(version.get("shadow_lag", 0))
        if lag != 0:
            failures.append(f"{version_id}: shadow_lag {lag} != 0")
        ratio = float(version.get("target_storage_ratio", 1.0))
        if ratio > thresholds.max_target_storage_ratio:
            failures.append(
                f"{version_id}: target_storage_ratio {ratio:.3f} > "
                f"{thresholds.max_target_storage_ratio:.3f}"
            )

    if mode == "cdp":
        wall = float(
            report.get(
                "active_wall_seconds",
                report.get("wall_seconds", 0.0),
            )
        )
        if wall > thresholds.max_wall_seconds:
            failures.append(
                f"wall_seconds {wall:.3f} > {thresholds.max_wall_seconds:.3f}"
            )
        rss = float(report.get("max_rss_mb", 0.0))
        if rss > thresholds.max_rss_mb:
            failures.append(
                f"max_rss_mb {rss:.3f} > {thresholds.max_rss_mb:.3f}"
            )
        cutover = report.get("cutover")
        if not isinstance(cutover, dict):
            failures.append("cutover report is missing")
        else:
            pause = float(cutover.get("paused_seconds", 0.0))
            if pause > thresholds.max_cutover_pause_seconds:
                failures.append(
                    f"cutover pause {pause:.3f} > "
                    f"{thresholds.max_cutover_pause_seconds:.3f}"
                )
            ready = float(cutover.get("target_ready_seconds", 0.0))
            if ready > thresholds.max_cutover_pause_seconds:
                failures.append(
                    f"target-ready {ready:.3f} > "
                    f"{thresholds.max_cutover_pause_seconds:.3f}"
                )
    return failures
