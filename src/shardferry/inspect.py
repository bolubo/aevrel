from __future__ import annotations

from typing import Any

from .manifest import ManifestError, validate_spec


def inspect_toy(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("version") != "1":
        raise ManifestError("unsupported toy checkpoint version")
    tensors = payload.get("tensors")
    source = payload.get("source")
    target = payload.get("target")
    if not isinstance(tensors, list) or not isinstance(source, dict) or not isinstance(target, dict):
        raise ManifestError("toy checkpoint requires tensors, source, and target")

    spec = {
        "version": "1",
        "checkpoint": {"format": "toy", "tensors": tensors},
        "source": source,
        "target": target,
    }
    validate_spec(spec)
    return spec
