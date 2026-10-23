from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aevrel.lrs.worker import LiveWorker, LiveWorkerError, LiveWorkerServer


def _request(
    *,
    world_size: int = 4,
    max_in_flight: int = 2,
    state_mode: str = "L4",
    ranks: list[int] | None = None,
) -> dict:
    return {
        "apiVersion": "aevrel.dev/v1",
        "kind": "LiveRankSplitReconcile",
        "migration": {
            "namespace": "default",
            "name": "live-demo",
            "uid": "uid-live-demo",
            "generation": 3,
        },
        "spec": {
            "mode": "live-rank-split",
            "source": {"worldSize": world_size},
            "target": {"worldSize": world_size},
            "live": {
                "maxInFlight": max_in_flight,
                "stateMode": state_mode,
                "stateRoot": "/state/step-100/rank{rank}",
                "stateProviders": ["python.random", "numpy.random"],
                "cutover": {
                    "quorum": world_size // 2 + 1,
                    "requireStateComplete": state_mode == "L4",
                },
                "rankPlan": {
                    "ranks": ranks if ranks is not None else list(range(world_size)),
                },
            },
        },
        "previousStatus": {},
    }


def test_live_worker_advances_to_proof(tmp_path: Path) -> None:
    worker = LiveWorker(tmp_path / "worker")
    request = _request()
    phases = []
    observation = {}
    for _ in range(16):
        observation = worker.reconcile(request)
        phases.append(observation["phase"])
        if observation["phase"] == "succeeded":
            break
    assert phases[0] == "preflight"
    assert "following" in phases
    assert phases[-1] == "succeeded"
    assert observation["coverage"] == [0, 1, 2, 3]
    assert observation["inFlight"] == []
    assert observation["stateComplete"] is True
    assert observation["proofRef"].startswith("file://")
    proof = json.loads(
        Path(observation["proofRef"][len("file://") :]).read_text(encoding="utf-8")
    )
    assert proof["backend"] == "cpu-contract"
    assert proof["steps_lost"] == 0
    assert proof["global_stop"] == 0
    assert proof["live_collective"] is False
    assert len(proof["proofs"]) == 4


def test_live_worker_is_durable_across_instances(tmp_path: Path) -> None:
    request = _request()
    first = LiveWorker(tmp_path / "worker")
    assert first.reconcile(request)["phase"] == "preflight"

    second = LiveWorker(tmp_path / "worker")
    resumed = second.reconcile(request)
    assert resumed["phase"] != "preflight"
    assert resumed["coverage"] == [0, 1, 2, 3]


def test_live_worker_rejects_invalid_spec(tmp_path: Path) -> None:
    request = _request()
    request["spec"]["live"]["maxInFlight"] = 4
    with pytest.raises(LiveWorkerError):
        LiveWorker(tmp_path / "worker").reconcile(request)


def test_live_worker_server_authenticates_and_reconciles(tmp_path: Path) -> None:
    server = LiveWorkerServer(
        host="0.0.0.0",
        port=0,
        token="live-secret",
        state_dir=tmp_path / "worker",
    )
    server.start()
    try:
        host, port = server.address
        host = _local_ip()
        body = json.dumps(_request()).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}/v1/live/reconcile",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer live-secret",
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5.0) as response:
            assert response.status == 200
            payload = json.loads(response.read())
        assert payload["phase"] == "preflight"

        unauthorized = urllib.request.Request(
            f"http://{host}:{port}/v1/live/reconcile",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            opener.open(unauthorized, timeout=5.0)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthorized request was accepted")
    finally:
        server.stop()


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()
