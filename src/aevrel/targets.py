from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .storage.file import FileStorage


def target_root_path(
    spec: dict,
    root: str | Path | None = None,
) -> Path:
    if root is not None:
        return Path(root).resolve()
    return FileStorage().path(spec["target"]["storage"]["uri"])


def target_file_path(
    spec: dict,
    tensor_name: str,
    rank: int,
    root: str | Path | None = None,
) -> Path:
    return (
        target_root_path(spec, root)
        / quote(tensor_name, safe="")
        / f"rank{rank}.bin"
    )


def target_file_uri(
    spec: dict,
    tensor_name: str,
    rank: int,
    root: str | Path | None = None,
) -> str:
    return target_file_path(spec, tensor_name, rank, root).as_uri()


def staging_root_path(
    spec: dict,
    plan_id: str,
    root: str | Path | None = None,
) -> Path:
    digest = plan_id.split(":", 1)[-1]
    return target_root_path(spec, root) / ".aevrel" / "staging" / digest


def staged_file_path(
    spec: dict,
    plan_id: str,
    tensor_name: str,
    rank: int,
    root: str | Path | None = None,
) -> Path:
    return (
        staging_root_path(spec, plan_id, root)
        / quote(tensor_name, safe="")
        / f"rank{rank}.bin"
    )


def journal_path(
    spec: dict,
    plan_id: str,
    root: str | Path | None = None,
) -> Path:
    return staging_root_path(spec, plan_id, root) / "journal.json"
