#!/usr/bin/env python3
"""Pre-release consistency gate for the public distribution."""

from __future__ import annotations

import argparse
import json
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
    chart = _load_yaml(REPO / "deploy/helm/shardferry/Chart.yaml")
    values = _load_yaml(REPO / "deploy/helm/shardferry/values.yaml")
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    app_version = str(chart.get("appVersion", ""))
    chart_version = str(chart.get("version", ""))
    if not app_version:
        errors.append("Chart.yaml appVersion is empty")
    if not chart_version:
        errors.append("Chart.yaml version is empty")

    images = values.get("image", {})
    for component in ("controller", "gateway", "agent"):
        image = str(images.get(component, ""))
        if not image:
            errors.append(f"values.yaml image.{component} is empty")
            continue
        if image.endswith(":latest"):
            errors.append(f"values.yaml image.{component} uses latest")
        expected_prefix = f"{image_prefix}/shardferry-{component}:"
        if not image.startswith(expected_prefix):
            errors.append(
                f"image.{component} must start with {expected_prefix!r}: {image}"
            )
        if image.rsplit(":", 1)[-1] != app_version:
            errors.append(
                f"image.{component} tag must equal appVersion {app_version}: {image}"
            )

    scripts = pyproject.get("project", {}).get("scripts", {})
    for command in (
        "shardferry",
        "shardferry-target-load",
        "shardferry-support-bundle",
    ):
        if command not in scripts:
            errors.append(f"pyproject.toml is missing console script {command}")

    required = [
        "deploy/docker/Dockerfile.controller",
        "deploy/docker/Dockerfile.gateway",
        "deploy/docker/Dockerfile.agent",
        "deploy/helm/shardferry/crds/checkpointmigration.yaml",
        "deploy/helm/shardferry/crds/shardferrycluster.yaml",
        "docs/SCENARIO.zh-CN.md",
        "docs/TARGET-LOAD.zh-CN.md",
        "docs/SUPPORT.zh-CN.md",
        "docs/ERROR-CATALOG.zh-CN.md",
        "docs/UPGRADE.zh-CN.md",
        "docs/USER-ENABLEMENT.zh-CN.md",
        "docs/RELEASE.zh-CN.md",
        ".github/workflows/release.yml",
    ]
    for relative in required:
        if not (REPO / relative).is_file():
            errors.append(f"missing required file: {relative}")

    _check(errors, REPO / "README.md", expected="shardferry-target-load")
    _check(errors, REPO / "README.md", expected="shardferry-support-bundle")
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
