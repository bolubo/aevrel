"""Machine acceptance for the UI styling & UX pass (U1-U14).

Style: hairline minimalism -- light U+2500 rules, a letter-spaced AEVREL
wordmark, monochrome + one accent, structure via rules not heavy frames.
Covers the shared theme helpers, the CLI presentation, and the TUI. Uses a
headless FakeScreen + a stubbed curses.color_pair (as in the 0.3 suite).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aevrel.cli import main
from aevrel.theme import (
    AEVREL_WORDMARK_SPACED,
    HAIRLINE,
    ROLE_PAIR,
    render_banner,
    print_section,
    print_table,
    print_result_block,
    status_label,
    status_role,
    status_symbol,
    use_color,
)
from aevrel.tui import TuiApp, View


class FakeScreen:
    def __init__(self, height, width):
        self.height = height
        self.width = width
        self.cells = {}
        self.attrs = []

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        self.cells = {}

    def refresh(self):
        pass

    def addnstr(self, y, x, text, n, attr=0):
        self.cells.setdefault(y, []).append(str(text))
        self.attrs.append(attr)


def _rows(screen):
    out = []
    for y in sorted(screen.cells):
        out.extend(screen.cells[y])
    return out


def _text(screen):
    return "\n".join(_rows(screen))


def _mkview(name, status, *, complete=True):
    return View(
        path=f"/data/{name}",
        record={"complete": complete, "ranks": []},
        readiness={
            "status": status,
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
    monkeypatch.setattr(curses, "color_pair", lambda pair: 0)
    return app


# ---------------------------------------------------------------- U1 banner
def test_render_banner_is_hairline_lockup():
    lines = render_banner().splitlines()
    assert len(lines) == 4
    assert set(lines[0]) == {HAIRLINE}
    assert set(lines[3]) == {HAIRLINE}
    assert lines[1].strip() == AEVREL_WORDMARK_SPACED == "A E V R E L"
    assert "checkpoint readiness" in lines[2]
    # centered (leading padding on both text rows)
    assert lines[1].startswith(" ")
    assert lines[2].startswith(" ")


def test_render_banner_deterministic():
    assert render_banner() == render_banner()


def test_cli_version_shows_wordmark(capsys):
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "A E V R E L" in out
    assert HAIRLINE in out


def test_tui_header_shows_wordmark(curses_app):
    screen = FakeScreen(30, 100)
    curses_app._draw_header(screen, 100)
    text = _text(screen)
    assert "A E V R E L" in text
    assert HAIRLINE in text


# ---------------------------------------------------------- U2 section rules
def test_print_section_is_hairline(capsys):
    print_section("MIGRATION")
    out = capsys.readouterr().out
    assert out.startswith(HAIRLINE)
    assert "AEVREL · MIGRATION" in out


def test_doctor_nonjson_has_section_and_result(capsys):
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert "AEVREL · DOCTOR" in out
    assert "RESULT:" in out
    if rc == 0:
        assert "RESULT: OK" in out


# ------------------------------------------------------------- U3 alignment
def test_print_table_header_and_alignment(capsys):
    print_table(["CHECK", "NAME"], [["OK", "alpha"], ["FAIL", "zulu"]])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:2] == ["CHECK", "NAME"]
    assert HAIRLINE in lines[1]  # hairline separator under header
    alpha_col = lines[2].index("alpha")
    zulu_col = lines[3].index("zulu")
    assert alpha_col == zulu_col  # columns aligned


def test_print_table_caps_width(capsys):
    print_table(["CHECK", "DETAIL"], [["OK", "x" * 200]], width=40)
    for line in capsys.readouterr().out.splitlines():
        assert len(line) <= 40  # never wraps at the cap


# ------------------------------------------------------------ U4 result block
def test_result_block_ok_and_failed(capsys):
    print_result_block(True, next_hint="aevrel demo")
    out = capsys.readouterr().out
    assert "RESULT: OK" in out
    assert "NEXT: aevrel demo" in out

    print_result_block(False, code="verify")
    assert "RESULT: FAILED (verify)" in capsys.readouterr().out


def test_migrate_failure_never_shows_ok(tmp_path, capsys):
    checkpoint = tmp_path / "source.dcp"
    checkpoint.mkdir()
    rc = main(
        [
            "migrate",
            "--checkpoint",
            str(checkpoint),
            "--output",
            str(checkpoint / "migrated"),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "RESULT: FAILED" in captured.err
    assert "RESULT: OK" not in captured.out


def test_readiness_failure_shows_result_failed(capsys):
    rc = main(["readiness", "--checkpoint", "/does-not-exist-xyz"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "aevrel readiness:" in captured.err
    # every command ends with an explicit RESULT line (U4), never a bare error
    assert "RESULT: FAILED" in captured.err
    assert "RESULT: OK" not in captured.out


# -------------------------------------------------------------- U5 palette
def test_status_roles_map_to_palette():
    assert status_role("READY") == "ok"
    assert status_role("UNVERIFIED") == "warn"
    assert status_role("INCOMPLETE") == "error"
    for role in ("ok", "warn", "error"):
        assert role in ROLE_PAIR


def test_tui_source_has_no_bare_palette_numbers():
    import re

    import aevrel.tui as tui_module

    source = Path(tui_module.__file__).read_text(encoding="utf-8")
    assert "self._attr(" in source
    # every call site must name a role, never a raw curses pair number
    leftovers = re.findall(r"self\._attr\(\d", source)
    assert leftovers == [], f"bare palette numbers remain: {leftovers}"


# -------------------------------------------------------------- U6 no-color
def test_no_color_makes_tui_monochrome(curses_app, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_color() is False
    # a color pair collapses to no color bits; only weight/reverse remain
    assert curses_app._attr("ok", bold=True) == curses_app.curses.A_BOLD
    # status stays distinguishable without color
    assert curses_app._status_attr("READY") == curses_app.curses.A_BOLD
    assert curses_app._status_attr("INCOMPLETE") == curses_app.curses.A_REVERSE


def test_cli_no_color_flag_accepted(capsys):
    assert main(["--version", "--no-color"]) == 0
    assert "A E V R E L" in capsys.readouterr().out


# ------------------------------------------------------- U7 unified status
def test_status_vocab_symbols_and_roles():
    assert status_label("READY") == "OK READY"
    assert status_label("UNVERIFIED") == "?? UNVERIFIED"
    assert status_label("STALE") == "!! STALE"
    assert status_label("INCOMPLETE") == "XX INCOMPLETE"
    # unverified/stale/incomplete must never render as OK
    for status in ("UNVERIFIED", "STALE", "INCOMPLETE", "UNKNOWN"):
        assert status_symbol(status) != "OK"


def test_tui_center_uses_status_symbols(curses_app):
    curses_app.state.views = [
        _mkview("a.dcp", "READY"),
        _mkview("b.dcp", "UNVERIFIED"),
    ]
    screen = FakeScreen(30, 100)
    curses_app._draw_center(screen, 2, 1, 26, 90)
    text = _text(screen)
    assert "OK READY" in text
    assert "?? UNVERIFIED" in text
    assert "NAME" in text and "STATE" in text


# ------------------------------------------------------------- U8 box layout
def test_box_uses_hairline_glyphs(curses_app):
    screen = FakeScreen(10, 24)
    curses_app._box(screen, 0, 0, 6, 24, "PANEL")
    text = _text(screen)
    assert "┌" in text and "┐" in text and "└" in text and "│" in text
    assert "+" not in text  # old ascii box gone


def test_tui_draws_across_sizes(curses_app):
    for h, w in [(30, 100), (36, 120), (26, 90)]:
        curses_app._draw(FakeScreen(h, w))  # must not raise
    small = FakeScreen(20, 70)
    curses_app._draw(small)
    assert any("terminal too small" in r for r in _rows(small))


# --------------------------------------------------------- U9 dashboard table
def test_center_dashboard_table_and_selection(curses_app):
    curses_app.state.views = [_mkview(f"c{i}.dcp", "READY") for i in range(4)]
    curses_app.state.selected = 1
    screen = FakeScreen(30, 100)
    curses_app._draw_center(screen, 2, 1, 26, 90)
    text = _text(screen)
    assert "NAME" in text and "STATE" in text
    assert "> c1.dcp" in text  # selected row marker


# ----------------------------------------------------------- U10 keybindings
def test_q_quits(curses_app):
    assert curses_app._handle_key("q") is True
    assert curses_app._handle_key("Q") is True


def test_unknown_key_is_safe(curses_app):
    assert curses_app._handle_key("z") is False
    assert curses_app._handle_key("\x00") is False


def test_arrow_navigation(curses_app):
    curses_app.state.views = [_mkview(f"c{i}", "READY") for i in range(5)]
    curses_app.state.selected = 0
    curses_app._handle_key(curses_app.curses.KEY_DOWN)
    assert curses_app.state.selected == 1
    curses_app._handle_key(curses_app.curses.KEY_UP)
    assert curses_app.state.selected == 0


def test_j_k_navigation(curses_app):
    curses_app.state.views = [_mkview(f"c{i}", "READY") for i in range(3)]
    curses_app.state.selected = 0
    curses_app._handle_key("j")
    assert curses_app.state.selected == 1
    curses_app._handle_key("j")
    assert curses_app.state.selected == 2
    curses_app._handle_key("k")
    assert curses_app.state.selected == 1


def test_drill_rank_edit_moved_off_k(curses_app, monkeypatch):
    # k is now vi-up, so the drill-rank prompt lives on y instead.
    monkeypatch.setattr(curses_app, "_prompt", lambda label, current: "7")
    curses_app._handle_key("y")
    assert curses_app.state.drill_rank == "7"


def test_tab_cycles_panel_focus(curses_app):
    assert curses_app.state.focus == "plane"  # dashboard is focused at start
    curses_app._handle_key("\t")
    assert curses_app.state.focus == "context"
    curses_app._handle_key("\t")
    assert curses_app.state.focus == "transfer"
    curses_app._handle_key("\t")
    assert curses_app.state.focus == "plane"


def test_focused_panel_gets_accent_border(curses_app, monkeypatch):
    import curses

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair)
    screen = FakeScreen(30, 100)
    curses_app.state.focus = "transfer"
    curses_app._draw_left(screen, 2, 0, 26, 32)
    # first draw call in _draw_left is the box top border; the accent pair
    # bit must be set (A_BOLD may also ride along for the monochrome cue)
    assert screen.attrs[0] & ROLE_PAIR["accent"]
    screen2 = FakeScreen(30, 100)
    curses_app.state.focus = "plane"
    curses_app._draw_left(screen2, 2, 0, 26, 32)
    assert not (screen2.attrs[0] & ROLE_PAIR["accent"])


def test_enter_opens_detail_overlay(curses_app):
    curses_app.state.views = [_mkview("a.dcp", "READY")]
    curses_app._handle_key("\n")
    assert curses_app.state.detail_open is True
    screen = FakeScreen(30, 100)
    curses_app._draw(screen)
    text = _text(screen)
    assert "← / Esc  back" in text  # overlay hint, not the footer bar
    curses_app._handle_key(27)
    assert curses_app.state.detail_open is False


def test_arrow_right_opens_detail(curses_app):
    curses_app.state.views = [_mkview("a.dcp", "READY")]
    curses_app._handle_key(curses_app.curses.KEY_RIGHT)
    assert curses_app.state.detail_open is True
    curses_app._handle_key(curses_app.curses.KEY_LEFT)
    assert curses_app.state.detail_open is False


def test_detail_overlay_renders_selected(curses_app):
    curses_app.state.views = [_mkview("a.dcp", "UNVERIFIED")]
    screen = FakeScreen(30, 100)
    curses_app._draw_detail(screen, 30, 100)
    text = _text(screen)
    assert "PATH" in text and "/data/a.dcp" in text
    assert "?? UNVERIFIED" in text
    assert "LOADABLE" in text


def test_detail_blocks_actions_but_stacks_help(curses_app):
    curses_app.state.views = [_mkview("a.dcp", "READY")]
    curses_app._handle_key("\n")  # open detail
    curses_app._handle_key("v")  # mode toggle must be ignored
    assert curses_app.state.mode == "demo"
    curses_app._handle_key("\t")  # focus cycling must be ignored too
    assert curses_app.state.focus == "plane"
    # ? stays reachable from every layer (U14): help stacks on detail
    curses_app._handle_key("?")
    assert curses_app.state.show_help is True
    assert curses_app.state.detail_open is True
    curses_app._handle_key(27)  # Esc closes only the topmost layer
    assert curses_app.state.show_help is False
    assert curses_app.state.detail_open is True
    curses_app._handle_key(27)  # Esc again closes the detail overlay
    assert curses_app.state.detail_open is False


def test_enter_without_selection_is_safe(curses_app):
    curses_app.state.views = []
    assert curses_app._handle_key("\n") is False
    assert curses_app.state.detail_open is False


# ------------------------------------------------------------- U11 help
def test_help_overlay_toggles_and_renders(curses_app):
    assert curses_app._handle_key("?") is False
    assert curses_app.state.show_help is True
    screen = FakeScreen(38, 100)
    curses_app._draw(screen)
    text = _text(screen)
    assert "HELP" in text
    assert "KEYS" in text
    assert "quit" in text
    assert curses_app._handle_key("?") is False
    assert curses_app.state.show_help is False


def test_help_esc_closes(curses_app):
    curses_app._handle_key("?")
    assert curses_app.state.show_help is True
    curses_app._handle_key(27)
    assert curses_app.state.show_help is False


def test_help_blocks_actions(curses_app):
    curses_app._handle_key("?")  # open
    # action keys are ignored while help is open
    curses_app._handle_key("v")
    assert curses_app.state.mode == "demo"  # unchanged


# ------------------------------------------------------------- U12 filter
def test_filter_narrows_and_confirms(curses_app):
    curses_app.state.views = [
        _mkview("alpha.dcp", "READY"),
        _mkview("beta.dcp", "READY"),
    ]
    curses_app._handle_key("/")
    assert curses_app.state.filter_active is True
    curses_app._handle_filter_key("a")
    curses_app._handle_filter_key("l")
    assert curses_app.state.filter_text == "al"
    visible = [v.path for _, v in curses_app._visible_views()]
    assert visible == ["/data/alpha.dcp"]
    curses_app._handle_filter_key("\n")  # confirm
    assert curses_app.state.filter_active is False
    assert curses_app.state.filter_text == "al"


def test_filter_no_matches_state(curses_app):
    curses_app.state.views = [_mkview("alpha.dcp", "READY")]
    curses_app.state.filter_text = "zzz"
    screen = FakeScreen(30, 100)
    curses_app._draw_center(screen, 2, 1, 26, 90)
    assert "NO MATCHES" in _text(screen)


def test_filter_esc_clears(curses_app):
    curses_app.state.filter_active = True
    curses_app.state.filter_text = "abc"
    curses_app._handle_key(27)
    assert curses_app.state.filter_active is False
    assert curses_app.state.filter_text == ""


# ------------------------------------------------- U13 empty/loading/error
def test_empty_state(curses_app):
    curses_app.state.views = []
    screen = FakeScreen(30, 100)
    curses_app._draw_center(screen, 2, 1, 26, 90)
    assert "NO CHECKPOINTS" in _text(screen)


def test_loading_state(curses_app):
    curses_app.state.running = True
    curses_app.state.progress = 42
    curses_app.state.task = "migrate"
    curses_app.state.task_message = "transferring"
    screen = FakeScreen(30, 100)
    curses_app._draw_footer(screen, 24, 100)
    text = _text(screen)
    assert "[" in text and "42" in text
    assert "MIGRATE" in text


def test_error_state_never_ok(curses_app):
    curses_app.state.error = "boom"
    screen = FakeScreen(30, 100)
    curses_app._draw_footer(screen, 24, 100)
    text = _text(screen)
    assert "ERROR" in text
    assert "READY" not in text
    assert "RESULT: OK" not in text


# ------------------------------------------------------------ U14 consistency
def test_footer_lists_core_keys(curses_app):
    screen = FakeScreen(30, 100)
    curses_app._draw_footer(screen, 24, 100)
    text = _text(screen)
    for key in ("/ filter", "? help", "q quit"):
        assert key in text


def test_footer_keys_are_contextual(curses_app):
    def bar():
        screen = FakeScreen(30, 100)
        curses_app._draw_footer(screen, 24, 100)
        return _text(screen)

    assert "Enter detail" in bar()
    curses_app.state.filter_active = True
    assert "type to filter" in bar()
    curses_app.state.filter_active = False
    curses_app.state.detail_open = True
    assert "←/Esc back" in bar()
    curses_app.state.detail_open = False
    curses_app.state.show_help = True
    assert "close help" in bar()
    # help stacked on detail still advertises closing help (topmost layer)
    curses_app.state.detail_open = True
    text = bar()
    assert "close help" in text
    assert "back" not in text


def test_overlays_do_not_mutate_state(curses_app):
    curses_app.state.views = [_mkview("a.dcp", "READY")]
    before = [v.path for v in curses_app.state.views]
    curses_app._handle_key("?")  # open help
    curses_app._handle_key(27)  # esc back
    curses_app._handle_key("\n")  # open detail
    curses_app._handle_key(27)  # esc back
    curses_app._handle_key("/")  # start filter
    curses_app._handle_key("a")  # type
    curses_app._handle_key("\n")  # confirm
    curses_app._handle_key("\t")  # cycle focus
    assert [v.path for v in curses_app.state.views] == before
    assert curses_app.state.selected == 0
    assert curses_app.state.detail_open is False
    assert curses_app.state.show_help is False


def test_help_overlay_mentions_new_keys(curses_app):
    curses_app._handle_key("?")
    screen = FakeScreen(38, 100)
    curses_app._draw(screen)
    text = _text(screen)
    for token in ("Tab", "Enter", "j / k", "→"):
        assert token in text
