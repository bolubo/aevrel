from __future__ import annotations

from pathlib import Path

from shardferry.inspect import inspect_toy
from shardferry.manifest import load_payload, validate_spec

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "toy-4to3" / "checkpoint.yaml"


def test_inspect_toy_builds_valid_spec() -> None:
    payload = load_payload(EXAMPLE)
    spec = inspect_toy(payload)
    assert spec["checkpoint"]["format"] == "toy"
    validate_spec(spec)
