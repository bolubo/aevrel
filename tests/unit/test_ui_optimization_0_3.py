from __future__ import annotations

import json
import shutil

import pytest

from aevrel.cli import main
from aevrel.cli import _print_migration_summary, _print_demo_summary
from aevrel.tui import TuiApp, View


class FakeScreen:
    def __init__(self, height, width):
        self.height = height
        self.width = width
        self.cells = {}
        self.refresh_count = 0
        self.erased = 0

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        self.erased += 1
        self.cells = {}

    def refresh(self):
        self.refresh_count += 1

    def addnstr(self, y, x, text, n, attr=0):
        self.cells.setdefault(y, []).append(str(text))


def _rows(screen):
    out = []
    for y in sorted(screen.cells):
        out.extend(screen.cells[y])
    return out


def _view(i):
    return View(
        path=f"/ckpt{i}",
        record={"complete": True, "ranks": []},
        readiness={
            "status": "READY",
            "fresh": True,
            "loadable": True,
            "movable": True,
            "reasons": [],
        },
    )


@pytest.fixture
def curses_app(monkeypatch):
    import curses

    app = TuiApp(work_dir="/tmp/aevrel-tui-test")
    app.curses = curses
    # In a headless test there is no terminal, so color_pair() raises.
    # The real TUI initializes colors via curses.wrapper; here we stub it.
    monkeypatch.setattr(curses, "color_pair", lambda pair: 0)
    return app


# ---- T1: CONTEXT panel must not fake "OK" for unverified capabilities ----
def test_tui_right_panel_no_fake_ok(curses_app, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    screen = FakeScreen(40, 31)
    curses_app._draw_right(screen, 0, 0, 40, 31)
    rows = _rows(screen)
    text = "\n".join(rows)
    for label in (
        "Checkpoint store",
        "Restore drill",
        "Migration",
        "Target load",
        "Proof / audit",
        "Training Job",
    ):
        line = next((r for r in rows if label in r), "")
        assert "OK" not in line, f"{label} shows fake OK: {line!r}"
    assert "STATUS: OK / -- / EXP" not in text
    assert "[-- ] Kubernetes" in text


# ---- T2: small terminal degrades gracefully, no busy redraw ----
def test_tui_too_small_stable(curses_app):
    screen = FakeScreen(20, 70)
    curses_app._draw(screen)
    rows = _rows(screen)
    assert any("terminal too small" in r for r in rows)
    assert screen.refresh_count == 1
    curses_app._draw(screen)
    assert screen.refresh_count == 1  # guard prevents re-draw


def test_tui_compact_renders(curses_app):
    screen = FakeScreen(26, 90)
    curses_app._draw(screen)
    text = "\n".join(_rows(screen))
    assert "CHECKPOINTS" in text


# ---- T3: readiness status legend present ----
def test_tui_readiness_legend(curses_app):
    curses_app.state.views = [_view(0)]
    screen = FakeScreen(30, 100)
    curses_app._draw_center(screen, 2, 1, 26, 90)
    text = "\n".join(_rows(screen))
    assert "LEGEND:" in text
    assert "READY" in text
    assert "UNKNOWN" in text


# ---- T4: checkpoint list scrolls ----
def test_tui_list_scroll(curses_app):
    curses_app.state.views = [_view(i) for i in range(12)]
    curses_app.state.selected = 0
    curses_app.state.scroll_offset = 0
    curses = curses_app.curses
    for _ in range(10):
        curses_app._handle_key(curses.KEY_DOWN)
    assert curses_app.state.selected == 10
    assert curses_app.state.scroll_offset == 10 - 7 + 1
    screen = FakeScreen(30, 100)
    curses_app._draw_center(screen, 2, 1, 26, 90)
    text = "\n".join(_rows(screen))
    assert "ckpt0" not in text
    assert "ckpt10" in text


# ---- C1: unified failure contract ----
def test_readiness_failure_prefix(capsys):
    assert main(["readiness", "--checkpoint", "/does-not-exist-xyz"]) == 2
    assert "aevrel readiness:" in capsys.readouterr().err


def test_restore_drill_failure_prefix(capsys):
    rc = main(
        [
            "restore-drill",
            "--checkpoint",
            "/does-not-exist-xyz",
            "--target-command",
            "echo hi",
            "--rank",
            "0",
            "--steps",
            "1",
            "--target-world",
            "1",
        ]
    )
    assert rc == 2
    assert "aevrel restore-drill:" in capsys.readouterr().err


def test_readiness_json_failure(capsys):
    assert main(["readiness", "--checkpoint", "/does-not-exist-xyz", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "error" in payload


def test_restore_drill_json_failure(capsys):
    assert (
        main(
            [
                "restore-drill",
                "--checkpoint",
                "/does-not-exist-xyz",
                "--target-command",
                "echo hi",
                "--rank",
                "0",
                "--steps",
                "1",
                "--target-world",
                "1",
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "error" in payload


def test_migrate_json_failure(tmp_path, capsys):
    checkpoint = tmp_path / "source.dcp"
    checkpoint.mkdir()
    assert (
        main(
            [
                "migrate",
                "--checkpoint",
                str(checkpoint),
                "--output",
                str(checkpoint / "migrated"),
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "error" in payload


def test_doctor_json_failure(capsys):
    assert main(["doctor", "--spec", "x", "--checkpoint", "y", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "error" in payload


# ---- C2: machine output sorted keys ----
def test_capabilities_json_sorted(capsys):
    assert main(["capabilities", "--json"]) == 0
    out = capsys.readouterr().out
    # sort_keys=True forces alphabetical top-level order; the source
    # insertion order (version, apiVersion, agentVersion, roles, features)
    # would NOT satisfy this, so this guards the change.
    assert out.index('"agentVersion"') < out.index('"apiVersion"')
    assert out.index('"apiVersion"') < out.index('"features"')
    assert out.index('"features"') < out.index('"roles"')
    assert out.index('"roles"') < out.index('"version"')


# ---- C3: aligned summaries ----
def test_summary_alignment(capsys):
    summary = {
        "checkpoint": "/tmp/ckpt",
        "output": "/tmp/out",
        "topology": {"source_world_size": 8, "target_world_size": 3},
        "plan": {"nodes": 2, "bytes": 1024},
        "execution": {"bytes_moved": 512, "duration_seconds": 1.5},
        "verification": {"method": "sha256", "passed": True},
        "work_dir": "/tmp/wd",
    }
    _print_migration_summary(summary)
    out = capsys.readouterr().out
    labels = {"checkpoint", "output", "topology", "plan", "moved", "verification", "work dir"}
    for line in out.splitlines():
        if not line.startswith("  "):
            continue  # skip the "Migration complete" header line
        assert line[16] == " ", line
        assert line[2:16].rstrip() in labels
