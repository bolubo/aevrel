from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from shardferry.manifest import ManifestError, load_spec, validate_spec

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "toy-4to3" / "spec.yaml"


def test_load_example_spec() -> None:
    spec = load_spec(EXAMPLE)
    assert spec["checkpoint"]["format"] == "toy"
    assert spec["source"]["mesh"]["world_size"] == 4


def test_world_size_mismatch_is_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["mesh"]["world_size"] = 5
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_shard_out_of_bounds_is_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["shards"][0]["size"][0] = 9
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_empty_source_shards_are_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["shards"] = []
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_duplicate_tensor_names_are_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["checkpoint"]["tensors"].append(deepcopy(spec["checkpoint"]["tensors"][0]))
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_unknown_tensor_in_source_shard_is_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["shards"][0]["tensor"] = "missing"
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_source_rank_out_of_range_is_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["shards"][0]["rank"] = 4
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_multiple_source_chunks_per_rank_are_allowed() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["shards"][1]["rank"] = spec["source"]["shards"][0]["rank"]
    spec["source"]["shards"][1]["tensor"] = spec["source"]["shards"][0]["tensor"]
    validate_spec(spec)


def test_shard_rank_must_match_tensor_rank() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["shards"][0]["offset"] = [0]
    spec["source"]["shards"][0]["size"] = [1]
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_shard_dim_must_fit_tensor_rank() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["mesh"]["placements"] = ["Shard(2)", "Shard(1)"]
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_duplicate_rank_map_entry_is_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["source"]["mesh"]["rank_map"] = [
        {"rank": 0, "endpoint": "http://a:9000"},
        {"rank": 0, "endpoint": "http://b:9000"},
    ]
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_rank_map_rank_out_of_range_is_rejected() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["target"]["mesh"]["rank_map"] = [
        {"rank": 3, "endpoint": "http://c:9000"},
    ]
    with pytest.raises(ManifestError):
        validate_spec(spec)


def test_schema_rejects_extra_fields() -> None:
    spec = deepcopy(load_spec(EXAMPLE))
    spec["unexpected"] = True
    with pytest.raises(ManifestError):
        validate_spec(spec)
