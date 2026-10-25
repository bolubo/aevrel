"""Shared UI theme, branding, and presentation helpers for Aevrel.

Aesthetic: technical / retro minimalism -- "hairline" editorial style.
No cartoonish styling and no figlet block art. Structure comes from light
hairline rules and letter-spaced typography, never from heavy frames or
multi-segment slash art (which is fiddly to keep correct and reads as
clutter, not minimalism). Everything here is data-driven and importable
WITHOUT an active terminal (curses is only touched inside the TUI at draw
time), so the helpers stay unit-testable.

Experimental presentation helpers are marked EXPERIMENTAL and kept isolated
so they can be toggled or removed without affecting the core invariants.
"""
from __future__ import annotations

import os
from typing import Iterable

# --------------------------------------------------------------------------
# Brand wordmark. Style: hairline minimalism / editorial terminal.
# The wordmark is letter-spaced (tracked) uppercase, framed by light
# hairline rules (U+2500). No heavy box-drawing frames and no figlet block
# art -- the lockup stays calm and readable in any monospace terminal.
# --------------------------------------------------------------------------
AEVREL_WORDMARK = "AEVREL"
AEVREL_WORDMARK_SPACED = " ".join(AEVREL_WORDMARK)  # "A E V R E L"
TAGLINE = "checkpoint readiness · recovery · mobility control plane"
HAIRLINE = "─"  # U+2500 light horizontal rule


def _center(text: str, width: int) -> str:
    if len(text) >= width:
        return text
    return " " * ((width - len(text)) // 2) + text


def render_banner(width: int = 64) -> str:
    """Return the AEVREL wordmark lockup framed by hairline rules.

    Layout (top to bottom): a full-width hairline, the letter-spaced
    wordmark, the tagline, and a closing hairline.
    """
    rule = HAIRLINE * width
    return "\n".join(
        [
            rule,
            _center(AEVREL_WORDMARK_SPACED, width),
            _center(TAGLINE, width),
            rule,
        ]
    )


def use_color() -> bool:
    """True unless NO_COLOR is set (https://no-color.org)."""
    return not bool(os.environ.get("NO_COLOR"))


# --------------------------------------------------------------------------
# Semantic palette: role -> curses color_pair index.
# Mirrors the pairs initialized in tui.TuiApp._init_display so the TUI and CLI
# share one vocabulary. Index 0 is the default (no color pair).
# --------------------------------------------------------------------------
ROLE_PAIR = {
    "neutral": 0,
    "accent": 1,   # cyan
    "ok": 2,       # green
    "warn": 3,     # yellow
    "error": 4,     # red
    "mode": 1,      # cyan (scheduler/runtime mode flag)
}

# --------------------------------------------------------------------------
# Unified status vocabulary: one source of truth for state labels, symbols and
# roles, shared by the TUI and CLI so the same concept always looks the same.
# --------------------------------------------------------------------------
STATUS_VOCAB = {
    "READY": {"symbol": "OK", "role": "ok"},
    "UNVERIFIED": {"symbol": "??", "role": "warn"},
    "STALE": {"symbol": "!!", "role": "warn"},
    "INCOMPLETE": {"symbol": "XX", "role": "error"},
    "BLOCKED": {"symbol": "XX", "role": "error"},
    "TARGET_BLOCKED": {"symbol": "XX", "role": "error"},
    "UNKNOWN": {"symbol": "??", "role": "neutral"},
}

LEGEND_ORDER = ["READY", "UNVERIFIED", "STALE", "INCOMPLETE", "BLOCKED", "UNKNOWN"]


def status_role(status: str) -> str:
    return STATUS_VOCAB.get(status, STATUS_VOCAB["UNKNOWN"])["role"]


def status_symbol(status: str) -> str:
    return STATUS_VOCAB.get(status, STATUS_VOCAB["UNKNOWN"])["symbol"]


def status_label(status: str) -> str:
    return f"{status_symbol(status)} {status}"


# --------------------------------------------------------------------------
# CLI presentation helpers (monochrome, ANSI-free, hairline style).
# --------------------------------------------------------------------------
_RULER_CHAR = HAIRLINE


def _rule(width: int) -> str:
    return _RULER_CHAR * max(1, width)


def print_section(title: str, width: int = 64) -> None:
    """Print a hairline section rule with a label.

    Example: ``─  AEVREL · MIGRATION  ──────────────────────────``
    """
    label = f"─  AEVREL · {title}  "
    pad = max(0, width - len(label))
    print(label + _RULER_CHAR * pad)


def print_table(
    headers: list[str],
    rows: Iterable[list[str]],
    *,
    width: int = 72,
) -> None:
    """Print a fixed-width aligned table with a header row (U3)."""
    cols = [str(h) for h in headers]
    body = [[str(c) for c in row] for row in rows]
    n = len(cols)
    if n == 0:
        return
    widths = []
    for i in range(n):
        col_w = len(cols[i])
        for row in body:
            if i < len(row):
                col_w = max(col_w, len(row[i]))
        widths.append(col_w)

    def line_width() -> int:
        return 2 + sum(widths) + 2 * (n - 1)

    # U3: keep every row within `width` columns so nothing wraps or
    # misaligns on an 80-col terminal. Shrink the last column (never below
    # its own header) and mark-truncate any overlong cell.
    if line_width() > width:
        overflow = line_width() - width
        floor = max(len(cols[-1]), 3)
        widths[-1] -= min(overflow, max(0, widths[-1] - floor))

    def cell(value: str, i: int) -> str:
        if len(value) > widths[i]:
            return value[: max(0, widths[i] - 1)] + "…"
        return value.ljust(widths[i])

    def fmt(row: list[str]) -> str:
        return "  " + "  ".join(
            cell(row[i] if i < len(row) else "", i) for i in range(n)
        )

    print(fmt(cols))
    print("  " + "  ".join(_RULER_CHAR * widths[i] for i in range(n)))
    for row in body:
        print(fmt(row))


def print_result_block(
    ok: bool,
    *,
    code: str | None = None,
    next_hint: str | None = None,
) -> None:
    """Print a terminal RESULT block for non-JSON mode (U4)."""
    if ok:
        print("RESULT: OK")
    else:
        label = f" ({code})" if code else ""
        print(f"RESULT: FAILED{label}")
    if next_hint:
        print(f"NEXT: {next_hint}")
