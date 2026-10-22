from __future__ import annotations

import pytest

from shardferry.remote import rewrite_source_uris
from tests.fixtures.generated import generate_sharded_spec


def test_rewrite_source_uris_for_http_agent(tmp_path) -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4), (2, 2), ("Shard(0)", "Shard(1)"), (3, 1), ("Shard(0)", "Replicate()")
    )
    result = rewrite_source_uris(
        spec,
        "http://source.example:9000/",
        target_storage=str(tmp_path / "target"),
    )
    assert all(
        shard["uri"].startswith("http://source.example:9000/shard/")
        for shard in result["source"]["shards"]
    )
    assert result["target"]["storage"]["uri"] == (
        tmp_path / "target"
    ).resolve().as_uri()


def test_rewrite_source_uris_rejects_non_http_base() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4), (2, 2), ("Shard(0)", "Shard(1)"), (3, 1), ("Shard(0)", "Replicate()")
    )
    with pytest.raises(ValueError):
        rewrite_source_uris(spec, "ftp://source")


def test_rewrite_source_uris_uses_rank_map_endpoints() -> None:
    spec, _, _ = generate_sharded_spec(
        (8, 4), (2, 2), ("Shard(0)", "Shard(1)"), (3, 1), ("Shard(0)", "Replicate()")
    )
    spec["source"]["mesh"]["rank_map"] = [
        {"rank": 0, "endpoint": "http://agent0:9000"},
        {"rank": 1, "endpoint": "http://agent1:9000"},
        {"rank": 2, "endpoint": "http://agent2:9000"},
        {"rank": 3, "endpoint": "http://agent3:9000"},
    ]
    result = rewrite_source_uris(spec, "http://fallback:9000")
    assert result["source"]["shards"][0]["uri"].startswith("http://agent0:9000/")
    assert result["source"]["shards"][3]["uri"].startswith("http://agent3:9000/")
