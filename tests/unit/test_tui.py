from __future__ import annotations

import sys

from aevrel.tui import run_tui


def test_tui_requires_interactive_terminal(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert run_tui(tmp_path / "tui") == 2
    assert "interactive terminal" in capsys.readouterr().err
