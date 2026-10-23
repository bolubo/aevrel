from __future__ import annotations

import numpy as np

from shardferry.cdp import (
    CheckpointFollower,
    ChunkingPolicy,
    VersionMarker,
    VersionTarget,
    write_marker,
)
from shardferry.planning import plan
from shardferry.target_layout import ApplyFileProvider
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files
from tests.oracle.reference import assert_bitwise_equal, reference_reshard


def test_cdp_provider_uses_real_planner_and_executor(tmp_path) -> None:
    spec, source_arrays, tensor = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (3,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, source_arrays, tmp_path / "work")
    plan_payload = plan(spec)

    source = tmp_path / "versions"
    version_dir = source / "step-100"
    version_dir.mkdir(parents=True)
    write_marker(
        version_dir,
        VersionMarker(
            version="1",
            version_id="step-100",
            job_uid="job-1",
            step=100,
            parent_id=None,
            metadata_hash="sha256:" + "a" * 64,
            created_at="2026-10-22T00:00:00Z",
            complete=True,
        ),
    )
    target = VersionTarget(tmp_path / "target")
    provider = ApplyFileProvider(
        lambda descriptor: (spec, plan_payload),
        work_parent=tmp_path / "provider-work",
    )
    follower = CheckpointFollower(
        source,
        target,
        chunking=ChunkingPolicy.fixed(64),
        manifest_provider=provider,
    )
    follower.sync_once()

    manifest = target.manifests.read("step-100")
    assert manifest.chunks
    assert {chunk.file for chunk in manifest.chunks} == {
        f"w/rank{rank}.bin" for rank in range(3)
    }

    expected = reference_reshard(tensor, (3,), ("Shard(0)",))
    for rank, (_, expected_array) in expected.items():
        actual = np.frombuffer(
            (target.shadow / "step-100" / "w" / f"rank{rank}.bin").read_bytes(),
            dtype=np.float32,
        ).reshape(expected_array.shape)
        assert_bitwise_equal(actual, expected_array)
