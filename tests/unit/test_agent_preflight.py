from pathlib import Path

import pytest

from shardferry.agent import AgentApp, AgentError


def test_target_preflight_accepts_writable_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "targets"
    root = allowed / "job42" / "rank0"
    root.parent.mkdir(parents=True)
    app = AgentApp(
        "secret",
        tmp_path / "state",
        target_root=allowed,
    )

    result = app.preflight(root, required_bytes=1)

    assert result["ok"] is True
    assert result["target_root"] == str(root.resolve())
    assert result["free_bytes"] > 0


def test_target_preflight_rejects_path_outside_allowed_root(
    tmp_path: Path,
) -> None:
    app = AgentApp(
        "secret",
        tmp_path / "state",
        target_root=tmp_path / "targets",
    )

    with pytest.raises(AgentError, match="outside"):
        app.preflight("/etc/shardferry/rank0")
