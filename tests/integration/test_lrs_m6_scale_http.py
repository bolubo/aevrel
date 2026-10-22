from __future__ import annotations

import json
import socket

from shardferry.lrs import HttpDeltaServer, run_m3_scenario
from shardferry.lrs.faults import DroppingProxy


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def test_m6_w12_http_scale_with_drops(tmp_path) -> None:
    host = _local_ip()
    server = HttpDeltaServer("0.0.0.0", 0, tmp_path)
    server.start()
    server_port = server.address[1]
    proxy = DroppingProxy(
        f"http://{host}:{server_port}",
        drop_posts=3,
    )
    proxy.start()
    proxy_url = f"http://{host}:{proxy.address[1]}"
    try:
        result = run_m3_scenario(
            server_url=proxy_url,
            world_size=12,
            steps=24,
            begin_step=4,
            cutover_step=12,
            in_flight=4,
            max_lag=2,
        )

        assert result["ok"] is True, result["checks"]
        assert result["world_size"] == 12
        assert proxy.dropped == 3
        assert result["lag_history_max"] <= 2
        assert (
            result["theta_digest_baseline"]
            == result["theta_digest_migrated"]
        )
        assert (
            result["momentum_digest_baseline"]
            == result["momentum_digest_migrated"]
        )
        assert result["cluster_layout"] == {
            "0": "B",
            "1": "B",
            "2": "B",
            "3": "B",
            "4": "A",
            "5": "A",
            "6": "A",
            "7": "A",
            "8": "A",
            "9": "A",
            "10": "A",
            "11": "A",
        }
        assert [
            proof["in_flight_at_cutover"]
            for proof in result["proofs"]
        ] == [4, 3, 2, 1]
        for proof in result["proofs"]:
            assert (
                proof["live_cutover_digest"]
                == proof["target_cutover_digest"]
            )

        for rank, info in result["sinks"].items():
            channel = info["channel"]
            latest = json.loads(
                (tmp_path / channel / "latest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert latest["step"] == 12
            proof = next(
                item
                for item in result["proofs"]
                if item["rank"] == int(rank)
            )
            assert latest["digest"] == proof["target_cutover_digest"]
    finally:
        proxy.stop()
        server.stop()
