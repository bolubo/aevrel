from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests.fixtures.toy import make_toy_spec  # noqa: E402


@pytest.fixture()
def toy_spec() -> dict:
    return make_toy_spec()
