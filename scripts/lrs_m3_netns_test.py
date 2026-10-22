#!/usr/bin/env python3
"""M3 netns acceptance: two network namespaces, HTTP stream, link down/up."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SERVER = ROOT / "scripts" / "lrs_m3_server.py"
CLIENT = ROOT / "scripts" / "lrs_m3_client.py"
NS_A = "sf-a"
NS_B = "sf-b"
CLIENT_IP = "10.200.0.1"
SERVER_IP = "10.200.0.2"
PORT = 19110


def _prefix() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo", "-n"]


def run(
    argv: list[str],
    *,
    check: bool = True,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        _prefix() + argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def ns_exec(
    ns: str,
    *argv: str,
    check: bool = True,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["ip", "netns", "exec", ns, *argv],
        check=check,
        timeout=timeout,
    )


def wait_health(timeout: float = 10.0) -> None:
    code = (
        "import urllib.request; "
        f"urllib.request.urlopen('http://{SERVER_IP}:{PORT}/health', "
        "timeout=1).read()"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = ns_exec(
            NS_A,
            PYTHON,
            "-c",
            code,
            check=False,
            timeout=5.0,
        )
        if result.returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError("server health check did not pass")


def stop(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="lrs-m3-netns-"))
    state_dir = workdir / "state"
    output = workdir / "client.json"
    server = None
    client = None
    try:
        run(["ip", "link", "del", "veth-a"], check=False)
        for ns in (NS_A, NS_B):
            run(["ip", "netns", "del", ns], check=False)
        run(["ip", "netns", "add", NS_A])
        run(["ip", "netns", "add", NS_B])
        run(
            [
                "ip",
                "link",
                "add",
                "veth-a",
                "type",
                "veth",
                "peer",
                "name",
                "veth-b",
            ]
        )
        run(["ip", "link", "set", "veth-a", "netns", NS_A])
        run(["ip", "link", "set", "veth-b", "netns", NS_B])
        for ns, dev, address in (
            (NS_A, "veth-a", f"{CLIENT_IP}/24"),
            (NS_B, "veth-b", f"{SERVER_IP}/24"),
        ):
            ns_exec(ns, "ip", "addr", "add", address, "dev", dev)
            ns_exec(ns, "ip", "link", "set", dev, "up")
            ns_exec(ns, "ip", "link", "set", "lo", "up")

        server = subprocess.Popen(
            _prefix()
            + [
                "ip",
                "netns",
                "exec",
                NS_B,
                PYTHON,
                str(SERVER),
                "--host",
                SERVER_IP,
                "--port",
                str(PORT),
                "--state-dir",
                str(state_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_health()

        client = subprocess.Popen(
            _prefix()
            + [
                "ip",
                "netns",
                "exec",
                NS_A,
                PYTHON,
                str(CLIENT),
                "--server",
                f"http://{SERVER_IP}:{PORT}",
                "--steps",
                "120",
                "--begin",
                "4",
                "--cutover",
                "100",
                "--in-flight",
                "2",
                "--max-lag",
                "2",
                "--step-delay",
                "0.01",
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        time.sleep(0.3)
        ns_exec(NS_A, "ip", "link", "set", "veth-a", "down")
        time.sleep(0.5)
        ns_exec(NS_A, "ip", "link", "set", "veth-a", "up")

        stdout, stderr = client.communicate(timeout=60)
        print(stdout, end="")
        if stderr:
            print(stderr, file=sys.stderr, end="")
        if client.returncode != 0:
            raise RuntimeError(f"client exited {client.returncode}")
        result = json.loads(output.read_text(encoding="utf-8"))
        if not result["ok"]:
            raise RuntimeError(f"client checks failed: {result['checks']}")
        for rank, info in result["sinks"].items():
            latest = json.loads(
                (state_dir / info["channel"] / "latest.json").read_text(
                    encoding="utf-8"
                )
            )
            proof = next(
                item
                for item in result["proofs"]
                if item["rank"] == int(rank)
            )
            if (
                latest["step"] != 100
                or latest["digest"] != proof["target_cutover_digest"]
            ):
                raise RuntimeError(
                    f"target persistence mismatch for rank {rank}: "
                    f"{latest}"
                )
        print(
            f"M3 NETNS PASSED lag_max={result['lag_history_max']} "
            f"temp_owners="
            f"{[proof['temp_owner'] for proof in result['proofs']]} "
            f"wait="
            f"{[round(proof['cutover_wait_seconds'], 4) for proof in result['proofs']]}"
        )
        if args.evidence:
            evidence = Path(args.evidence)
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text(
                json.dumps(
                    {
                        "schema": "shardferry.lrs.m3.netns.v1",
                        "ok": True,
                        "result": result,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"evidence {evidence}")
        return 0
    except Exception as exc:
        print(f"M3 NETNS FAILED: {exc}", file=sys.stderr)
        if server is not None:
            stop(server)
            try:
                stdout, stderr = server.communicate(timeout=1)
                if stdout:
                    print(stdout, file=sys.stderr, end="")
                if stderr:
                    print(stderr, file=sys.stderr, end="")
            except Exception:
                pass
        return 1
    finally:
        stop(client)
        stop(server)
        for ns in (NS_A, NS_B):
            run(["ip", "netns", "del", ns], check=False)
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
