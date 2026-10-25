from __future__ import annotations

import argparse
import json
import os
import sys
import time
from math import prod
from pathlib import Path

import yaml

from . import __version__
from .agent import AgentError, build_agent_server
from .agent.executor import HttpArtifactResolver, TargetTaskExecutor
from .coordinator import expected_rank_work
from .commands.readiness import run_readiness
from .commands.restore_drill import run_restore_drill_command
from .contracts import contract_version, validate_capabilities
from .state import (
    StateBundle,
    StateBundleError,
    builtin_providers,
    capture_state,
    restore_state,
    verify_state,
)
from .dcp import DcpError, export_dcp, inspect_dcp, migrate_dcp
from .doctor import checkpoint_doctor, doctor, environment_doctor
from .defaults import (
    DEFAULT_LISTEN,
    DEFAULT_PUBLIC_LISTEN,
    DEFAULT_SOURCE_PLACEMENTS,
    DEFAULT_TARGET_PLACEMENTS,
)
from .demo import DemoError, run_demo
from .executor import ExecutionError, apply
from .inspect import inspect_toy
from .manifest import ManifestError, load_payload, load_spec
from .planning import PlannerError, plan
from .verification import ProofError, build_proof, combine_proofs, verify_proof
from .remote import fetch_spec, rewrite_source_uris
from .reports import ReportError, load_reports, summarize_reports
from .serve import serve
from .storage.file import StorageError
from .planning.tensor_io import TensorIOError
from .verification import VerificationError, verify


_PUBLIC_COMMANDS = (
    "demo",
    "migrate",
    "doctor",
    "capabilities",
    "readiness",
    "restore-drill",
    "state",
    "tui",
)
_INTERNAL_COMMANDS = (
    "inspect",
    "plan",
    "apply",
    "verify",
    "proof-build",
    "proof-verify",
    "proof-merge",
    "serve",
    "agent",
    "dcp-inspect",
    "dcp-export",
    "dcp-migrate",
    "spec-http",
    "fetch-spec",
    "summarize",
)


