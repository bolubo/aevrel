from __future__ import annotations

import pytest

from shardferry.manifest import ManifestError, load_spec


def test_load_spec_missing_file_raises() -> None:
    with pytest.raises(ManifestError):
        load_spec("does-not-exist.yaml")
