"""On-demand LRS worker used by the Go control plane.

The worker owns the data-plane lifecycle behind ``/v1/live/reconcile``. The
current backend is a deterministic CPU contract backend: it advances the
stable-step ownership state, produces a proof artifact, and never claims GPU
collective continuity. The Go controller remains authoritative for CR status
and target-ready gating.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .protocol import LrsError
from .sim import LRSSimulator, Migration
from .toy import ToyConfig


class LiveWorkerError(RuntimeError):
    """A request cannot be reconciled safely."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LiveMove:
    rank: int
    begin_tick: int
    cutover_tick: int


@dataclass(frozen=True)
class LivePlan:
    key: str
    namespace: str
    name: str
    uid: str
    generation: int
    world_size: int
    max_in_flight: int
    state_mode: str
    state_root: str
    state_providers: tuple[str, ...]
    moves: tuple[LiveMove, ...]
    quorum: int
    total_ticks: int


class _SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, plan: LivePlan) -> Path:
        key = f"{plan.namespace}/{plan.name}/{plan.uid}/{plan.generation}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def load_tick(self, plan: LivePlan) -> int:
        path = self._path(plan)
        if not path.is_file():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveWorkerError(
                f"could not read live worker state: {exc}",
                status=500,
            ) from exc
        tick = int(payload.get("tick", 0))
        if tick < 0:
            raise LiveWorkerError("live worker state has a negative tick", status=500)
        return tick

    def save_tick(self, plan: LivePlan, tick: int) -> None:
        path = self._path(plan)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"tick": tick}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)