def _not_implemented(command: str) -> int:
    print(
        f"aevrel {command}: not implemented",
        file=sys.stderr,
    )
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aevrel",
        description=(
            "Inspect checkpoint readiness and execute checkpoint recovery, "
            "mobility, and restore-drill actions."
        ),
        epilog=(
            "Start here:\n"
            "  aevrel demo\n"
            "  aevrel tui\n"
            "  aevrel readiness --checkpoint /path/to/source.dcp "
            "--target-world 3\n"
            "  aevrel restore-drill --checkpoint /path/to/target.dcp "
            "--target-command 'python train_resume.py --checkpoint {checkpoint}'\n"
            "  aevrel migrate --checkpoint /path/to/source.dcp --target-world 3\n"
            "Advanced: aevrel state"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="print version")
    sub = parser.add_subparsers(
        dest="command",
        metavar=(
            "{demo,migrate,doctor,capabilities,readiness,restore-drill,state,tui}"
        ),
    )

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--input", required=True)
    inspect.add_argument("--out", required=False)

    plan = sub.add_parser("plan")
    plan.add_argument("--spec", required=True)
    plan.add_argument("--out", required=False)

    apply = sub.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--spec", required=True)
    apply.add_argument("--source", required=False)
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--report", required=False)
    apply.add_argument("--target-rank", type=int, required=False)
    apply.add_argument("--target-root", required=False)

    verify = sub.add_parser("verify")
    verify.add_argument("--spec", required=True)
    verify.add_argument("--plan", required=True)
    verify.add_argument("--report", required=False)
    verify.add_argument("--target-rank", type=int, required=False)
    verify.add_argument("--target-root", required=False)

    proof_build = sub.add_parser("proof-build")
    proof_build.add_argument("--spec", required=True)
    proof_build.add_argument("--plan", required=True)
    proof_build.add_argument("--target-root", required=True)
    proof_build.add_argument("--source", required=False)
    proof_build.add_argument("--token", required=False)
    proof_build.add_argument("--target-rank", type=int, required=False)
    proof_build.add_argument("--out", required=True)

    proof_verify = sub.add_parser("proof-verify")
    proof_verify.add_argument("--proof", required=True)
    proof_verify.add_argument("--target-root", required=True)
    proof_verify.add_argument("--plan", required=False)
    proof_verify.add_argument("--source", required=False)
    proof_verify.add_argument("--token", required=False)

    proof_merge = sub.add_parser("proof-merge")
    proof_merge.add_argument("--proof", action="append", required=True)
    proof_merge.add_argument("--out", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--manifest", required=True)
    serve.add_argument("--listen", default=DEFAULT_LISTEN)
    serve.add_argument(
        "--rank",
        type=int,
        required=False,
        help="serve only this source rank's local shards",
    )
    serve.add_argument(
        "--public",
        action="store_true",
        help="bind 0.0.0.0:9000 for a physical A/B run",
    )

    agent = sub.add_parser("agent")
    agent.add_argument("--listen", default="0.0.0.0:9443")
    agent.add_argument("--state-dir", required=True)
    agent.add_argument("--spec", required=False)
    agent.add_argument("--checkpoint", required=False)
    agent.add_argument("--metadata", required=False)
    agent.add_argument("--target-root", required=False)
    agent.add_argument("--public-url", required=False)
    agent.add_argument("--tls-cert", default="")
    agent.add_argument("--tls-key", default="")
    agent.add_argument("--tls-ca", default="")
    agent.add_argument("--cache-bytes", type=int, default=512 * 1024 * 1024)
    agent.add_argument("--artifact-base-url", required=True)
    agent.add_argument("--proof", action="store_true")
    agent.add_argument(
        "--load-smoke",
        action="store_true",
        help="verify each completed target rank with a local DCP load",
    )

    dcp_inspect = sub.add_parser("dcp-inspect")
    dcp_inspect.add_argument("--input", required=True)
    dcp_inspect.add_argument("--work-dir", required=False)
    dcp_inspect.add_argument("--out", required=False)
    dcp_inspect.add_argument("--source-mesh-shape", required=False)
    dcp_inspect.add_argument(
        "--source-placements", default=",".join(DEFAULT_SOURCE_PLACEMENTS)
    )
    dcp_inspect.add_argument("--target-world", type=int, required=False)
    dcp_inspect.add_argument("--target-mesh-shape", required=False)
    dcp_inspect.add_argument(
        "--target-placements", default=",".join(DEFAULT_TARGET_PLACEMENTS)
    )

    dcp_export = sub.add_parser("dcp-export")
    dcp_export.add_argument("--spec", required=True)
    dcp_export.add_argument("--output", required=True)
    dcp_export.add_argument("--force", action="store_true")

    dcp_migrate = sub.add_parser("dcp-migrate")
    dcp_migrate.add_argument("--input", required=True)
    dcp_migrate.add_argument("--output", required=False)
    dcp_migrate.add_argument("--work-dir", required=False)
    dcp_migrate.add_argument("--source-mesh-shape", required=False)
    dcp_migrate.add_argument(
        "--source-placements", default=",".join(DEFAULT_SOURCE_PLACEMENTS)
    )
    dcp_migrate.add_argument("--target-world", type=int, required=False)
    dcp_migrate.add_argument("--target-mesh-shape", required=False)
    dcp_migrate.add_argument(
        "--target-placements", default=",".join(DEFAULT_TARGET_PLACEMENTS)
    )
    dcp_migrate.add_argument("--force", action="store_true")

    demo = sub.add_parser(
        "demo",
        help=(
            "run a self-contained 4 -> 3 migration; --dcp adds a real "
            "CPU DCP pair"
        ),
    )
    demo.add_argument(
        "--work-dir",
        help="output directory (default: ./aevrel-demo)",
    )
    demo.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON",
    )
    demo.add_argument(
        "--dcp",
        action="store_true",
        help="also build a real source/target DCP pair and verify target load",
    )

    migrate = sub.add_parser(
        "migrate",
        help="migrate a DCP checkpoint to a new world size",
    )
    migrate.add_argument(
        "--checkpoint",
        "-c",
        required=True,
        metavar="PATH",
        help="source DCP directory",
    )
    migrate.add_argument(
        "--output",
        "-o",
        required=False,
        metavar="PATH",
        help="target DCP directory (default: <checkpoint>.migrated)",
    )
    migrate.add_argument(
        "--target-world",
        "-n",
        type=int,
        required=False,
        metavar="N",
        help="target world size (default: source world size)",
    )
    migrate.add_argument(
        "--work-dir",
        metavar="PATH",
        help="resumable work directory (default: <checkpoint>.aevrel)",
    )
    migrate.add_argument(
        "--force",
        action="store_true",
        help="allow writing into a non-empty output directory",
    )
    migrate.add_argument(
        "--keep-work",
        action="store_true",
        help="keep target raw staging files after successful export",
    )
    migrate.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON",
    )
    migrate.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the transfer progress display",
    )
    topology = migrate.add_argument_group(
        "topology (inferred for the common 1D FSDP case)"
    )
    topology.add_argument(
        "--source-mesh-shape",
        metavar="2,2",
        help="source mesh shape",
    )
    topology.add_argument(
        "--source-placements",
        default=",".join(DEFAULT_SOURCE_PLACEMENTS),
        metavar="Shard(0),Shard(1)",
        help="source placements (default: Shard(0))",
    )
    topology.add_argument(
        "--target-mesh-shape",
        metavar="3,1",
        help="target mesh shape (default: one dimension of --target-world)",
    )
    topology.add_argument(
        "--target-placements",
        default=",".join(DEFAULT_TARGET_PLACEMENTS),
        metavar="Shard(0),Replicate()",
        help="target placements (default: Shard(0))",
    )

    spec_http = sub.add_parser("spec-http")
    spec_http.add_argument("--spec", required=True)
    spec_http.add_argument("--base-url", required=True)
    spec_http.add_argument("--out", required=True)
    spec_http.add_argument("--target-storage", required=False)

    fetch = sub.add_parser("fetch-spec")
    fetch.add_argument("--server", required=True)
    fetch.add_argument("--target-storage", required=True)
    fetch.add_argument("--out", required=True)

    summarize = sub.add_parser("summarize")
    summarize.add_argument("--reports-dir", required=True)
    summarize.add_argument("--plan", required=False)
    summarize.add_argument("--spec", required=False)
    summarize.add_argument("--out", required=True)

    doctor_parser = sub.add_parser(
        "doctor",
        help="check Python, PyTorch, DCP, and paths (read-only)",
    )
    doctor_parser.add_argument(
        "--checkpoint",
        "-c",
        metavar="PATH",
        help="source DCP directory to check",
    )
    doctor_parser.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        help="planned target DCP directory",
    )
    doctor_parser.add_argument(
        "--work-dir",
        metavar="PATH",
        help="planned resumable work directory",
    )
    doctor_parser.add_argument(
        "--force",
        action="store_true",
        help="accept a non-empty output directory",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON",
    )
    doctor_parser.add_argument("--spec", help=argparse.SUPPRESS)

    capabilities = sub.add_parser(
        "capabilities",
        help="print machine-readable capabilities",
    )
    capabilities.add_argument("--json", action="store_true")

    readiness = sub.add_parser(
        "readiness",
        help="show checkpoint completeness, freshness, and readiness",
    )
    readiness.add_argument("--checkpoint", "-c", required=True)
    readiness.add_argument("--namespace", default="")
    readiness.add_argument("--job-uid", default="")
    readiness.add_argument("--version", default="")
    readiness.add_argument("--step", type=int, default=None)
    readiness.add_argument("--target-world", type=int, default=None)
    readiness.add_argument("--max-age-seconds", type=int, default=86400)
    readiness.add_argument(
        "--drill-report",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "target-ready report file or directory; repeatable; "
            "all checkpoint ranks must be covered"
        ),
    )
    readiness.add_argument("--strict", action="store_true")
    readiness.add_argument("--json", action="store_true")

    restore_drill = sub.add_parser(
        "restore-drill",
        help="run a real target-command load + N-step restore drill",
    )
    restore_drill.add_argument("--checkpoint", "-c", required=True)
    restore_drill.add_argument("--target-command", required=True)
    restore_drill.add_argument("--target-world", type=int, default=1)
    restore_drill.add_argument("--rank", type=int, default=0)
    restore_drill.add_argument("--steps", type=int, default=10)
    restore_drill.add_argument("--version", default="")
    restore_drill.add_argument("--migration-uid", default="")
    restore_drill.add_argument("--report", default="")
    restore_drill.add_argument("--timeout", type=float, default=900.0)
    restore_drill.add_argument("--require-state-complete", action="store_true")
    restore_drill.add_argument("--json", action="store_true")

    state = sub.add_parser(
        "state",
        help="capture, verify, or restore rank-local resume state (advanced)",
    )
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_capture = state_sub.add_parser("capture")
    state_capture.add_argument("--root", required=True)
    state_capture.add_argument("--checkpoint-id", required=True)
    state_capture.add_argument("--checkpoint-version", required=True)
    state_capture.add_argument("--rank", type=int, required=True)
    state_capture.add_argument("--source-world", type=int, required=True)
    state_capture.add_argument("--target-world", type=int, default=None)
    state_capture.add_argument("--rank-map", default="")
    state_capture.add_argument(
        "--providers",
        default="python.random,numpy.random",
    )
    state_verify = state_sub.add_parser("verify")
    state_verify.add_argument("--root", required=True)
    state_verify.add_argument("--require-complete", action="store_true")
    state_restore = state_sub.add_parser("restore")
    state_restore.add_argument("--root", required=True)
    state_restore.add_argument("--target-rank", type=int, required=True)
    state_restore.add_argument(
        "--providers",
        default="python.random,numpy.random",
    )
    tui = sub.add_parser(
        "tui",
        help="open the terminal checkpoint control plane",
    )
    tui.add_argument("--work-dir", default=None)

    return parser


