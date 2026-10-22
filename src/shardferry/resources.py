from __future__ import annotations

from pathlib import Path


_PACKAGE_ROOT = Path(__file__).resolve().parent
_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """Resolve a schema in both source and wheel installs."""
    relative = Path(*parts)
    candidates = (
        _PACKAGE_ROOT / "spec" / relative,
        _SOURCE_ROOT / "spec" / relative,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def resource_dir(*parts: str) -> Path:
    """Resolve a schema directory in both source and wheel installs."""
    relative = Path(*parts)
    candidates = (
        _PACKAGE_ROOT / "spec" / relative,
        _SOURCE_ROOT / "spec" / relative,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]