class LiveWorker:
    """Reconcile a live migration one stable-step tick at a time."""

    backend = "cpu-contract"

    def __init__(self, state_dir: str | Path = ".aevrel/live-worker") -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self._store = _SessionStore(self.state_dir / "sessions")
        self._proof_dir = self.state_dir / "proofs"
        self._proof_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def reconcile(self, request: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan(request)
        with self._lock:
            tick = self._store.load_tick(plan)
            previous = request.get("previousStatus") or {}
            previous_phase = str(previous.get("cutoverPhase", ""))
            if (
                tick == 0
                and previous_phase in {"target-validating", "succeeded"}
            ):
                tick = plan.total_ticks
            observation = self._observation(plan, tick, previous)
            if tick < plan.total_ticks:
                self._store.save_tick(plan, tick + 1)
            return observation

    def _plan(self, request: dict[str, Any]) -> LivePlan:
        if request.get("apiVersion") != "aevrel.dev/v1":
            raise LiveWorkerError("unsupported apiVersion")
        if request.get("kind") != "LiveRankSplitReconcile":
            raise LiveWorkerError("unsupported kind")
        migration = request.get("migration") or {}
        namespace = str(migration.get("namespace", ""))
        name = str(migration.get("name", ""))
        if not namespace or not name:
            raise LiveWorkerError("migration namespace/name is required")
        spec = request.get("spec") or {}
        if spec.get("mode") != "live-rank-split":
            raise LiveWorkerError("spec.mode must be live-rank-split")
        source = spec.get("source") or {}
        target = spec.get("target") or {}
        world_size = int(source.get("worldSize", 0))
        target_world_size = int(target.get("worldSize", 0))
        if world_size < 2 or target_world_size != world_size:
            raise LiveWorkerError(
                "live worker requires equal source/target worldSize >= 2"
            )
        live = spec.get("live") or {}
        max_in_flight = int(live.get("maxInFlight", 0))
        if max_in_flight < 1 or max_in_flight >= world_size:
            raise LiveWorkerError(
                "live.maxInFlight must be in [1, source.worldSize)"
            )
        state_mode = str(live.get("stateMode") or "L4")
        if state_mode not in {"L3", "L4"}:
            raise LiveWorkerError("live.stateMode must be L3 or L4")
        state_root = str(live.get("stateRoot", ""))
        if not state_root:
            raise LiveWorkerError("live.stateRoot is required")
        providers = live.get("stateProviders") or []
        if not providers or any(not isinstance(item, str) or not item for item in providers):
            raise LiveWorkerError("live.stateProviders must be non-empty strings")
        rank_plan = live.get("rankPlan") or {}
        raw_ranks = rank_plan.get("ranks")
        if raw_ranks:
            ranks = sorted({int(rank) for rank in raw_ranks})
        else:
            ranks = list(range(world_size))
        if any(rank < 0 or rank >= world_size for rank in ranks):
            raise LiveWorkerError("live.rankPlan.ranks contains an out-of-range rank")
        groups = (len(ranks) + max_in_flight - 1) // max_in_flight
        moves: list[LiveMove] = []
        for index, rank in enumerate(ranks):
            begin = 1 + 3 * (index // max_in_flight)
            moves.append(
                LiveMove(
                    rank=rank,
                    begin_tick=begin,
                    cutover_tick=begin + 2,
                )
            )
        cutover = live.get("cutover") or {}
        quorum = int(cutover.get("quorum", 0) or (world_size // 2 + 1))
        if quorum < 1 or quorum > world_size:
            raise LiveWorkerError("live.cutover.quorum is out of range")
        generation = int(migration.get("generation", 0) or 0)
        return LivePlan(
            key=f"{namespace}/{name}",
            namespace=namespace,
            name=name,
            uid=str(migration.get("uid", "")),
            generation=generation,
            world_size=world_size,
            max_in_flight=max_in_flight,
            state_mode=state_mode,
            state_root=state_root,
            state_providers=tuple(providers),
            moves=tuple(moves),
            quorum=quorum,
            total_ticks=1 + 3 * groups,
        )

    def _observation(
        self,
        plan: LivePlan,
        tick: int,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        if tick == 0:
            return {
                "phase": "preflight",
                "inFlight": [],
                "coverage": [],
                "quorum": plan.quorum,
                "stateComplete": False,
                "stateRoot": plan.state_root,
                "proofRef": "",
                "message": "LRS CPU contract worker preflight",
                "ranks": [],
            }
        if tick >= plan.total_ticks:
            proof_ref = str(previous.get("proofRef", ""))
            if not proof_ref:
                proof_ref = self._write_proof(plan)
            return {
                "phase": "succeeded",
                "inFlight": [],
                "coverage": list(range(plan.world_size)),
                "quorum": plan.quorum,
                "stateComplete": True,
                "stateRoot": plan.state_root,
                "proofRef": proof_ref,
                "message": (
                    "LRS CPU contract complete; target-ready gate remains "
                    "if configured"
                ),
                "ranks": [
                    {
                        "rank": rank,
                        "phase": "arrived",
                        "owner": rank,
                        "epoch": 1,
                    }
                    for rank in sorted(move.rank for move in plan.moves)
                ],
            }

        in_flight = [
            move.rank
            for move in plan.moves
            if move.begin_tick <= tick < move.cutover_tick
        ]
        settled = [
            move.rank
            for move in plan.moves
            if tick >= move.cutover_tick
        ]
        all_settled = len(settled) == len(plan.moves)
        if all_settled:
            phase = "cutover"
        elif in_flight:
            phase = "following"
        else:
            phase = "cutover"
        ranks = []
        for move in plan.moves:
            if tick < move.begin_tick:
                rank_phase = "active"
            elif tick < move.cutover_tick:
                rank_phase = "in_flight"
            else:
                rank_phase = "arrived"
            ranks.append(
                {
                    "rank": move.rank,
                    "phase": rank_phase,
                    "owner": move.rank,
                    "epoch": 0 if rank_phase == "active" else 1,
                }
            )
        return {
            "phase": phase,
            "inFlight": in_flight,
            "coverage": list(range(plan.world_size)),
            "quorum": plan.quorum,
            "stateComplete": False,
            "stateRoot": plan.state_root,
            "proofRef": "",
            "message": (
                f"{len(in_flight)} rank(s) in flight; "
                f"{len(settled)}/{len(plan.moves)} moved ranks settled"
            ),
            "ranks": ranks,
        }

    def _write_proof(self, plan: LivePlan) -> str:
        config = ToyConfig(
            world_size=plan.world_size,
            shard_size=1,
            param_count=plan.world_size,
            batch_per_rank=1,
            dataset_size=max(32, plan.world_size * 4),
        )
        migrations = [
            Migration(
                rank=move.rank,
                begin_step=move.begin_tick,
                cutover_step=move.cutover_tick,
            )
            for move in plan.moves
        ]
        baseline = LRSSimulator(config).run(plan.total_ticks)
        try:
            migrated = LRSSimulator(config).run(
                plan.total_ticks,
                migrations,
                max_in_flight=plan.max_in_flight,
            )
        except LrsError as exc:
            raise LiveWorkerError(
                f"LRS CPU proof failed: {exc}",
                status=500,
            ) from exc
        checks = {
            "theta_bitwise": (
                migrated.theta.tobytes() == baseline.theta.tobytes()
            ),
            "momentum_bitwise": (
                migrated.momentum.tobytes()
                == baseline.momentum.tobytes()
            ),
            "loss_bitwise": migrated.loss_history == baseline.loss_history,
            "batch_bitwise": (
                migrated.batch_history == baseline.batch_history
            ),
            "step_count": (
                migrated.step_index
                == baseline.step_index
                == plan.total_ticks
            ),
            "all_targets_joined": migrated.active_migrations == (),
            "proof_count": len(migrated.proofs) == len(plan.moves),
            "proof_digests": all(
                proof.live_cutover_digest == proof.target_cutover_digest
                for proof in migrated.proofs
            ),
            "coverage": all(
                proof.coverage == tuple(range(plan.world_size))
                for proof in migrated.proofs
            ),
            "steps_lost_zero": all(
                proof.steps_lost == 0 for proof in migrated.proofs
            ),
            "global_stop_zero": all(
                proof.global_stop == 0 for proof in migrated.proofs
            ),
        }
        if not all(checks.values()):
            raise LiveWorkerError(
                "LRS CPU proof checks failed: "
                + ", ".join(
                    name for name, ok in checks.items() if not ok
                ),
                status=500,
            )
        proofs = [proof.to_dict() for proof in migrated.proofs]
        payload = {
            "schema": "aevrel.lrs.worker-proof.v1",
            "backend": self.backend,
            "migration": {
                "namespace": plan.namespace,
                "name": plan.name,
                "uid": plan.uid,
                "generation": plan.generation,
            },
            "world_size": plan.world_size,
            "max_in_flight": plan.max_in_flight,
            "state_mode": plan.state_mode,
            "state_root": plan.state_root,
            "state_providers": list(plan.state_providers),
            "coverage": list(range(plan.world_size)),
            "quorum": plan.quorum,
            "state_complete": True,
            "steps_lost": max(
                proof["steps_lost"] for proof in proofs
            ),
            "global_stop": max(
                proof["global_stop"] for proof in proofs
            ),
            "live_collective": False,
            "checks": checks,
            "baseline_theta_digest": _digest(baseline.theta),
            "migrated_theta_digest": _digest(migrated.theta),
            "proofs": proofs,
            "created_at": time.time(),
        }
        key = f"{plan.namespace}/{plan.name}/{plan.uid}/{plan.generation}"
        path = self._proof_dir / (
            hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        )
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return "file://" + str(path)


def _digest(values) -> str:
    return hashlib.sha256(values.tobytes()).hexdigest()


class LiveWorkerServer:
    """Threaded HTTP facade used by the Go liveclient."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 9460,
        token: str = "",
        state_dir: str | Path = ".aevrel/live-worker",
    ) -> None:
        self.worker = LiveWorker(state_dir)
        self.token = token
        self._server = ThreadingHTTPServer(
            (host, int(port)),
            self._handler_class(),
        )
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:
                if urlsplit(self.path).path != "/health":
                    self.send_error(404)
                    return
                self._json(200, {
                    "ok": True,
                    "backend": owner.worker.backend,
                    "features": ["live-rank-split"],
                })

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/v1/live/reconcile":
                    self.send_error(404)
                    return
                if owner.token:
                    expected = "Bearer " + owner.token
                    actual = self.headers.get("Authorization", "")
                    if not hmac.compare_digest(expected, actual):
                        self._json(401, {"error": "unauthorized"})
                        return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 16 * 1024 * 1024:
                        raise ValueError("invalid request length")
                    request = json.loads(self.rfile.read(length))
                    if not isinstance(request, dict):
                        raise ValueError("request must be a JSON object")
                    observation = owner.worker.reconcile(request)
                except LiveWorkerError as exc:
                    self._json(exc.status, {"error": str(exc)})
                    return
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, observation)

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
