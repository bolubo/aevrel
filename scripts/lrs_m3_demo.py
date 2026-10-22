#!/usr/bin/env python3
"""LRS M3 demo: HTTP delta stream with a dropped-connection proxy."""

from __future__ import annotations

import json
import socket
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shardferry.lrs import HttpDeltaServer, run_m3_scenario  # noqa: E402
from shardferry.lrs.faults import DroppingProxy  # noqa: E402


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def main() -> int:
    host = _local_ip()
    evidence_dir = ROOT / "evidence" / "lrs" / "2026-10-22-m3"
    server = HttpDeltaServer(
        "0.0.0.0",
        0,
        evidence_dir / "server-state",
    )
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
            steps=24,
            begin_step=4,
            cutover_step=12,
            in_flight=2,
            max_lag=2,
        )
        server_acks: dict[str, dict] = {}
        for info in result["sinks"].values():
            channel = info["channel"]
            with urllib.request.urlopen(
                f"http://{host}:{server_port}"
                f"/v1/channels/{channel}/ack",
                timeout=3,
            ) as response:
                server_acks[channel] = json.loads(
                    response.read().decode("utf-8")
                )

        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = evidence_dir / "proof.json"
        evidence.write_text(
            json.dumps(
                {
                    "schema": "shardferry.lrs.m3.proof.v1",
                    "proxy_dropped": proxy.dropped,
                    "server_acks": server_acks,
                    "result": result,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        print("LRS M3 · W=8 · F=2 · HTTP")
        print(
            f"ok={result['ok']} lag_max={result['lag_history_max']} "
            f"dropped={proxy.dropped} "
            f"theta={result['theta_digest_migrated'][:16]}"
        )
        for proof in result["proofs"]:
            print(
                f"  rank={proof['rank']} "
                f"in_flight={proof['in_flight_at_cutover']} "
                f"temp_owner={proof['temp_owner']} "
                f"steps={proof['begin_step']}..{proof['cutover_step']} "
                f"wait={proof['cutover_wait_seconds']:.4f}s "
                f"digest_match="
                f"{proof['live_cutover_digest'] == proof['target_cutover_digest']}"
            )
        print(f"evidence {evidence}")
        if not result["ok"]:
            print("M3 FAILED")
            return 1
        print("M3 PASSED")
        return 0
    finally:
        proxy.stop()
        server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
