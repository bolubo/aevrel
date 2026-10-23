#!/usr/bin/env python3
"""Check public repository hygiene, governance files and local links."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml


REPO = Path(__file__).resolve().parents[1]

REQUIRED_FILES = (
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "MAINTAINERS.md",
    "CONTRIBUTOR_LADDER.md",
    "TRIAGE.md",
    "SUPPORT.md",
    "SECURITY.md",
    "RELEASING.md",
    ".editorconfig",
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/question.yml",
    ".github/ISSUE_TEMPLATE/integration.yml",
    ".github/ISSUE_TEMPLATE/good_first_issue.yml",
    ".github/ISSUE_TEMPLATE/design_rfc.yml",
    ".github/ISSUE_TEMPLATE/task.yml",
    ".github/ISSUE_TEMPLATE/refactor.yml",
    ".github/DISCUSSION_TEMPLATE/design.yml",
    ".github/DISCUSSION_TEMPLATE/ideas.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    ".github/dependabot.yml",
    "docs/INDEX.zh-CN.md",
    "docs/QUICKSTART.zh-CN.md",
    "docs/PRODUCT-FORM.zh-CN.md",
    "docs/ARCHITECTURE.zh-CN.md",
    "docs/CONSTRUCTION.zh-CN.md",
    "docs/RFC-GUIDE.zh-CN.md",
    "docs/RFC-TEMPLATE.zh-CN.md",
    "docs/ROADMAP.zh-CN.md",
    "docs/USAGE.zh-CN.md",
    "docs/SUPPORT.zh-CN.md",
)

MARKDOWN_ROOTS = (
    REPO / "README.md",
    REPO / "CONTRIBUTING.md",
    REPO / "CODE_OF_CONDUCT.md",
    REPO / "GOVERNANCE.md",
    REPO / "MAINTAINERS.md",
    REPO / "CONTRIBUTOR_LADDER.md",
    REPO / "TRIAGE.md",
    REPO / "SUPPORT.md",
    REPO / "SECURITY.md",
    REPO / "RELEASING.md",
    REPO / "CHANGELOG.md",
)


def _check_required(errors: list[str]) -> None:
    for relative in REQUIRED_FILES:
        if not (REPO / relative).is_file():
            errors.append(f"missing required file: {relative}")


def _check_forbidden(errors: list[str]) -> None:
    for relative in (
        "WORKLOG.md",
        "spec/charter.md",
        "spec/verification.md",
    ):
        if (REPO / relative).exists():
            errors.append(f"internal document is public: {relative}")


def _markdown_files() -> list[Path]:
    files = list(MARKDOWN_ROOTS)
    files.extend(sorted((REPO / "docs").glob("*.md")))
    files.extend(sorted((REPO / "examples").rglob("*.md")))
    return [path for path in files if path.is_file()]


def _check_links(errors: list[str]) -> None:
    pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            target = match.group(1).split("#", 1)[0].strip()
            if (
                not target
                or target.startswith(("http://", "https://", "mailto:"))
                or target.startswith("/")
            ):
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(
                    f"broken local link in {path.relative_to(REPO)}: {target}"
                )


def _check_yaml(errors: list[str]) -> None:
    paths = [
        *sorted((REPO / ".github/ISSUE_TEMPLATE").glob("*.yml")),
        *sorted((REPO / ".github/DISCUSSION_TEMPLATE").glob("*.yml")),
        *sorted((REPO / ".github/workflows").glob("*.yml")),
    ]
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as stream:
                yaml.safe_load(stream)
        except Exception as exc:
            errors.append(f"invalid YAML {path.relative_to(REPO)}: {exc}")


def _check_governance_content(errors: list[str]) -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for required in (
        "CONTRIBUTING.md",
        "GOVERNANCE.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CONSTRUCTION.zh-CN.md",
        "ROADMAP.zh-CN.md",
        "aevrel demo --dcp",
        "aevrel-target-load",
    ):
        if required not in readme:
            errors.append(f"README.md is missing {required!r}")

    owners = (REPO / ".github/CODEOWNERS").read_text(encoding="utf-8")
    if "@bolubo" not in owners:
        errors.append(".github/CODEOWNERS must name the current owner")

    issue_config = yaml.safe_load(
        (REPO / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")
    )
    if not isinstance(issue_config, dict):
        errors.append("issue template config must be a mapping")
    elif issue_config.get("blank_issues_enabled") is not False:
        errors.append("blank issues must be disabled")


def check() -> dict[str, Any]:
    errors: list[str] = []
    _check_required(errors)
    _check_forbidden(errors)
    _check_links(errors)
    _check_yaml(errors)
    _check_governance_content(errors)
    return {
        "status": "ok" if not errors else "failed",
        "errors": errors,
        "markdown_files": len(_markdown_files()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check repository governance and public documentation hygiene."
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    result = check()
    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"repo-check {result['status']}")
        for error in result["errors"]:
            print(f"- {error}")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
