#!/usr/bin/env python3
"""Run the cross-cluster user acceptance path against an existing management cluster.

This wrapper deliberately delegates to kubectl and shardferryctl. It does not
create clusters, install releases, or mutate source/target data.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    dry_run: bool,
) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(argv), flush=True)
    if dry_run:
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.run(
        argv,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _cluster_refs(migration: Path) -> tuple[str, str]:
    payload = yaml.safe_load(migration.read_text(encoding="utf-8"))
    spec = payload.get("spec", {}) if isinstance(payload, dict) else {}
    source = spec.get("source", {}) if isinstance(spec, dict) else {}
    target = spec.get("target", {}) if isinstance(spec, dict) else {}
    return str(source.get("clusterRef", "")), str(target.get("clusterRef", ""))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the cross-cluster live acceptance path."
    )
    parser.add_argument("--management-kubeconfig", required=True)
    parser.add_argument("--namespace", default="shardferry-control")
    parser.add_argument("--migration", required=True)
    parser.add_argument("--shardferryctl", default="shardferryctl")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--tls-ca", default="")
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--output", default="live-acceptance.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    migration = Path(args.migration).expanduser().resolve()
    if not migration.is_file():
        print(f"live-acceptance: migration not found: {migration}", file=sys.stderr)
        return 2
    source_ref, target_ref = _cluster_refs(migration)
    if not source_ref or not target_ref:
        print(
            "live-acceptance: migration must set source.clusterRef and "
            "target.clusterRef",
            file=sys.stderr,
        )
        return 2
    env = dict(os.environ)
    env["KUBECONFIG"] = str(Path(args.management_kubeconfig).expanduser())
    env.setdefault("SHARDFERRY_AGENT_TOKEN", os.environ.get("SHARDFERRY_AGENT_TOKEN", ""))
    if args.tls_ca:
        env["SHARDFERRY_TLS_CA"] = args.tls_ca
    if args.tls_cert:
        env["SHARDFERRY_TLS_CERT"] = args.tls_cert
    if args.tls_key:
        env["SHARDFERRY_TLS_KEY"] = args.tls_key
    report: dict[str, Any] = {
        "status": "running",
        "migration": str(migration),
        "source_cluster": source_ref,
        "target_cluster": target_ref,
        "namespace": args.namespace,
        "wait": args.wait,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
    }
    try:
        clusters = _run(
            [
                args.kubectl,
                "-n",
                args.namespace,
                "get",
                "shardferryclusters",
                "-o",
                "json",
            ],
            env=env,
            dry_run=args.dry_run,
        )
        if clusters.returncode != 0:
            raise RuntimeError(clusters.stderr.strip() or "kubectl get clusters failed")
        if not args.dry_run:
            payload = json.loads(clusters.stdout)
            items = {
                item.get("metadata", {}).get("name"): item
                for item in payload.get("items", [])
            }
            for ref in (source_ref, target_ref):
                item = items.get(ref)
                if item is None:
                    raise RuntimeError(f"ShardferryCluster {ref!r} not found")
                status = item.get("status", {})
                if not status.get("ready"):
                    raise RuntimeError(
                        f"ShardferryCluster {ref!r} is not ready: "
                        f"{status.get('message', '')}"
                    )
        report["steps"].append({"name": "clusters", "ok": True})

        preflight = _run(
            [
                args.shardferryctl,
                "preflight",
                "-f",
                str(migration),
                "--json",
            ],
            env=env,
            dry_run=args.dry_run,
        )
        if preflight.returncode != 0:
            raise RuntimeError(preflight.stderr.strip() or "preflight failed")
        if not args.dry_run:
            payload = json.loads(preflight.stdout)
            if payload.get("status") != "ok":
                raise RuntimeError(f"preflight status: {payload.get('status')}")
            clusters_result = payload.get("clusters", {})
            for role, ref in (("source", source_ref), ("target", target_ref)):
                item = clusters_result.get(role, {})
                if item.get("name") not in (None, ref) or item.get("ready") is False:
                    raise RuntimeError(
                        f"preflight cluster {role} is not ready: {item}"
                    )
            report["preflight"] = payload
        report["steps"].append({"name": "preflight", "ok": True})

        if args.wait:
            migrate = _run(
                [
                    args.shardferryctl,
                    "migrate",
                    "-f",
                    str(migration),
                    "--wait",
                    "--json",
                ],
                env=env,
                dry_run=args.dry_run,
            )
            if migrate.returncode != 0:
                raise RuntimeError(migrate.stderr.strip() or "migrate failed")
            if not args.dry_run:
                report["migration_result"] = json.loads(migrate.stdout)
            report["steps"].append({"name": "migrate", "ok": True})

        report["status"] = "dry-run" if args.dry_run else "passed"
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(args.output)
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"live-acceptance failed: {exc}", file=sys.stderr)
        print(f"report: {args.output}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
