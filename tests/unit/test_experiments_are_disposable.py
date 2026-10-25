"""Guard: the experiments directory stays disposable.

No CI workflow, test, or repository script may reference a file inside the
experiments directory (its README is the only exempt path, because the
hygiene check requires that README to exist). The moment an experiment
becomes load-bearing it must be promoted -- moved to scripts/, or folded
into src/ or native/ with tests. See experiments/README.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SEARCH_ROOTS = (Path(".github/workflows"), Path("tests"), Path("scripts"))
SCANNED_SUFFIXES = {".py", ".yml", ".yaml"}
# any path under the experiments directory except its README
EXPERIMENT_REFERENCE = re.compile(r"experiments/(?!README\.md)[A-Za-z0-9_.-]+")


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_no_automation_references_experiment_files():
    offenders: list[str] = []
    for relative in _tracked_files():
        path = Path(relative)
        if path.suffix not in SCANNED_SUFFIXES:
            continue
        if not any(path.is_relative_to(root) for root in SEARCH_ROOTS):
            continue
        text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
        if EXPERIMENT_REFERENCE.search(text):
            offenders.append(relative)
    assert offenders == [], (
        "the experiments directory must stay disposable; promote the file "
        "instead of referencing it: " + ", ".join(sorted(offenders))
    )
