from __future__ import annotations

import json
import socket
import time
import urllib.request

import pytest

from aevrel.lrs import (
    HttpDeltaServer,
    HttpDeltaSink,
    LrsError,
    ShardBlob,
)
from aevrel.lrs.faults import DroppingProxy


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def _start_server(tmp_path):
    server = HttpDeltaServer("0.0.0.0", 0, tmp_path)
    server.start()
    host = _local_ip()
    port = server.address[1]
    return server, f"http://{host}:{port}"


def _blob(step: int, *, shard_id: int = 0) -> ShardBlob:
    return ShardBlob.capture(
        step=step,
        shard_id=shard_id,
        epoch=0,
        theta=(float(step) + 1.0,),
        momentum=(0.0,),
    )


def _ack(url: str, channel: str) -> dict:
    with urllib.request.urlopen(
        f"{url}/v1/channels/{channel}/ack",
        timeout=3,
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def test_http_sink_roundtrip(tmp_path) -> None:
    server, url = _start_server(tmp_path)
    sink = HttpDeltaSink(url, "rank-0", wait_timeout=10.0)
    try:
        blobs = [_blob(step) for step in range(3)]
        for blob in blobs:
            sink.send(blob)
        sink.wait_for(2)

        assert sink.last_acked_step == 2
        assert sink.acked_digest(2) == blobs[2].digest
        payload = _ack(url, "rank-0")
        assert payload["step"] == 2
        assert payload["digest"] == blobs[2].digest
    finally:
        sink.close()
        server.stop()


def test_http_sink_resumes_after_dropped_posts(tmp_path) -> None:
    server, url = _start_server(tmp_path)
    proxy = DroppingProxy(url, drop_posts=3)
    proxy.start()
    proxy_url = f"http://{_local_ip()}:{proxy.address[1]}"
    sink = HttpDeltaSink(proxy_url, "rank-drop", wait_timeout=15.0)
    try:
        blobs = [_blob(step) for step in range(5)]
        for blob in blobs:
            sink.send(blob)
        sink.wait_for(4)

        assert sink.last_acked_step == 4
        assert sink.acked_digest(4) == blobs[4].digest
        assert proxy.dropped == 3
        payload = _ack(url, "rank-drop")
        assert payload["step"] == 4
    finally:
        sink.close()
        proxy.stop()
        server.stop()


def test_http_sink_survives_server_restart(tmp_path) -> None:
    server = HttpDeltaServer("0.0.0.0", 0, tmp_path)
    server.start()
    host = _local_ip()
    port = server.address[1]
    url = f"http://{host}:{port}"
    sink = HttpDeltaSink(url, "rank-restart", wait_timeout=15.0)
    replacement = None
    try:
        sink.send(_blob(0))
        sink.wait_for(0)
        server.stop()

        sink.send(_blob(1))
        time.sleep(0.1)
        replacement = HttpDeltaServer("0.0.0.0", port, tmp_path)
        replacement.start()

        sink.wait_for(1)
        assert sink.last_acked_step == 1
        assert _ack(url, "rank-restart")["step"] == 1
    finally:
        sink.close()
        server.stop()
        if replacement is not None:
            replacement.stop()


def test_http_sink_duplicate_step_is_idempotent(tmp_path) -> None:
    server, url = _start_server(tmp_path)
    blob = _blob(0)
    first = HttpDeltaSink(url, "rank-dup", wait_timeout=10.0)
    second = HttpDeltaSink(url, "rank-dup", wait_timeout=10.0)
    try:
        first.send(blob)
        first.wait_for(0)
        second.send(blob)
        second.wait_for(0)
        assert second.last_acked_step == 0
        assert second.acked_digest(0) == blob.digest
        assert _ack(url, "rank-dup")["digest"] == blob.digest
    finally:
        first.close()
        second.close()
        server.stop()


def test_http_sink_wait_timeout() -> None:
    sink = HttpDeltaSink(
        f"http://{_local_ip()}:1",
        "rank-dead",
        timeout=0.2,
        retry_delay=0.05,
        wait_timeout=0.3,
    )
    try:
        sink.send(_blob(0))
        with pytest.raises(LrsError):
            sink.wait_for(0)
    finally:
        sink.close()
