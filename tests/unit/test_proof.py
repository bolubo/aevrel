from __future__ import annotations

from pathlib import Path

import pytest

from aevrel.executor import apply
from aevrel.planning import plan
from aevrel.verification import (
    ProofError,
    build_proof,
    combine_proofs,
    verify_proof,
)
from tests.fixtures.generated import (
    generate_partial_source_spec,
    generate_sharded_spec,
    materialize_source_files,
)


def test_merkle_proof_verifies_target_without_source(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)

    proof = build_proof(spec, plan_payload, target_root)
    result = verify_proof(proof, target_root, plan=plan_payload)
    assert result["passed"] is True
    assert result["method"] == "merkle"
    assert result["source_verified"] is False
    assert result["target_files"] > 0
    broken = dict(proof)
    broken["coverage"] = [dict(item) for item in proof["coverage"]]
    broken["coverage"][0]["target"] = list(broken["coverage"][0]["target"])
    broken["coverage"][0]["target"][2] += 1
    with pytest.raises(ProofError, match="coverage"):
        verify_proof(broken, target_root)


def test_merkle_proof_detects_target_corruption(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)
    proof = build_proof(spec, plan_payload, target_root)

    leaf = proof["target_leaves"][0]
    path = target_root / leaf["file"]
    with path.open("r+b") as stream:
        stream.seek(leaf["offset"])
        original = stream.read(1)
        stream.seek(leaf["offset"])
        stream.write(bytes([original[0] ^ 0xFF]))

    with pytest.raises(ProofError, match="target leaf mismatch"):
        verify_proof(proof, target_root)


def test_merkle_proof_detects_target_size_mismatch(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)
    proof = build_proof(spec, plan_payload, target_root)

    entry = proof["target_files"][0]
    path = target_root / entry["file"]
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ProofError, match="size mismatch"):
        verify_proof(proof, target_root)


def test_merkle_proof_can_verify_source(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)
    proof = build_proof(spec, plan_payload, target_root)

    result = verify_proof(
        proof,
        target_root,
        source=spec["source"]["storage"]["uri"],
    )
    assert result["reduction_verified"] is True

    source_leaf = proof["source_leaves"][0]
    source_path = Path(source_leaf["uri"].removeprefix("file://"))
    with source_path.open("r+b") as stream:
        stream.seek(source_leaf["offset"])
        original = stream.read(1)
        stream.seek(source_leaf["offset"])
        stream.write(bytes([original[0] ^ 0xFF]))
    try:
        with pytest.raises(ProofError, match="source leaf mismatch"):
            verify_proof(
                proof,
                target_root,
                source=spec["source"]["storage"]["uri"],
            )
    finally:
        with source_path.open("r+b") as stream:
            stream.seek(source_leaf["offset"])
            stream.write(original)


def test_merkle_proof_verifies_partial_reduction(tmp_path: Path) -> None:
    spec, arrays, _ = generate_partial_source_spec(
        (4, 2),
        2,
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)
    proof = build_proof(spec, plan_payload, target_root)

    result = verify_proof(
        proof,
        target_root,
        source=spec["source"]["storage"]["uri"],
    )
    assert result["passed"] is True
    assert result["reduction_verified"] is True


def test_rank_proofs_combine_into_full_proof(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)

    full = build_proof(spec, plan_payload, target_root)
    ranks = []
    for rank in range(3):
        proof = build_proof(
            spec,
            plan_payload,
            target_root,
            target_rank=rank,
        )
        assert proof["target_files"]
        assert all(
            f"/rank{rank}.bin" in entry["file"]
            for entry in proof["target_files"]
        )
        ranks.append(proof)

    combined = combine_proofs(ranks)
    assert combined["root"] == full["root"]
    assert combined["plan_hash"] == full["plan_hash"]
    result = verify_proof(combined, target_root, plan=plan_payload)
    assert result["passed"] is True


def test_combine_proofs_rejects_different_plans(tmp_path: Path) -> None:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (3, 1),
        ("Shard(0)", "Replicate()"),
    )
    materialize_source_files(spec, arrays, tmp_path)
    plan_payload = plan(spec)
    target_root = tmp_path / "target"
    apply(plan_payload, spec, target_root=target_root)
    first = build_proof(spec, plan_payload, target_root, target_rank=0)
    changed = dict(plan_payload)
    changed["migration_id"] = "different"
    with pytest.raises(ProofError, match="different plans"):
        combine_proofs([first, {**first, "plan_id": "different"}])
