#!/usr/bin/env python3
"""Collect a redacted Shardferry support bundle with kubectl.

The bundle is intentionally boring: versions, Kubernetes objects, events,
pod logs and command results. Secret values are never collected; only Secret
names are listed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_REDACTIONS = (
    re.compile(
        r"(?im)^(\s*(?:[A-Za-z0-9_.-]*(?:token|password|secret|authorization|"
        r"api[-_]?key)[A-Za-z0-9_.-]*)\s*[:=]\s*).*$"
    ),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)([\"']?(?:token|password|secret|authorization|api[-_]?key)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+"
    ),
)


def redact_text(value: str) -> str:
    """Redact likely credentials while preserving surrounding YAML/JSON."""
    result = value
    for pattern in _REDACTIONS:
        result = pattern.sub(r"\1<redacted>", result)
    return result


@dataclass
class CommandResult:
    name: str
    argv: list[str]
    returncode: int
    stdout_file: str | None
    stderr_file: str | None
    error: str | None = None


def _run(
    argv: list[str],
    output_dir: Path,
    name: str,
    *,
    timeout: int,
    dry_run: bool,
) -> CommandResult:
    stdout_file = output_dir / "commands" / f"{name}.stdout.txt"
    stderr_file = output_dir / "commands" / f"{name}.stderr.txt"
    stdout_file.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return CommandResult(
            name=name,
            argv=argv,
            returncode=0,
            stdout_file=None,
            stderr_file=None,
            error="dry-run",
        )
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return CommandResult(
            name=name,
            argv=argv,
            returncode=127,
            stdout_file=None,
            stderr_file=None,
            error=f"command not found: {argv[0]}",
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            name=name,
            argv=argv,
            returncode=124,
            stdout_file=None,
            stderr_file=None,
            error=f"timeout after {timeout}s: {exc}",
        )
    stdout_file.write_text(redact_text(completed.stdout), encoding="utf-8")
    stderr_file.write_text(redact_text(completed.stderr), encoding="utf-8")
    return CommandResult(
        name=name,
        argv=argv,
        returncode=completed.returncode,
        stdout_file=str(stdout_file.relative_to(output_dir)),
        stderr_file=str(stderr_file.relative_to(output_dir)),
    )


def _kubectl(
    kubeconfig: str | None,
    context: str | None,
    args: Iterable[str],
) -> list[str]:
    result = ["kubectl"]
    if kubeconfig:
        result.extend(["--kubeconfig", kubeconfig])
    if context:
        result.extend(["--context", context])
    result.extend(args)
    return result


def _build_commands(
    namespace: str,
    kubeconfig: str | None,
    context: str | None,
) -> list[tuple[str, list[str]]]:
    k = lambda *args: _kubectl(kubeconfig, context, args)  # noqa: E731
    return [
        ("kubectl_version_client", k("version", "--client", "-o", "yaml")),
        ("kubectl_current_context", k("config", "current-context")),
        ("kubectl_nodes", k("get", "nodes", "-o", "wide")),
        ("kubectl_namespace_objects", k("-n", namespace, "get", "all")),
        (
            "kubectl_checkpoint_migrations",
            k("-n", namespace, "get", "checkpointmigrations", "-o", "yaml"),
        ),
        (
            "kubectl_shardferry_clusters",
            k("-n", namespace, "get", "shardferryclusters", "-o", "yaml"),
        ),
        (
            "kubectl_events",
            k(
                "-n",
                namespace,
                "get",
                "events",
                "--sort-by=.lastTimestamp",
            ),
        ),
        (
            "kubectl_pods_json",
            k("-n", namespace, "get", "pods", "-o", "json"),
        ),
        (
            "kubectl_secret_names",
            k("-n", namespace, "get", "secrets", "-o", "name"),
        ),
        ("helm_list", ["helm", "list", "-n", namespace]),
    ]


def _collect_pod_logs(
    namespace: str,
    kubeconfig: str | None,
    context: str | None,
    output_dir: Path,
    *,
    timeout: int,
    dry_run: bool,
) -> list[CommandResult]:
    if dry_run:
        return []
    pods_result = _run(
        _kubectl(
            kubeconfig,
            context,
            ["-n", namespace, "get", "pods", "-o", "json"],
        ),
        output_dir,
        "pod_logs_pod_list",
        timeout=timeout,
        dry_run=False,
    )
    if pods_result.returncode != 0 or not pods_result.stdout_file:
        return []
    try:
        payload = json.loads(
            (output_dir / pods_result.stdout_file).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    results: list[CommandResult] = []
    for pod in payload.get("items", []):
        name = pod.get("metadata", {}).get("name")
        if not name:
            continue
        argv = _kubectl(
            kubeconfig,
            context,
            [
                "-n",
                namespace,
                "logs",
                name,
                "--all-containers",
                "--tail=2000",
                "--timestamps",
            ],
        )
        results.append(
            _run(
                argv,
                output_dir,
                "logs_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", name),
                timeout=timeout,
                dry_run=False,
            )
        )
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect a redacted Shardferry support bundle from a Kubernetes "
            "cluster. Secret values are never collected."
        )
    )
    parser.add_argument("--namespace", default="shardferry-control")
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--context", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--no-logs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if shutil.which("kubectl") is None:
        print("support-bundle: kubectl not found in PATH", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else Path.cwd() / f"shardferry-support-{now}.tar.gz"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="shardferry-support-",
        dir=str(output.parent),
    ) as temporary:
        temp_root = Path(temporary)
        bundle = temp_root / f"shardferry-support-{now}"
        (bundle / "commands").mkdir(parents=True, exist_ok=True)
        commands = _build_commands(args.namespace, args.kubeconfig, args.context)
        results: list[CommandResult] = []
        for name, command in commands:
            results.append(
                _run(
                    command,
                    bundle,
                    name,
                    timeout=args.timeout,
                    dry_run=args.dry_run,
                )
            )
        if not args.no_logs:
            results.extend(
                _collect_pod_logs(
                    args.namespace,
                    args.kubeconfig,
                    args.context,
                    bundle,
                    timeout=args.timeout,
                    dry_run=args.dry_run,
                )
            )
        manifest = {
            "created_at": now,
            "namespace": args.namespace,
            "context": args.context,
            "kubeconfig": args.kubeconfig,
            "redacted": True,
            "secret_values_collected": False,
            "commands": [asdict(result) for result in results],
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if args.dry_run:
            for result in results:
                print(" ".join(result.argv))
            return 0
        with tarfile.open(output, "w:gz") as archive:
            archive.add(bundle, arcname=bundle.name)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
