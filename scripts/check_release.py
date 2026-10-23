#!/usr/bin/env python3
"""Pre-release consistency gate for the public distribution."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import yaml


REPO = Path(__file__).resolve().parents[1]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def _check(
    errors: list[str],
    path: Path,
    *,
    expected: str | None = None,
) -> None:
    if not path.is_file():
        errors.append(f"missing required file: {path.relative_to(REPO)}")
    elif expected is not None:
        with path.open("r", encoding="utf-8") as stream:
            if expected not in stream.read():
                errors.append(
                    f"{path.relative_to(REPO)} does not contain {expected!r}"
                )


def check(image_prefix: str) -> dict[str, Any]:
    errors: list[str] = []
    chart = _load_yaml(REPO / "deploy/helm/aevrel/Chart.yaml")
    values = _load_yaml(REPO / "deploy/helm/aevrel/values.yaml")
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = str(
        pyproject.get("project", {}).get("version", "")
    )
    app_version = str(chart.get("appVersion", ""))
    chart_version = str(chart.get("version", ""))
    if not app_version:
        errors.append("Chart.yaml appVersion is empty")
    if not chart_version:
        errors.append("Chart.yaml version is empty")
    if chart_version != app_version:
        errors.append(
            "Chart.yaml version must equal appVersion: "
            f"{chart_version} != {app_version}"
        )
    if project_version != app_version:
        errors.append(
            "pyproject.toml version must equal Chart.yaml appVersion: "
            f"{project_version} != {app_version}"
        )
    init_match = re.search(
        r'^__version__\s*=\s*"([^"]+)"',
        (REPO / "src/aevrel/__init__.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    init_version = init_match.group(1) if init_match else ""
    if init_version != app_version:
        errors.append(
            "src/aevrel/__init__.py version must equal Chart.yaml "
            f"appVersion: {init_version or '<missing>'} != {app_version}"
        )

    images = values.get("image", {})
    for key, artifact in (
        ("controller", "controller"),
        ("gateway", "gateway"),
        ("agent", "agent"),
        ("liveWorker", "live-worker"),
        ("kueueAdapter", "kueue-adapter"),
    ):
        image = str(images.get(key, ""))
        if not image:
            errors.append(f"values.yaml image.{key} is empty")
            continue
        if image.endswith(":latest"):
            errors.append(f"values.yaml image.{key} uses latest")
        expected_prefix = f"{image_prefix}/aevrel-{artifact}:"
        if not image.startswith(expected_prefix):
            errors.append(
                f"image.{key} must start with {expected_prefix!r}: {image}"
            )
        if image.rsplit(":", 1)[-1] != app_version:
            errors.append(
                f"image.{key} tag must equal appVersion {app_version}: {image}"
            )

    scripts = pyproject.get("project", {}).get("scripts", {})
    for command in (
        "aevrel",
        "aevrel-target-load",
        "aevrel-target-report",
        "aevrel-support-bundle",
        "aevrel-live-worker",
    ):
        if command not in scripts:
            errors.append(f"pyproject.toml is missing console script {command}")

    required = [
        "deploy/docker/Dockerfile.controller",
        "deploy/docker/Dockerfile.gateway",
        "deploy/docker/Dockerfile.agent",
        "deploy/docker/Dockerfile.live-worker",
        "deploy/docker/Dockerfile.kueue-adapter",
        "deploy/helm/aevrel/crds/checkpointmigration.yaml",
        "deploy/helm/aevrel/crds/aevrelcluster.yaml",
        "docs/INDEX.zh-CN.md",
        "docs/QUICKSTART.zh-CN.md",
        "docs/PRODUCT-FORM.zh-CN.md",
        "docs/ARCHITECTURE.zh-CN.md",
        "docs/KUEUE-PREEMPTION-GUARD.zh-CN.md",
        "docs/LRS-WORKER.zh-CN.md",
        "docs/TARGET-LOAD.zh-CN.md",
        "docs/TARGET-READY.zh-CN.md",
        "docs/SUPPORT.zh-CN.md",
        "docs/USAGE.zh-CN.md",
        "docs/ROADMAP.zh-CN.md",
        "docs/LIVE-RANK-SPLIT-USAGE.zh-CN.md",
        "docs/RESCUE-DEMO.zh-CN.md",
        "spec/v1/target-ready.schema.json",
        ".github/workflows/release.yml",
    ]
    for relative in required:
        if not (REPO / relative).is_file():
            errors.append(f"missing required file: {relative}")
    release_notes = REPO / f"docs/RELEASE-NOTES-{app_version}.md"
    if not release_notes.is_file():
        errors.append(
            f"missing release notes: {release_notes.relative_to(REPO)}"
        )

    _check(errors, REPO / "README.md", expected="aevrel-target-load")
    _check(errors, REPO / "README.md", expected="aevrel-support-bundle")
    return {
        "status": "ok" if not errors else "failed",
        "chart_version": chart_version,
        "app_version": app_version,
        "image_prefix": image_prefix,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check release files, image tags and user-facing commands."
    )
    parser.add_argument("--image-prefix", default="ghcr.io/bolubo")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    try:
        result = check(args.image_prefix)
    except Exception as exc:
        print(f"release-check: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"release-check {result['status']}: "
            f"chart={result['chart_version']} app={result['app_version']}"
        )
        for error in result["errors"]:
            print(f"- {error}")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
