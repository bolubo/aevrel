"""The failure code catalog and the code must describe the same set.

A code that reaches a user without a documented meaning is a bug: the whole
point of this project is that a verdict always comes with a specific reason.
"""

from __future__ import annotations

import re
from pathlib import Path

from aevrel.local.models import ReasonCode
from aevrel.state.resume_state import RESUME_REASON_CODES

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "docs" / "FAILURE-CODES.md"
ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", re.MULTILINE)


def documented_codes() -> set[str]:
    return set(ROW.findall(CATALOG.read_text(encoding="utf-8")))


def _go_constants(relative: str, pattern: str) -> set[str]:
    text = (ROOT / relative).read_text(encoding="utf-8")
    return set(re.findall(pattern, text))


def emitted_codes() -> set[str]:
    codes = {code.value for code in ReasonCode}
    codes |= set(RESUME_REASON_CODES)
    codes |= _go_constants(
        "internal/control/models.go",
        r'Reason[A-Za-z]+\s*=\s*"([A-Z][A-Z0-9_]*)"',
    )
    codes |= _go_constants(
        "internal/control/rescue.go",
        r'Reason[A-Za-z]+\s*=\s*"([A-Z][A-Z0-9_]*)"',
    )
    codes |= _go_constants(
        "internal/controller/agent_scan.go",
        r'addReason\(\s*"([A-Z][A-Z0-9_]*)"',
    )
    codes |= _go_constants(
        "internal/controller/agent_scan.go",
        r'ControlReason\{Code:\s*"([A-Z][A-Z0-9_]*)"',
    )
    codes |= _go_constants(
        "internal/controller/readiness.go",
        r'Code:\s+"([A-Z][A-Z0-9_]*)"',
    )
    return codes


def test_every_emitted_code_is_documented() -> None:
    missing = sorted(emitted_codes() - documented_codes())
    assert missing == [], f"undocumented failure codes: {missing}"


def test_the_catalog_does_not_invent_codes() -> None:
    unknown = sorted(documented_codes() - emitted_codes())
    assert unknown == [], f"documented but never emitted: {unknown}"


def test_the_catalog_lists_each_code_once() -> None:
    rows = ROW.findall(CATALOG.read_text(encoding="utf-8"))
    duplicates = sorted({code for code in rows if rows.count(code) > 1})
    assert duplicates == [], f"codes documented twice: {duplicates}"
