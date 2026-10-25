#!/usr/bin/env python3
"""Check public repository hygiene, governance files and local links."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
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
    "docs/INDEX.md",
    "docs/QUICKSTART.md",
    "docs/FIRST-CONTRIBUTION.md",
    "docs/DEVELOPMENT-MAP.md",
    "docs/ARCHITECTURE.md",
    "docs/RFC-GUIDE.md",
    "docs/RFC-TEMPLATE.md",
    "docs/ROADMAP.md",
    "docs/USAGE.md",
    "docs/SUPPORT.md",
    "docs/TARGET-READY.md",
    "docs/TARGET-LOAD.md",
    "scripts/README.md",
    "experiments/README.md",
    "docs/LANGUAGE-BOUNDARY.md",
)

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Text files must not carry the executable bit. Copying from a Windows or
# network filesystem sets it, and a stray 100755 makes the tree misleading.
EXECUTABLE_TEXT_SUFFIXES = (".md", ".py", ".json", ".yml", ".yaml", ".toml")

FFI_DIRECTORIES = ("native/ffi/",)
C_CPP_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")

# The crate produces two artefacts from one build. Both are required: the
# binary drives CDC and CAS, the extension module drives the Python kernels.
NATIVE_RELEASE = Path("native/aevrel-fast/target/release")
NATIVE_ARTEFACTS = (
    "aevrel-fast",
    "libaevrel_fast.so",
    "libaevrel_fast.dylib",
    "aevrel_fast.dll",
)
NATIVE_BUILD_HINT = (
    "cargo build --release --manifest-path native/aevrel-fast/Cargo.toml"
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
    result = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", "*.md"],
        check=True,
        capture_output=True,
    )
    relative_paths = [
        item for item in result.stdout.decode("utf-8").split("\0") if item
    ]
    return [
        REPO / relative
        for relative in relative_paths
        if (REPO / relative).is_file()
    ]


def _check_public_markdown_policy(errors: list[str]) -> None:
    for path in _markdown_files():
        relative = path.relative_to(REPO)
        if path.name.endswith(".zh-CN.md"):
            errors.append(f"public Markdown must be English: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        match = CJK_PATTERN.search(text)
        if match is not None:
            line = text.count("\n", 0, match.start()) + 1
            errors.append(
                f"public Markdown contains Chinese text: {relative}:{line}"
            )


def _check_language_boundary(errors: list[str]) -> None:
    """C/C++ sources may only live inside a designated FFI shim directory.

    Aevrel's own native hot path is Rust. C/C++ appears only as a thin FFI
    boundary when an external C/C++ library must be called, never as the
    implementation language for project kernels.
    """
    result = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    tracked = [
        item for item in result.stdout.decode("utf-8").split("\0") if item
    ]
    for relative in tracked:
        if relative.endswith(C_CPP_EXTENSIONS) and not relative.startswith(
            FFI_DIRECTORIES
        ):
            errors.append(
                "C/C++ source outside the FFI boundary "
                f"({', '.join(FFI_DIRECTORIES)}): {relative}"
            )


def _check_native_binary(errors: list[str]) -> None:
    """The native hot path must be built; Aevrel has no Python fallback.

    Every kernel is expected to run natively, and a missing build is a hard
    failure rather than a silent downgrade. Catching it here keeps that
    failure at gate time, where the message can name the build command,
    instead of at the first data-plane call.
    """
    release = REPO / NATIVE_RELEASE
    built = [name for name in NATIVE_ARTEFACTS if (release / name).is_file()]
    if not built:
        errors.append(
            f"native artefacts are not built in {NATIVE_RELEASE} "
            f"(run: {NATIVE_BUILD_HINT})"
        )
        return
    if not any(name.startswith("aevrel-fast") for name in built):
        errors.append(
            f"native binary is missing from {NATIVE_RELEASE} "
            f"(run: {NATIVE_BUILD_HINT})"
        )
    if not any(not name.startswith("aevrel-fast") for name in built):
        errors.append(
            f"native extension module is missing from {NATIVE_RELEASE} "
            f"(run: {NATIVE_BUILD_HINT})"
        )


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


def _check_file_modes(errors: list[str]) -> None:
    result = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-s"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        meta, _, relative = line.partition("\t")
        fields = meta.split()
        if len(fields) < 3:
            continue
        if (
            fields[0] == "100755"
            and relative.lower().endswith(EXECUTABLE_TEXT_SUFFIXES)
        ):
            errors.append(f"text file has the executable bit: {relative}")

    # Tracked modes only cover files already in the index. New files copied
    # from a Windows or network filesystem carry the executable bit before
    # their first commit, so the guard must also look at the working tree:
    # otherwise the failure is first seen in CI, not locally.
    status = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in status.stdout.splitlines():
        if not line.startswith("??"):
            continue
        relative = line[3:].strip().rstrip("/")
        candidate = REPO / relative
        if (
            candidate.is_file()
            and relative.lower().endswith(EXECUTABLE_TEXT_SUFFIXES)
            and os.access(candidate, os.X_OK)
        ):
            errors.append(
                "untracked text file has the executable bit "
                f"(committing it would fail CI): {relative}"
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
        "ROADMAP.md",
        "aevrel demo --dcp",
        "aevrel-target-load",
        "INDEX.md",
        "FIRST-CONTRIBUTION.md",
        "DEVELOPMENT-MAP.md",
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
    _check_public_markdown_policy(errors)
    _check_file_modes(errors)
    _check_language_boundary(errors)
    _check_native_binary(errors)
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
