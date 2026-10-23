from __future__ import annotations

import json
import http.client
import socket
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aevrel.agent import build_agent_server
from aevrel.agent.state import TaskStore


def _post(base: str, path: str, payload: dict) -> dict:
    request = Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return json.loads(urlopen(request).read())


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _task(tmp_path) -> dict:
    return {
        "version": "1",
        "migration_uid": "m-1",
        "rank": 0,
        "task_hash": "sha256:" + "a" * 64,
        "target_root": str(tmp_path / "target"),
        "spec_ref": "spec.yaml",
        "plan_ref": "plan.json",
        "plan_slice": {"nodes": [], "bytes": 0},
        "state": "created",
    }


def test_agent_health_and_task_lifecycle(tmp_path) -> None:
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path,
        executor=lambda task: None,
        committer=lambda task: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        assert json.loads(urlopen(base + "/v1/healthz").read())["ok"] is True
        task = _post(base, "/v1/tasks", _task(tmp_path))
        task = _post(base, f"/v1/tasks/{task['task_id']}/prepare", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/execute", {})
        task = _post(base, f"/v1/tasks/{task['task_id']}/commit", {})
        assert task["state"] == "committed"
        assert TaskStore(tmp_path).list()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_agent_consumes_json_body_before_keepalive_next_request(tmp_path) -> None:
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "keepalive-state",
        executor=lambda task: None,
        committer=lambda task: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(
            _local_ip(),
            server.server_address[1],
            timeout=5,
        )
        task_payload = _task(tmp_path)
        connection.request(
            "POST",
            "/v1/tasks",
            body=json.dumps(task_payload),
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        task = json.loads(response.read())
        assert response.status == 201
        connection.request(
            "POST",
            f"/v1/tasks/{task['task_id']}/prepare",
            body="{}",
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        prepared = json.loads(response.read())
        assert response.status == 200
        assert prepared["state"] == "prepared"
        connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_agent_rejects_missing_token(tmp_path) -> None:
    server = build_agent_server("0.0.0.0:0", "secret", tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        request = Request(
            base + "/v1/tasks/not-found",
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(request)
        except HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("missing token must be rejected")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_executor_failure_is_retryable(tmp_path) -> None:
    def fail(_task) -> None:
        raise RuntimeError("transient executor failure")

    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path,
        executor=fail,
        committer=lambda _task: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        task = _post(base, "/v1/tasks", _task(tmp_path))
        task = _post(base, f"/v1/tasks/{task['task_id']}/prepare", {})
        try:
            _post(base, f"/v1/tasks/{task['task_id']}/execute", {})
        except HTTPError as exc:
            error = json.loads(exc.read())
            assert exc.code == 409
            assert error["code"] == "TARGET_CONFLICT"
            assert error["retryable"] is True
        else:
            raise AssertionError("executor failure must be returned")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