def _emit_failure(command: str, exc: Exception, json_mode: bool) -> int:
    if json_mode:
        payload = {
            "status": "failed",
            "error": {
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"aevrel {command}: {exc}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        arguments
        and not arguments[0].startswith("-")
        and arguments[0] not in _PUBLIC_COMMANDS
        and arguments[0] not in _INTERNAL_COMMANDS
    ):
        print(f"aevrel: unknown command: {arguments[0]}", file=sys.stderr)
        print(
            "Available commands: " + ", ".join(_PUBLIC_COMMANDS),
            file=sys.stderr,
        )
        print("Run `aevrel --help` for usage.", file=sys.stderr)
        return 2
    args = parser.parse_args(arguments)

    if args.version:
        print(f"aevrel {__version__}")
        return 0
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "inspect":
        try:
            payload = load_payload(args.input)
            spec = inspect_toy(payload)
            text = yaml.safe_dump(spec, sort_keys=False)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            else:
                print(text, end="")
            return 0
        except (ManifestError, OSError, ValueError) as exc:
            print(f"aevrel inspect: {exc}", file=sys.stderr)
            return 2
    if args.command == "plan":
        try:
            spec = load_spec(args.spec)
            result = plan(spec)
            text = json.dumps(result, indent=2) + "\n"
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            else:
                print(text, end="")
            return 0
        except (ManifestError, PlannerError, OSError, ValueError) as exc:
            print(f"aevrel plan: {exc}", file=sys.stderr)
            return 2
    if args.command == "apply":
        try:
            spec = load_spec(args.spec)
            plan_payload = _load_json_object(args.plan)
            report = apply(
                plan_payload,
                spec,
                source=args.source,
                dry_run=args.dry_run,
                target_rank=args.target_rank,
                target_root=args.target_root,
            )
            _write_json(report, args.report)
            return 0
        except (
            ExecutionError,
            ManifestError,
            PlannerError,
            StorageError,
            TensorIOError,
            OSError,
            ValueError,
        ) as exc:
            print(f"aevrel apply: {exc}", file=sys.stderr)
            return 2
    if args.command == "verify":
        try:
            spec = load_spec(args.spec)
            plan_payload = _load_json_object(args.plan)
            result = verify(
                spec,
                plan_payload,
                target_rank=args.target_rank,
                target_root=args.target_root,
            )
            _write_json(result, args.report)
            return 0
        except (
            ManifestError,
            PlannerError,
            StorageError,
            TensorIOError,
            VerificationError,
            OSError,
            ValueError,
        ) as exc:
            print(f"aevrel verify: {exc}", file=sys.stderr)
            return 2
    if args.command == "proof-build":
        try:
            spec = load_spec(args.spec)
            plan_payload = _load_json_object(args.plan)
            proof = build_proof(
                spec,
                plan_payload,
                args.target_root,
                source=args.source,
                token=args.token,
                target_rank=args.target_rank,
            )
            Path(args.out).write_text(
                json.dumps(proof, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return 0
        except (ManifestError, PlannerError, ProofError, OSError, ValueError) as exc:
            print(f"aevrel proof-build: {exc}", file=sys.stderr)
            return 2
    if args.command == "proof-verify":
        try:
            proof = _load_json_object(args.proof)
            plan_payload = (
                _load_json_object(args.plan) if args.plan else None
            )
            result = verify_proof(
                proof,
                args.target_root,
                plan=plan_payload,
                source=args.source,
                token=args.token,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        except (ManifestError, ProofError, OSError, ValueError) as exc:
            print(f"aevrel proof-verify: {exc}", file=sys.stderr)
            return 2
    if args.command == "proof-merge":
        try:
            proofs = [_load_json_object(path) for path in args.proof]
            merged = combine_proofs(proofs)
            Path(args.out).write_text(
                json.dumps(merged, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return 0
        except (ManifestError, ProofError, OSError, ValueError) as exc:
            print(f"aevrel proof-merge: {exc}", file=sys.stderr)
            return 2
    if args.command == "serve":
        try:
            listen = DEFAULT_PUBLIC_LISTEN if args.public else args.listen
            serve(args.manifest, listen, rank=args.rank)
            return 0
        except (ManifestError, OSError, StorageError, ValueError) as exc:
            print(f"aevrel serve: {exc}", file=sys.stderr)
            return 2
    if args.command == "agent":
        token = os.environ.get("AEVREL_AGENT_TOKEN", "")
        if not token:
            print(
                "aevrel agent: AEVREL_AGENT_TOKEN is required",
                file=sys.stderr,
            )
            return 2
        try:
            public_url = args.public_url
            if public_url:
                public_url = (
                    public_url.replace(
                        "${POD_NAME}",
                        os.environ.get("POD_NAME", ""),
                    )
                    .replace(
                        "${HOSTNAME}",
                        os.environ.get("HOSTNAME", ""),
                    )
                    .replace(
                        "${NODE_IP}",
                        os.environ.get("NODE_IP", ""),
                    )
                    .replace(
                        "{pod}",
                        os.environ.get("POD_NAME", ""),
                    )
                )
            executor = TargetTaskExecutor(
                HttpArtifactResolver(args.artifact_base_url, token),
                source_token=token,
                proof_enabled=args.proof,
                load_smoke=args.load_smoke,
            )
            server = build_agent_server(
                args.listen,
                token,
                args.state_dir,
                executor=executor,
                committer=executor.commit,
                spec_path=args.spec,
                dcp_checkpoint=args.checkpoint,
                dcp_metadata=args.metadata,
                target_root=args.target_root,
                public_url=public_url,
                max_cache_bytes=args.cache_bytes,
                tls_cert=args.tls_cert or None,
                tls_key=args.tls_key or None,
                tls_ca=args.tls_ca or None,
            )
            print(
                f"aevrel agent listening on "
                f"{server.server_address[0]}:{server.server_address[1]}"
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        except (OSError, ValueError, AgentError) as exc:
            print(f"aevrel agent: {exc}", file=sys.stderr)
            return 2
    if args.command == "dcp-inspect":
        try:
            spec = inspect_dcp(
                args.input,
                args.work_dir,
                source_mesh_shape=_parse_shape(args.source_mesh_shape),
                source_placements=_parse_csv(args.source_placements),
                target_world_size=args.target_world,
                target_mesh_shape=_parse_shape(args.target_mesh_shape),
                target_placements=_parse_csv(args.target_placements),
            )
            text = yaml.safe_dump(spec, sort_keys=False)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
            else:
                print(text, end="")
            return 0
        except (DcpError, ManifestError, OSError, ValueError) as exc:
            print(f"aevrel dcp-inspect: {exc}", file=sys.stderr)
            return 2
    if args.command == "dcp-export":
        try:
            spec = load_spec(args.spec)
            output = export_dcp(spec, args.output, force=args.force)
            print(output)
            return 0
        except (DcpError, ManifestError, OSError, ValueError) as exc:
            print(f"aevrel dcp-export: {exc}", file=sys.stderr)
            return 2
    if args.command == "dcp-migrate":
        try:
            result = migrate_dcp(
                args.input,
                args.output,
                args.work_dir,
                source_mesh_shape=_parse_shape(args.source_mesh_shape),
                source_placements=_parse_csv(args.source_placements),
                target_world_size=args.target_world,
                target_mesh_shape=_parse_shape(args.target_mesh_shape),
                target_placements=_parse_csv(args.target_placements),
                force=args.force,
            )
            print(json.dumps(
                {
                    "output": result["output"],
                    "work_dir": result["work_dir"],
                    "verification": result["report"]["verification"],
                },
                indent=2,
            ))
            return 0
        except (
            DcpError,
            ExecutionError,
            ManifestError,
            PlannerError,
            OSError,
            ValueError,
        ) as exc:
            print(f"aevrel dcp-migrate: {exc}", file=sys.stderr)
            return 2
    if args.command == "migrate":
        target_world = args.target_world
        target_shape = _parse_shape(args.target_mesh_shape)
        if target_world is None and target_shape:
            target_world = prod(target_shape)
        if (
            target_world is not None
            and target_shape is not None
            and prod(target_shape) != target_world
        ):
            print(
                "aevrel migrate: --target-world does not match "
                "--target-mesh-shape",
                file=sys.stderr,
            )
            return 2

        def report_progress(message: str) -> None:
            if not args.json:
                print(f"aevrel: {message}", file=sys.stderr, flush=True)

        progress_event = None
        if not args.json and not args.no_progress:
            progress_event = _make_progress_event_reporter()

        try:
            result = migrate_dcp(
                args.checkpoint,
                args.output,
                args.work_dir,
                source_mesh_shape=_parse_shape(args.source_mesh_shape),
                source_placements=_parse_csv(args.source_placements),
                target_world_size=target_world,
                target_mesh_shape=target_shape,
                target_placements=_parse_csv(args.target_placements),
                force=args.force,
                keep_work=args.keep_work,
                progress=report_progress,
                progress_event=progress_event,
            )
            summary = _migration_summary(args.checkpoint, result)
            if args.json:
                print(json.dumps(summary, indent=2))
            else:
                _print_migration_summary(summary)
            return 0
        except KeyboardInterrupt:
            print(
                "\naevrel migrate: interrupted; rerun the same command "
                "to resume",
                file=sys.stderr,
            )
            return 130
        except (
            DcpError,
            ExecutionError,
            ManifestError,
            PlannerError,
            OSError,
            ValueError,
        ) as exc:
            return _emit_failure("migrate", exc, args.json)
    if args.command == "demo":
        try:
            result = run_demo(args.work_dir, include_dcp=args.dcp)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _print_demo_summary(result)
            return 0
        except (DemoError, OSError, ValueError) as exc:
            return _emit_failure("demo", exc, args.json)
    if args.command == "tui":
        try:
            from .tui import run_tui

            return run_tui(args.work_dir)
        except (OSError, ValueError) as exc:
            print(f"aevrel tui: {exc}", file=sys.stderr)
            return 2
    if args.command == "spec-http":
        try:
            spec = load_spec(args.spec)
            rewritten = rewrite_source_uris(
                spec,
                args.base_url,
                target_storage=args.target_storage,
            )
            Path(args.out).write_text(
                yaml.safe_dump(rewritten, sort_keys=False),
                encoding="utf-8",
            )
            return 0
        except (ManifestError, OSError, ValueError) as exc:
            print(f"aevrel spec-http: {exc}", file=sys.stderr)
            return 2
    if args.command == "fetch-spec":
        try:
            fetch_spec(args.server, args.target_storage, args.out)
            return 0
        except (OSError, ValueError) as exc:
            print(f"aevrel fetch-spec: {exc}", file=sys.stderr)
            return 2
    if args.command == "summarize":
        try:
            if bool(args.plan) != bool(args.spec):
                raise ReportError("--plan and --spec must be provided together")
            expected_by_rank = None
            if args.plan:
                plan_payload = _load_json_object(args.plan)
                spec = load_spec(args.spec)
                expected_by_rank = expected_rank_work(plan_payload, spec)
            summary = summarize_reports(
                load_reports(args.reports_dir),
                expected_by_rank=expected_by_rank,
            )
            Path(args.out).write_text(
                json.dumps(summary, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(summary, indent=2))
            return 0
        except (OSError, ReportError, ValueError) as exc:
            print(f"aevrel summarize: {exc}", file=sys.stderr)
            return 2
    if args.command == "capabilities":
        try:
            payload = {
                "version": contract_version(),
                "apiVersion": "aevrel.dev/v1",
                "agentVersion": __version__,
                "roles": ["source", "target"],
                "features": [
                    "dcp",
                    "range-read",
                    "bitwise-verify",
                    "merkle-proof",
                    "rank-discovery",
                    "metadata-transfer",
                    "checkpoint-readiness",
                    "restore-drill",
                    "state-bundle",
                    "target-ready",
                    "adaptive-rate-limit",
                    "local-tui",
                ],
            }
            validate_capabilities(payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        except ValueError as exc:
            return _emit_failure("capabilities", exc, args.json)
    if args.command == "readiness":
        return run_readiness(args)
    if args.command == "restore-drill":
        return run_restore_drill_command(args)
    if args.command == "state":
        try:
            if args.state_command == "capture":
                rank_map = _parse_rank_map(args.rank_map)
                bundle = capture_state(
                    root=Path(args.root),
                    checkpoint_id=args.checkpoint_id,
                    version=args.checkpoint_version,
                    rank=args.rank,
                    source_world_size=args.source_world,
                    target_world_size=args.target_world,
                    rank_map=rank_map,
                    providers=builtin_providers(
                        args.providers.split(",")
                    ),
                )
                _write_json(bundle.manifest.to_dict(), None)
                return 0
            if args.state_command == "verify":
                bundle = StateBundle.read(args.root)
                verify_state(
                    bundle,
                    require_complete=args.require_complete,
                )
                _write_json(
                    {
                        "root": str(bundle.root),
                        "complete": bundle.manifest.complete,
                        "entries": len(bundle.manifest.entries),
                    },
                    None,
                )
                return 0
            if args.state_command == "restore":
                bundle = StateBundle.read(args.root)
                restore_state(
                    bundle,
                    target_rank=args.target_rank,
                    providers=builtin_providers(
                        args.providers.split(",")
                    ),
                )
                _write_json(
                    {
                        "root": str(bundle.root),
                        "targetRank": args.target_rank,
                        "version": bundle.manifest.version,
                    },
                    None,
                )
                return 0
        except (OSError, StateBundleError, ValueError) as exc:
            print(f"aevrel state: {exc}", file=sys.stderr)
            return 2
    if args.command == "doctor":
        if args.spec and (
            args.checkpoint or args.output or args.work_dir
        ):
            return _emit_failure(
                "doctor",
                ValueError(
                    "--spec cannot be combined with "
                    "--checkpoint/--output/--work-dir"
                ),
                args.json,
            )
        if args.spec:
            result = doctor(args.spec)
        elif args.checkpoint:
            result = checkpoint_doctor(
                args.checkpoint,
                output=args.output,
                work_dir=args.work_dir,
                force=args.force,
            )
        elif args.output or args.work_dir:
            return _emit_failure(
                "doctor",
                ValueError("--output/--work-dir require --checkpoint"),
                args.json,
            )
        else:
            result = environment_doctor()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_doctor_summary(result, checkpoint=args.checkpoint)
        return 0 if result["status"] == "ok" else 2
    return _not_implemented(args.command)


def _migration_summary(checkpoint: str, result: dict) -> dict:
    spec = result["spec"]
    plan = result["plan"]
    report = result["report"]
    source_mesh = spec["source"]["mesh"]
    target_mesh = spec["target"]["mesh"]
    return {
        "status": "success",
        "checkpoint": str(Path(checkpoint).resolve()),
        "output": result["output"],
        "work_dir": result["work_dir"],
        "topology": {
            "source_world_size": source_mesh["world_size"],
            "target_world_size": target_mesh["world_size"],
            "source_mesh_shape": source_mesh["shape"],
            "target_mesh_shape": target_mesh["shape"],
        },
        "plan": {
            "nodes": len(plan["nodes"]),
            "bytes": plan["total_bytes"],
        },
        "execution": {
            "bytes_moved": report["bytes_moved"],
            "duration_seconds": report["duration_seconds"],
            "retries": report["retries"],
        },
        "verification": report["verification"],
    }


def _print_kv(label: str, value: str, width: int = 14) -> None:
    print(f"  {label:<{width}} {value}")


def _print_migration_summary(summary: dict) -> None:
    topology = summary["topology"]
    verification = summary["verification"]
    execution = summary["execution"]
    print("Migration complete")
    _print_kv("checkpoint", summary["checkpoint"])
    _print_kv("output", summary["output"])
    _print_kv(
        "topology",
        f"{topology['source_world_size']} -> "
        f"{topology['target_world_size']} ranks",
    )
    _print_kv(
        "plan",
        f"{summary['plan']['nodes']} chunks, "
        f"{_format_bytes(summary['plan']['bytes'])}",
    )
    _print_kv(
        "moved",
        f"{_format_bytes(execution['bytes_moved'])} in "
        f"{_format_seconds(execution['duration_seconds'])}",
    )
    _print_kv(
        "verification",
        f"{verification.get('method', 'unknown')} "
        f"{'passed' if verification.get('passed') else 'FAILED'}",
    )
    _print_kv("work dir", summary["work_dir"])


def _print_demo_summary(result: dict) -> None:
    verification = result["verification"]
    target = Path(result["work_dir"]) / "target"
    print("Demo complete")
    _print_kv("work dir", result["work_dir"])
    _print_kv("target", target)
    _print_kv(
        "verification",
        f"{verification.get('method', 'unknown')} "
        f"{'passed' if verification.get('passed') else 'FAILED'}",
    )
    dcp = result.get("dcp")
    if dcp:
        _print_kv("dcp source", dcp["source"])
        _print_kv("dcp target", dcp["target"])
        _print_kv(
            "dcp load",
            f"source {dcp['source_readiness']['status']}, target "
            f"{dcp['target_readiness']['status']}",
        )
        _print_kv("target load", dcp["target_load"])
        _print_kv(
            "next",
            f"aevrel-target-load --checkpoint {dcp['target']} --rank 0 --json",
        )
    else:
        _print_kv("next", "aevrel demo --dcp --work-dir ./aevrel-demo-dcp")


def _print_doctor_summary(
    result: dict,
    *,
    checkpoint: str | None,
) -> None:
    if result["status"] == "ok":
        print("Ready to migrate." if checkpoint else "Environment ready.")
    else:
        print("Not ready.")
    for check in result["checks"]:
        marker = "ok" if check["ok"] else "FAIL"
        print(f"  [{'ok' if check['ok'] else 'FAIL':<4}] {check['name']:<22} {check['detail']}")
        if check.get("hint"):
            print(f"       {'':<22} {check['hint']}")
    if result["status"] == "ok" and not checkpoint:
        print("Next: aevrel demo")


def _format_bytes(value: int) -> str:
    size = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _format_seconds(value: float) -> str:
    if value < 60:
        return f"{value:.2f}s"
    minutes, seconds = divmod(int(round(value)), 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _make_progress_event_reporter():
    tty = sys.stderr.isatty()
    started = time.perf_counter()
    last_bucket = -1

    def report(event: dict) -> None:
        nonlocal last_bucket
        phase = event.get("phase")
        total_nodes = max(0, int(event.get("total_nodes", 0)))
        completed = max(0, int(event.get("completed_nodes", 0)))
        moved = max(0, int(event.get("bytes_moved", 0)))
        planned = max(0, int(event.get("bytes_planned", 0)))
        ratio = (
            min(1.0, moved / planned)
            if planned
            else min(1.0, completed / total_nodes)
            if total_nodes
            else 0.0
        )
        percent = int(round(ratio * 100))
        elapsed = max(0.001, time.perf_counter() - started)
        rate = moved / elapsed if moved else 0.0
        eta = (
            (planned - moved) / rate
            if rate > 0 and planned > moved
            else 0.0
        )

        if phase == "transfer":
            bucket = percent // 5
            if not tty and bucket == last_bucket and percent < 100:
                return
            last_bucket = bucket
            bar = _progress_bar(ratio, tty=tty)
            line = (
                f"transfer [{bar}] {percent:3d}% "
                f"{_format_bytes(moved)}/{_format_bytes(planned)} "
                f"{completed}/{total_nodes} nodes"
            )
            if rate:
                line += f" {_format_bytes(int(rate))}/s"
            if eta:
                line += f" ETA {_format_seconds(eta)}"
            if event.get("current_tensor"):
                line += (
                    f" {event['current_tensor']}"
                    f"/rank{event.get('current_rank', '?')}"
                )
            if tty:
                print(f"\r{line[:140]:<140}", end="", file=sys.stderr, flush=True)
            else:
                print(line, file=sys.stderr, flush=True)
            return

        if tty:
            print(file=sys.stderr)
        if phase == "verifying":
            print(
                f"verify [{_progress_bar(0.0, tty=tty)}] "
                f"checking {_format_bytes(moved)}",
                file=sys.stderr,
                flush=True,
            )
        elif phase == "verified":
            print(
                f"verified [{_progress_bar(1.0, tty=tty)}] "
                f"{_format_bytes(moved)}",
                file=sys.stderr,
                flush=True,
            )
        elif phase == "committing":
            print(
                f"commit [{_progress_bar(1.0, tty=tty)}] "
                f"{completed}/{total_nodes} nodes",
                file=sys.stderr,
                flush=True,
            )
        elif phase == "committed":
            print(
                f"committed [{_progress_bar(1.0, tty=tty)}] "
                f"{completed}/{total_nodes} nodes",
                file=sys.stderr,
                flush=True,
            )

    return report


def _progress_bar(ratio: float, *, tty: bool, width: int = 24) -> str:
    filled = max(0, min(width, int(round(ratio * width))))
    if tty:
        return "█" * filled + "░" * (width - filled)
    return "#" * filled + "-" * (width - filled)


def _load_json_object(path: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"could not read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"JSON document must be an object: {path}")
    return payload


def _write_json(payload: dict, path: str | None) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_rank_map(value: str) -> dict[int, int] | None:
    if not value.strip():
        return None
    result: dict[int, int] = {}
    for item in _parse_csv(value):
        try:
            source, target = item.split(":", 1)
            result[int(source)] = int(target)
        except ValueError as exc:
            raise ValueError(
                f"invalid rank map entry {item!r}; expected SOURCE:TARGET"
            ) from exc
    return result


def _parse_shape(value: str | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    return tuple(int(item) for item in value.split(",") if item.strip())


if __name__ == "__main__":
    raise SystemExit(main())
