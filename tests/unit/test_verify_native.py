"""The native verifier must fail exactly where the old loop failed.

The one-pass kernel is a performance change, not a semantic one, so these tests
pin the things a speed-up could quietly break: which bytes count as verified,
where the first mismatch is reported, and when the kernel must decline the work
and let the storage layer handle a source it cannot read.
"""

from __future__ import annotations

import pytest

from aevrel.dcp import export_dcp, migrate_dcp
from aevrel.executor import apply
from aevrel.planning import plan as build_plan
from aevrel.verification.verify import (
    VerificationError,
    _local_source,
    verify,
)
from tests.fixtures.generated import generate_sharded_spec, materialize_source_files


@pytest.fixture()
def migrated(tmp_path):
    """A real 4 -> 3 migration with its work directory kept for inspection."""
    elements = 4 * 1024 * 1024 // 4
    spec, arrays, _ = generate_sharded_spec(
        (elements,), (4,), ("Shard(0)",), (4,), ("Shard(0)",)
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(build_plan(spec), spec)
    checkpoint = tmp_path / "source.dcp"
    export_dcp(spec, checkpoint)
    migration = migrate_dcp(
        checkpoint,
        output=tmp_path / "target.dcp",
        work_dir=tmp_path / "work",
        target_world_size=3,
        keep_work=True,
    )
    return migration, tmp_path / "work" / "target"


def test_local_source_resolves_a_stored_dcpzip_entry() -> None:
    uri = (
        "dcpzip:///tmp/source.dcp/__0_0.distcp"
        "?name=__0_0%2Fdata%2F0&offset=564&size=4194304&compression=0"
    )
    assert _local_source(uri) == ("/tmp/source.dcp/__0_0.distcp", 564)


def test_local_source_declines_compressed_entries() -> None:
    uri = (
        "dcpzip:///tmp/source.dcp/__0_0.distcp"
        "?name=__0_0%2Fdata%2F0&offset=564&size=4194304&compression=8"
    )
    assert _local_source(uri) is None


def test_local_source_declines_remote_sources() -> None:
    assert _local_source("https://example.invalid/shard.bin") is None


def test_migrated_checkpoint_verifies(migrated) -> None:
    migration, target_root = migrated
    result = verify(migration["spec"], migration["plan"], target_root=target_root)
    assert result["passed"] is True
    assert result["method"] == "bitwise"


def test_a_corrupted_target_byte_fails_verification(migrated) -> None:
    """Flip one byte in the target; the check must catch it and name the offset."""
    migration, target_root = migrated
    victim = sorted(target_root.rglob("*.bin"))[0]
    payload = bytearray(victim.read_bytes())
    position = len(payload) // 2
    payload[position] ^= 0xFF
    victim.write_bytes(bytes(payload))

    with pytest.raises(VerificationError) as caught:
        verify(migration["spec"], migration["plan"], target_root=target_root)
    message = str(caught.value)
    assert "bitwise mismatch" in message
    assert f"byte {position}" in message, message


def test_verification_counts_every_planned_byte(migrated) -> None:
    """A byte count that drifts would make the check look cheaper than it is."""
    migration, target_root = migrated
    plan = migration["plan"]
    result = verify(migration["spec"], plan, target_root=target_root)
    assert result["passed"] is True
    details = result["details"]
    assert str(plan["total_bytes"]) in details, details
