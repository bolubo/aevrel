from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from aevrel.agent import build_agent_server
from aevrel.dcp import export_dcp
from aevrel.executor import apply
from aevrel.planning import plan
from tests.fixtures.generated import (
    generate_sharded_spec,
    materialize_source_files,
)


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _source_dcp(tmp_path: Path) -> Path:
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (4,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    output = tmp_path / "source.dcp"
    export_dcp(spec, output)
    return output


def test_agent_exposes_readonly_checkpoint_record(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _source_dcp(tmp_path)
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
        public_url="http://source",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/checkpoint-record",
            headers={"Authorization": "Bearer secret"},
        )
        payload = json.loads(urlopen(request).read())
        assert payload["record"]["complete"] is True
        assert len(payload["record"]["ranks"]) == 4
        assert payload["record"]["provider"] == "dcp"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _checkpoint_record(tmp_path: Path, **kwargs):
    """Serve one agent and read its checkpoint record."""
    checkpoint = _source_dcp(tmp_path)
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
        public_url="http://source",
        **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/checkpoint-record",
            headers={"Authorization": "Bearer secret"},
        )
        return json.loads(urlopen(request).read())
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_checkpoint_record_is_protocol_two_without_closure(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    payload = _checkpoint_record(tmp_path)
    assert payload["version"] == "2"
    # No manifest configured is an absence of evidence, not a bad checkpoint.
    assert payload["resumeState"] is None


def test_checkpoint_record_carries_the_resume_state_summary(
    tmp_path: Path,
) -> None:
    pytest.importorskip("torch")
    manifest = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "resume-state"
        / "complete.json"
    )
    payload = _checkpoint_record(tmp_path, resume_state_path=manifest)
    summary = payload["resumeState"]
    assert summary["closure"] == "COMPLETE"
    assert summary["manifestDigest"].startswith("sha256:")
    assert summary["reasonCodes"] == []
    kinds = {item["kind"] for item in summary["components"]}
    assert {"model", "optimizer", "global_step", "rng", "dataloader"} <= kinds
    for item in summary["components"]:
        if item["required"]:
            assert item["present"] is True


def test_checkpoint_record_surfaces_a_malformed_manifest(
    tmp_path: Path,
) -> None:
    pytest.importorskip("torch")
    broken = tmp_path / "broken-resume.json"
    broken.write_text(json.dumps({"components": []}), encoding="utf-8")
    payload = _checkpoint_record(tmp_path, resume_state_path=broken)
    summary = payload["resumeState"]
    # A manifest that does not parse stays visible instead of reading as
    # "no evidence": the closure is MALFORMED and carries no digest.
    assert summary["closure"] == "MALFORMED"
    assert summary["manifestDigest"] == ""
    assert "RESUME_MANIFEST_INVALID" in summary["reasonCodes"]
