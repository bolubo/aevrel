"""LRS simulator: M1 2+2/F=1 and M2 W=8/F>1, CPU."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .protocol import (
    Cluster,
    CoverageError,
    DigestMismatchError,
    FencedError,
    LrsError,
    OwnershipMap,
    QuorumError,
    RankPhase,
    RankState,
    TakeoverError,
    require_quorum,
)
from .sampler import RankSampler
from .state import DeltaLog, InMemoryTransfer, ShardBlob
from .transport import DeltaSink
from .toy import (
    ToyConfig,
    ToyDataset,
    canonical_gradient,
    per_sample_gradients,
)


@dataclass(frozen=True)
class Migration:
    rank: int
    begin_step: int
    cutover_step: int

    def __post_init__(self) -> None:
        if self.rank < 0:
            raise ValueError("rank must be non-negative")
        if self.begin_step < 1:
            raise ValueError("begin_step must be at least 1")
        if self.cutover_step <= self.begin_step:
            raise ValueError("cutover_step must be after begin_step")


def _max_overlap(migrations: list[Migration]) -> int:
    events: list[tuple[int, int]] = []
    for migration in migrations:
        events.append((migration.begin_step, 1))
        events.append((migration.cutover_step + 1, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    current = 0
    maximum = 0
    for _, delta in events:
        current += delta
        maximum = max(maximum, current)
    return maximum


@dataclass(frozen=True)
class MigrationProof:
    rank: int
    shard_id: int
    begin_step: int
    cutover_step: int
    source_cluster: Cluster
    target_cluster: Cluster
    snapshot_step: int
    snapshot_digest: str
    live_cutover_digest: str
    target_cutover_digest: str
    temp_owner: int
    in_flight_at_cutover: int
    source_epoch: int
    cutover_epoch: int
    target_epoch: int
    coverage: tuple[int, ...]
    quorum_votes: tuple[int, ...]
    steps_lost: int
    global_stop: int
    timeline: tuple[tuple[str, int], ...]
    cutover_wait_seconds: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "shard_id": self.shard_id,
            "begin_step": self.begin_step,
            "cutover_step": self.cutover_step,
            "snapshot_step": self.snapshot_step,
            "source_cluster": self.source_cluster.value,
            "target_cluster": self.target_cluster.value,
            "snapshot_digest": self.snapshot_digest,
            "live_cutover_digest": self.live_cutover_digest,
            "target_cutover_digest": self.target_cutover_digest,
            "temp_owner": self.temp_owner,
            "in_flight_at_cutover": self.in_flight_at_cutover,
            "source_epoch": self.source_epoch,
            "cutover_epoch": self.cutover_epoch,
            "target_epoch": self.target_epoch,
            "coverage": list(self.coverage),
            "quorum_votes": list(self.quorum_votes),
            "steps_lost": self.steps_lost,
            "global_stop": self.global_stop,
            "timeline": [list(item) for item in self.timeline],
            "cutover_wait_seconds": self.cutover_wait_seconds,
        }


@dataclass(frozen=True)
class ShadowReplica:
    shard_id: int
    owner: int
    blob: ShardBlob


@dataclass(frozen=True)
class Takeover:
    step: int
    quorum: int | None = None

    def __post_init__(self) -> None:
        if self.step < 0:
            raise ValueError("takeover step must be non-negative")
        if self.quorum is not None and self.quorum < 1:
            raise ValueError("takeover quorum must be positive")


@dataclass(frozen=True)
class TakeoverProof:
    step: int
    degraded_world_size: int
    b_ranks: tuple[int, ...]
    coverage: tuple[int, ...]
    quorum_required: int
    quorum_votes: tuple[int, ...]
    migrated_shards: tuple[int, ...]
    shadow_shards: tuple[int, ...]
    digests: tuple[tuple[int, str], ...]
    steps_lost: int = 0
    global_stop: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "degraded_world_size": self.degraded_world_size,
            "b_ranks": list(self.b_ranks),
            "coverage": list(self.coverage),
            "quorum_required": self.quorum_required,
            "quorum_votes": list(self.quorum_votes),
            "migrated_shards": list(self.migrated_shards),
            "shadow_shards": list(self.shadow_shards),
            "digests": {
                str(shard_id): digest
                for shard_id, digest in self.digests
            },
            "steps_lost": self.steps_lost,
            "global_stop": self.global_stop,
        }


@dataclass
class _MigrationRuntime:
    spec: Migration
    shard_id: int
    temp_owner: int
    source_epoch: int
    target_epoch: int
    snapshot: ShardBlob
    delta_log: DeltaLog
    transfer: DeltaSink
    timeline: list[tuple[str, int]] = field(default_factory=list)


class LRSSimulator:
    """Deterministic LRS M1 simulator.

    The simulator models four logical ranks and four shards. Each rank owns
    one shard. A migration drains one rank, transfers its shard to a temporary
    owner on cluster A, streams a snapshot plus per-step deltas to the target,
    and promotes the target at a stable step.
    """

    def __init__(
        self,
        config: ToyConfig | None = None,
        *,
        sink_factory: Callable[[Migration], DeltaSink] | None = None,
    ) -> None:
        self.config = config or ToyConfig()
        self.dataset = ToyDataset(self.config)
        per_rank_dataset = (
            self.config.dataset_size // self.config.world_size
        )
        self.samplers = {
            rank: RankSampler(
                rank=rank,
                seed=self.config.seed + 1000 + rank,
                start=rank * per_rank_dataset,
                end=(rank + 1) * per_rank_dataset,
                batch_size=self.config.batch_per_rank,
            )
            for rank in range(self.config.world_size)
        }
        self.dtype = np.dtype(self.config.dtype)
        self.theta = np.zeros(self.config.param_count, dtype=self.dtype)
        self.momentum = np.zeros(self.config.param_count, dtype=self.dtype)
        self.step_index = 0
        self.loss_history: list[float] = []
        self.batch_history: list[tuple[int, ...]] = []
        self.assignment_history: list[dict[int, tuple[int, ...]]] = []
        self.ranks = {
            rank: RankState(
                rank=rank,
                cluster=Cluster.A,
                phase=RankPhase.ACTIVE,
            )
            for rank in range(self.config.world_size)
        }
        self.ownership = OwnershipMap(
            {
                shard_id: shard_id
                for shard_id in range(self.config.world_size)
            }
        )
        self.delta_logs = {
            shard_id: DeltaLog(shard_id=shard_id)
            for shard_id in range(self.config.world_size)
        }
        self.proofs: list[MigrationProof] = []
        self.takeover_proofs: list[TakeoverProof] = []
        self._shadows: dict[int, ShadowReplica] = {}
        self._runtimes: dict[int, _MigrationRuntime] = {}
        self._max_in_flight = 1
        self._max_lag: int | None = None
        self._step_delay = 0.0
        self.lag_history: list[int] = []
        self._sink_factory = sink_factory or (
            lambda spec: InMemoryTransfer()
        )

    @property
    def cluster_layout(self) -> dict[int, Cluster]:
        return {
            rank: state.cluster
            for rank, state in sorted(self.ranks.items())
        }

    @property
    def active_migrations(self) -> tuple[Migration, ...]:
        return tuple(
            runtime.spec
            for _, runtime in sorted(self._runtimes.items())
        )

    @property
    def active_migration(self) -> Migration | None:
        active = self.active_migrations
        return active[0] if len(active) == 1 else None

    def run(
        self,
        steps: int,
        migrations: list[Migration] | None = None,
        *,
        max_in_flight: int = 1,
        max_lag: int | None = None,
        step_delay: float = 0.0,
        takeovers: list[Takeover] | None = None,
    ) -> "LRSSimulator":
        if steps < self.step_index:
            raise ValueError("steps must not move backwards")
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be positive")
        if max_in_flight >= self.config.world_size:
            raise ValueError("max_in_flight must be less than world_size")
        if max_lag is not None and max_lag < 0:
            raise ValueError("max_lag must be non-negative")
        if step_delay < 0:
            raise ValueError("step_delay must be non-negative")
        self._max_in_flight = max_in_flight
        self._max_lag = max_lag
        self._step_delay = step_delay
        planned = sorted(
            migrations or [],
            key=lambda migration: (migration.begin_step, migration.rank),
        )
        self._validate_migrations(planned, steps, max_in_flight)
        planned_takeovers = sorted(
            takeovers or [],
            key=lambda takeover: takeover.step,
        )
        if len(planned_takeovers) > 1:
            raise ValueError("at most one takeover is supported per run")
        if planned_takeovers:
            takeover = planned_takeovers[0]
            if takeover.step < self.step_index:
                raise ValueError("takeover is in the past")
            if takeover.step >= steps:
                raise ValueError("takeover is outside the run")
            for migration in planned:
                if migration.cutover_step >= takeover.step:
                    raise LrsError(
                        "migration must finish before takeover"
                    )
        by_takeover = {
            takeover.step: takeover
            for takeover in planned_takeovers
        }
        by_begin: dict[int, list[Migration]] = defaultdict(list)
        by_cutover: dict[int, list[Migration]] = defaultdict(list)
        for migration in planned:
            by_begin[migration.begin_step].append(migration)
            by_cutover[migration.cutover_step].append(migration)
        for step in range(self.step_index, steps):
            if step in by_takeover:
                self._takeover(by_takeover[step])
            for migration in by_begin.get(step, []):
                reserved = {
                    other.rank
                    for other in planned
                    if other.rank != migration.rank
                    and other.begin_step <= migration.cutover_step
                    and other.cutover_step >= migration.begin_step
                }
                self._begin_migration(migration, reserved=reserved)
            self._run_step(step)
            for migration in sorted(
                by_cutover.get(step, []),
                key=lambda item: item.rank,
            ):
                self._cutover(migration)
            if self._step_delay:
                time.sleep(self._step_delay)
        if self._runtimes:
            raise LrsError("run ended with unfinished migrations")
        return self

    def write_as_source(
        self,
        rank: int,
        shard_id: int,
        *,
        epoch: int,
        theta,
        momentum,
    ) -> None:
        state = self.ranks[rank]
        if state.source_fenced or state.phase is not RankPhase.ACTIVE:
            raise FencedError(f"source rank {rank} is fenced")
        self.ownership.assert_write(shard_id, rank, epoch)
        window = self._slice(shard_id)
        self.theta[window] = theta
        self.momentum[window] = momentum

    def write_as_target(
        self,
        rank: int,
        shard_id: int,
        *,
        epoch: int,
        theta,
        momentum,
    ) -> None:
        state = self.ranks[rank]
        if state.phase is not RankPhase.ACTIVE or state.cluster is not Cluster.B:
            raise FencedError(f"target rank {rank} is not active on B")
        self.ownership.assert_write(shard_id, rank, epoch)
        window = self._slice(shard_id)
        self.theta[window] = theta
        self.momentum[window] = momentum

    def _validate_migrations(
        self,
        migrations: list[Migration],
        steps: int,
        max_in_flight: int,
    ) -> None:
        if not migrations:
            return
        if self._runtimes:
            raise LrsError("another migration is already active")
        seen_ranks: set[int] = set()
        for migration in migrations:
            if migration.rank in seen_ranks:
                raise LrsError(f"rank {migration.rank} migrates more than once")
            seen_ranks.add(migration.rank)
            if migration.rank >= self.config.world_size:
                raise LrsError(f"rank {migration.rank} is outside the world")
            if migration.begin_step < self.step_index:
                raise LrsError("migration begins in the past")
            if migration.cutover_step >= steps:
                raise LrsError("migration cutover is outside the run")
        if _max_overlap(migrations) > max_in_flight:
            raise LrsError(
                f"migration schedule exceeds max_in_flight={max_in_flight}"
            )

    def _begin_migration(
        self,
        spec: Migration,
        *,
        reserved: set[int] | None = None,
    ) -> None:
        if len(self._runtimes) >= self._max_in_flight:
            raise LrsError(
                f"max_in_flight={self._max_in_flight} reached"
            )
        if spec.rank in self._runtimes:
            raise LrsError(f"rank {spec.rank} is already migrating")
        if spec.rank not in self.ranks:
            raise LrsError(f"rank {spec.rank} is outside the world")
        if spec.begin_step != self.step_index:
            raise LrsError("migration begins at the wrong step")
        source = self.ranks[spec.rank]
        if source.phase is not RankPhase.ACTIVE or source.cluster is not Cluster.A:
            raise LrsError(f"rank {spec.rank} is not active on cluster A")
        shard_id = spec.rank
        record = self.ownership.get(shard_id)
        if record.primary != spec.rank:
            raise LrsError(f"rank {spec.rank} does not own shard {shard_id}")
        for runtime in self._runtimes.values():
            if runtime.temp_owner == spec.rank:
                raise LrsError(
                    f"rank {spec.rank} is temporary owner for shard "
                    f"{runtime.shard_id}; cannot drain"
                )
        source_epoch = record.epoch
        reserved_ranks = set(reserved or ())
        migrating = set(self._runtimes) | {spec.rank}
        source_candidates = [
            rank
            for rank in sorted(self.ranks)
            if rank not in migrating
            and rank not in reserved_ranks
            and self.ranks[rank].phase is RankPhase.ACTIVE
            and self.ranks[rank].cluster is Cluster.A
        ]
        candidates = source_candidates or [
            rank
            for rank in sorted(self.ranks)
            if rank not in migrating
            and rank not in reserved_ranks
            and self.ranks[rank].phase is RankPhase.ACTIVE
            and self.ranks[rank].cluster is Cluster.B
        ]
        if not candidates:
            raise LrsError("no active rank for temporary ownership")
        load = {rank: 0 for rank in candidates}
        for runtime in self._runtimes.values():
            if runtime.temp_owner in load:
                load[runtime.temp_owner] += 1
        temp_owner = min(candidates, key=lambda rank: (load[rank], rank))
        source.drain()
        source.mark_in_flight()
        target_epoch = self.ownership.transfer_primary(
            shard_id,
            from_owner=spec.rank,
            to_owner=temp_owner,
            expected_epoch=source_epoch,
        )
        self.ownership.assign_shadow(shard_id, spec.rank)
        window = self._slice(shard_id)
        snapshot = ShardBlob.capture(
            step=spec.begin_step - 1,
            shard_id=shard_id,
            epoch=target_epoch,
            theta=self.theta[window],
            momentum=self.momentum[window],
            aux=self.samplers[shard_id].capture(),
        )
        transfer = self._sink_factory(spec)
        transfer.send(snapshot)
        self._runtimes[spec.rank] = _MigrationRuntime(
            spec=spec,
            shard_id=shard_id,
            temp_owner=temp_owner,
            source_epoch=source_epoch,
            target_epoch=target_epoch,
            snapshot=snapshot,
            delta_log=self.delta_logs[shard_id],
            transfer=transfer,
            timeline=[
                ("begin", spec.begin_step),
                ("snapshot", spec.begin_step - 1),
                ("transfer_to_temp", spec.begin_step),
                ("temp_epoch", target_epoch),
            ],
        )

    def replicate_shadow(self, shard_id: int, owner: int) -> ShadowReplica:
        """Copy a shard's stable-step state to a B standby without cutover."""
        if self.step_index < 1:
            raise LrsError("no stable step to shadow")
        if shard_id not in self.ownership.shard_ids():
            raise LrsError(f"unknown shard {shard_id}")
        record = self.ownership.get(shard_id)
        primary = self.ranks.get(record.primary)
        if primary is None or primary.cluster is not Cluster.A:
            raise LrsError(f"shard {shard_id} is no longer on A")
        if owner in self.ranks:
            state = self.ranks[owner]
            if state.cluster is not Cluster.B:
                raise LrsError(f"shadow owner {owner} must be on B")
        else:
            self.ranks[owner] = RankState(
                rank=owner,
                cluster=Cluster.B,
                phase=RankPhase.ARRIVED,
            )
        last = self.step_index - 1
        window = self._slice(shard_id)
        blob = ShardBlob.capture(
            step=last,
            shard_id=shard_id,
            epoch=record.epoch,
            theta=self.theta[window],
            momentum=self.momentum[window],
            aux=self.samplers[shard_id].capture(),
        )
        shadow = ShadowReplica(
            shard_id=shard_id,
            owner=owner,
            blob=blob,
        )
        self._shadows[shard_id] = shadow
        return shadow

    def _shard_blob(self, shard_id: int, step: int) -> ShardBlob:
        record = self.ownership.get(shard_id)
        window = self._slice(shard_id)
        return ShardBlob.capture(
            step=step,
            shard_id=shard_id,
            epoch=record.epoch,
            theta=self.theta[window],
            momentum=self.momentum[window],
            aux=self.samplers[shard_id].capture(),
        )

    def _takeover(self, spec: Takeover) -> None:
        if self._runtimes:
            raise TakeoverError(
                "in-flight migrations are not supported at takeover"
            )
        if spec.step != self.step_index:
            raise LrsError("takeover is not at a stable step boundary")
        if spec.step < 1:
            raise LrsError("takeover requires a completed step")
        last = spec.step - 1
        owners: dict[int, int] = {}
        migrated: list[int] = []
        shadowed: list[int] = []
        digests: list[tuple[int, str]] = []
        for shard_id in self.ownership.shard_ids():
            record = self.ownership.get(shard_id)
            primary = self.ranks.get(record.primary)
            if (
                primary is not None
                and primary.cluster is Cluster.B
                and primary.phase is RankPhase.ACTIVE
            ):
                owners[shard_id] = record.primary
                migrated.append(shard_id)
                digests.append(
                    (shard_id, self._shard_blob(shard_id, last).digest)
                )
                continue
            shadow = self._shadows.get(shard_id)
            if shadow is None:
                raise CoverageError(
                    f"shard {shard_id} has no committed B copy"
                )
            if shadow.blob.step != last:
                raise CoverageError(
                    f"shadow for shard {shard_id} is at step "
                    f"{shadow.blob.step}, expected {last}"
                )
            source = self._shard_blob(shard_id, last)
            if shadow.blob.digest != source.digest:
                raise DigestMismatchError(
                    f"shadow for shard {shard_id} does not match "
                    f"the stable step"
                )
            owners[shard_id] = shadow.owner
            shadowed.append(shard_id)
            digests.append((shard_id, shadow.blob.digest))
        b_ranks = sorted(set(owners.values()))
        for owner in b_ranks:
            state = self.ranks.get(owner)
            if state is None or state.cluster is not Cluster.B:
                raise TakeoverError(
                    f"takeover owner {owner} is not a B rank"
                )
        required = (
            spec.quorum
            if spec.quorum is not None
            else self.config.world_size // 2 + 1
        )
        if len(b_ranks) < required:
            raise QuorumError(
                f"takeover quorum not met: {len(b_ranks)}/{required}"
            )
        for rank, state in self.ranks.items():
            if state.cluster is Cluster.A:
                state.phase = RankPhase.DEAD
                state.source_fenced = True
        for rank in b_ranks:
            state = self.ranks[rank]
            if state.phase is not RankPhase.ACTIVE:
                state.activate(Cluster.B)
            state.renew(spec.step, ttl=1)
        for shard_id, owner in owners.items():
            self.ownership.force_takeover(shard_id, owner)
        per_rank_dataset = (
            self.config.dataset_size // self.config.world_size
        )
        for shard_id in shadowed:
            shadow = self._shadows.get(shard_id)
            if shadow is None:
                continue
            self.samplers[shard_id] = RankSampler.from_state(
                rank=shard_id,
                start=shard_id * per_rank_dataset,
                end=(shard_id + 1) * per_rank_dataset,
                batch_size=self.config.batch_per_rank,
                data=shadow.blob.aux,
            )
        self.ownership.assert_coverage()
        self.ownership.assert_single_primary(self.ranks)
        self._shadows.clear()
        self.takeover_proofs.append(
            TakeoverProof(
                step=spec.step,
                degraded_world_size=len(b_ranks),
                b_ranks=tuple(b_ranks),
                coverage=tuple(sorted(owners)),
                quorum_required=required,
                quorum_votes=tuple(b_ranks),
                migrated_shards=tuple(sorted(migrated)),
                shadow_shards=tuple(sorted(shadowed)),
                digests=tuple(sorted(digests)),
                steps_lost=0,
                global_stop=0,
            )
        )

    def _cutover(self, spec: Migration) -> None:
        runtime = self._runtimes.get(spec.rank)
        if runtime is None or runtime.spec != spec:
            raise LrsError("cutover does not match the active migration")
        if self.step_index != spec.cutover_step + 1:
            raise LrsError("cutover is not at a stable step boundary")
        if runtime.snapshot.step != spec.begin_step - 1:
            raise LrsError("snapshot is not at the begin boundary")
        window = self._slice(runtime.shard_id)
        live = ShardBlob.capture(
            step=spec.cutover_step,
            shard_id=runtime.shard_id,
            epoch=runtime.target_epoch,
            theta=self.theta[window],
            momentum=self.momentum[window],
            aux=self.samplers[runtime.shard_id].capture(),
        )
        replayed = runtime.delta_log.replay(
            runtime.snapshot,
            spec.cutover_step,
        )
        replayed.assert_matches(live)
        started = time.monotonic()
        runtime.transfer.wait_for(spec.cutover_step)
        cutover_wait_seconds = time.monotonic() - started
        target_blob = runtime.transfer.acked_blob(spec.cutover_step)
        target_blob.assert_matches(live)
        target_digest = target_blob.digest
        self.ownership.assert_coverage()
        self.ownership.assert_single_primary(self.ranks)
        members = frozenset(self._active_ranks()) | {spec.rank}
        require_quorum(members, members)
        target_epoch = self.ownership.transfer_primary(
            runtime.shard_id,
            from_owner=runtime.temp_owner,
            to_owner=spec.rank,
            expected_epoch=runtime.target_epoch,
        )
        target = self.ranks[spec.rank]
        target.activate(Cluster.B)
        target.fence_source()
        per_rank_dataset = (
            self.config.dataset_size // self.config.world_size
        )
        self.samplers[spec.rank] = RankSampler.from_state(
            rank=spec.rank,
            start=spec.rank * per_rank_dataset,
            end=(spec.rank + 1) * per_rank_dataset,
            batch_size=self.config.batch_per_rank,
            data=target_blob.aux,
        )
        proof = MigrationProof(
            rank=spec.rank,
            shard_id=runtime.shard_id,
            begin_step=spec.begin_step,
            cutover_step=spec.cutover_step,
            source_cluster=Cluster.A,
            target_cluster=Cluster.B,
            snapshot_step=runtime.snapshot.step,
            snapshot_digest=runtime.snapshot.digest,
            live_cutover_digest=live.digest,
            target_cutover_digest=target_digest,
            temp_owner=runtime.temp_owner,
            in_flight_at_cutover=len(self._runtimes),
            source_epoch=runtime.source_epoch,
            cutover_epoch=runtime.target_epoch,
            target_epoch=target_epoch,
            coverage=tuple(sorted(self.ownership.coverage())),
            quorum_votes=tuple(sorted(members)),
            steps_lost=0,
            global_stop=0,
            timeline=tuple(
                runtime.timeline
                + [
                    ("cutover", spec.cutover_step),
                    ("fence", spec.cutover_step),
                ]
            ),
            cutover_wait_seconds=cutover_wait_seconds,
        )
        self.proofs.append(proof)
        runtime.transfer.close()
        self._runtimes.pop(spec.rank, None)

    def _run_step(self, step: int) -> None:
        if step != self.step_index:
            raise LrsError("step is not contiguous")
        self.ownership.assert_coverage()
        self.ownership.assert_single_primary(self.ranks)
        active = self._active_ranks()
        if not active:
            raise LrsError("no active ranks")
        for rank in active:
            self.ranks[rank].renew(step, ttl=1)
        rank_batches = {
            rank: self.samplers[rank].draw()
            for rank in range(self.config.world_size)
        }
        batch = tuple(
            sorted(
                sample_id
                for samples in rank_batches.values()
                for sample_id in samples
            )
        )
        assignments = self._assign_samples(batch, active)
        self._assert_assignment(batch, assignments)
        gradient = self._gradient(batch, assignments)
        momentum_coefficient = self.dtype.type(self.config.momentum)
        learning_rate = self.dtype.type(self.config.learning_rate)
        for shard_id in self.ownership.shard_ids():
            record = self.ownership.get(shard_id)
            window = self._slice(shard_id)
            next_momentum = (
                momentum_coefficient * self.momentum[window]
                + gradient[window]
            )
            next_theta = (
                self.theta[window]
                - learning_rate * next_momentum
            )
            self._write_shard(
                shard_id,
                owner=record.primary,
                epoch=record.epoch,
                theta_new=next_theta,
                momentum_new=next_momentum,
                aux=self.samplers[shard_id].capture(),
                step=step,
            )
        if self._max_lag is not None and self._runtimes:
            for runtime in self._runtimes.values():
                runtime.transfer.wait_for(step - self._max_lag)
            lag = max(
                step - runtime.transfer.last_acked_step
                for runtime in self._runtimes.values()
            )
            self.lag_history.append(lag)
            if lag > self._max_lag:
                raise LrsError(
                    f"target lag {lag} exceeds max_lag={self._max_lag}"
                )
        self.loss_history.append(self.dataset.loss(self.theta))
        self.batch_history.append(batch)
        self.assignment_history.append(assignments)
        self.ownership.assert_coverage()
        self.ownership.assert_single_primary(self.ranks)
        self.step_index += 1

    def _write_shard(
        self,
        shard_id: int,
        *,
        owner: int,
        epoch: int,
        theta_new: np.ndarray,
        momentum_new: np.ndarray,
        step: int,
        aux: bytes = b"",
    ) -> None:
        self.ownership.assert_write(shard_id, owner, epoch)
        state = self.ranks[owner]
        if state.phase is not RankPhase.ACTIVE:
            raise FencedError(f"owner rank {owner} is not active")
        if not state.lease_valid(step):
            raise FencedError(f"owner rank {owner} lease expired at step {step}")
        window = self._slice(shard_id)
        self.theta[window] = theta_new
        self.momentum[window] = momentum_new
        blob = ShardBlob.capture(
            step=step,
            shard_id=shard_id,
            epoch=epoch,
            theta=theta_new,
            momentum=momentum_new,
            aux=aux,
        )
        self.delta_logs[shard_id].append(blob)
        for runtime in self._runtimes.values():
            if runtime.shard_id != shard_id:
                continue
            runtime.transfer.send(blob)

    def _active_ranks(self) -> tuple[int, ...]:
        return tuple(
            rank
            for rank, state in sorted(self.ranks.items())
            if state.phase is RankPhase.ACTIVE
        )

    def _assign_samples(
        self,
        batch: tuple[int, ...],
        active: tuple[int, ...],
    ) -> dict[int, tuple[int, ...]]:
        assignments: dict[int, list[int]] = {
            rank: [] for rank in active
        }
        for index, sample_id in enumerate(batch):
            assignments[active[index % len(active)]].append(sample_id)
        return {
            rank: tuple(samples)
            for rank, samples in assignments.items()
        }

    def _gradient(
        self,
        batch: tuple[int, ...],
        assignments: dict[int, tuple[int, ...]],
    ) -> np.ndarray:
        if self.config.reduction == "canonical":
            return canonical_gradient(
                self.dataset,
                self.theta,
                batch,
            ).astype(self.dtype, copy=False)
        sample_gradients = per_sample_gradients(
            self.dataset,
            self.theta,
            batch,
        )
        per_rank: dict[int, np.ndarray] = {}
        for rank in sorted(assignments):
            rank_sum = np.zeros(self.config.param_count, dtype=self.dtype)
            for sample_id in assignments[rank]:
                rank_sum = (rank_sum + sample_gradients[sample_id]).astype(
                    self.dtype,
                    copy=False,
                )
            per_rank[rank] = rank_sum
        total = np.zeros(self.config.param_count, dtype=self.dtype)
        for rank in sorted(per_rank):
            total = (total + per_rank[rank]).astype(
                self.dtype,
                copy=False,
            )
        return total

    @staticmethod
    def _assert_assignment(
        batch: tuple[int, ...],
        assignments: dict[int, tuple[int, ...]],
    ) -> None:
        assigned = [
            sample_id
            for samples in assignments.values()
            for sample_id in samples
        ]
        if sorted(assigned) != sorted(batch):
            raise LrsError("work redistribution did not cover the global batch")
        if len(assigned) != len(set(assigned)):
            raise LrsError("work redistribution assigned a sample twice")

    def _slice(self, shard_id: int) -> slice:
        start = shard_id * self.config.shard_size
        return slice(start, start + self.config.shard_size)
