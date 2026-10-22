from __future__ import annotations

import json
import sys
from pathlib import Path

from shardferry.cli import main


FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "drill_command.py"
)


def test_restore_drill_requires_real_target_command(tmp_path, capsys) -> None:
    checkpoint = tmp_path / "source.dcp"
    checkpoint.mkdir()
    report = tmp_path / "drill.json"
    result = main(
        [
            "restore-drill",
            "--checkpoint",
            str(checkpoint),
            "--target-command",
            f"{sys.executable} {FIXTURE}",
            "--steps",
            "3",
            "--report",
            str(report),
            "--json",
        ]
    )
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["scope"] == "rank-local"
    assert payload["proof"]["stepsRan"] == 3
    assert report.is_file()
