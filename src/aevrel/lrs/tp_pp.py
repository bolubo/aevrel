"""TP=2 / PP=2 toy model and LRS reconfiguration scenarios (M5)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .protocol import (
    Cluster,
    CoverageError,
    DigestMismatchError,
    LrsError,
    OwnershipMap,
    RankPhase,
    RankState,
)
from .state import DeltaLog, InMemoryTransfer, ShardBlob


@dataclass(frozen=True)
class TPPPConfig:
    d_in: int = 4
    hidden: int = 4
    d_out: int = 1
    tp: int = 2
    pp: int = 2
    batch_size: int = 8
    dataset_size: int = 64
    seed: int = 7
    learning_rate: float = 0.02
    momentum: float = 0.9
    init_scale: float = 0.1

    @property
    def world_size(self) -> int:
        return self.tp * self.pp

    def __post_init__(self) -> None:
        if self.tp < 2 or self.pp < 2:
            raise ValueError("tp and pp must be at least 2")
        if self.hidden % self.tp != 0:
            raise ValueError("hidden must be divisible by tp")
        if self.dataset_size < self.batch_size:
            raise ValueError("dataset is too small for one batch")


@dataclass(frozen=True)
class CollectiveRecord:
    step: int
    pp: int
    op: str
    logical: tuple[int, ...]
    owners: tuple[int, ...]
    tp_epoch: int
    pp_epoch: int


@dataclass(frozen=True)
class PipelineRecord:
    step: int
    direction: str
    from_pp: int
    to_pp: int
    from_owners: tuple[int, ...]
    to_owners: tuple[int, ...]
    pp_epoch: int


@dataclass(frozen=True)
class TPMigration:
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


@dataclass(frozen=True)
class TPMigrationProof:
    rank: int
    begin_step: int
    cutover_step: int
    temp_owner: int
    tp_epoch: int
    pp_epoch: int
    snapshot_digest: str
    live_digest: str
    target_digest: str
    collective_owners_after: tuple[int, ...]
    steps_lost: int = 0
    global_stop: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "begin_step": self.begin_step,
            "cutover_step": self.cutover_step,
            "temp_owner": self.temp_owner,
            "tp_epoch": self.tp_epoch,
            "pp_epoch": self.pp_epoch,
            "snapshot_digest": self.snapshot_digest,
            "live_digest": self.live_digest,
            "target_digest": self.target_digest,
            "collective_owners_after": list(
                self.collective_owners_after
            ),
            "steps_lost": self.steps_lost,
            "global_stop": self.global_stop,
        }


@dataclass(frozen=True)
class PPStageProof:
    pp: int
    step: int
    logical_ranks: tuple[int, ...]
    old_owners: tuple[int, ...]
    new_owners: tuple[int, ...]
    tp_epoch: int
    pp_epoch: int
    digests: tuple[tuple[int, str], ...]
    steps_lost: int = 0
    global_stop: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pp": self.pp,
            "step": self.step,
            "logical_ranks": list(self.logical_ranks),
            "old_owners": list(self.old_owners),
            "new_owners": list(self.new_owners),
            "tp_epoch": self.tp_epoch,
            "pp_epoch": self.pp_epoch,
            "digests": {
                str(rank): digest
                for rank, digest in self.digests
            },
            "steps_lost": self.steps_lost,
            "global_stop": self.global_stop,
        }


@dataclass
class _TPRuntime:
    spec: TPMigration
    temp_owner: int
    source_epoch: int
    target_epoch: int
    snapshot: ShardBlob
    delta_log: DeltaLog
    transfer: InMemoryTransfer
    target_state: ShardBlob


class TPPPBaseline:
    """Full-model baseline for the TP/PP toy."""

    def __init__(
        self,
        config: TPPPConfig,
        x: np.ndarray,
        y: np.ndarray,
        w1: np.ndarray,
        w2: np.ndarray,
    ) -> None:
        self.config = config
        self.x = x
        self.y = y
        self.w1 = w1.copy()
        self.w2 = w2.copy()
        self.m1 = np.zeros_like(self.w1)
        self.m2 = np.zeros_like(self.w2)
        self._rng = np.random.default_rng(config.seed + 1)
        self._batches: dict[int, tuple[int, ...]] = {}
        self.step_index = 0
        self.loss_history: list[float] = []

    def _batch(self, step: int) -> tuple[int, ...]:
        if step not in self._batches:
            if step != len(self._batches):
                raise ValueError("batches must be requested in order")
            chosen = self._rng.choice(
                self.config.dataset_size,
                size=self.config.batch_size,
                replace=False,
            )
            self._batches[step] = tuple(int(value) for value in chosen)
        return self._batches[step]

    def run(self, steps: int) -> "TPPPBaseline":
        for step in range(self.step_index, steps):
            batch = self._batch(step)
            xb = self.x[list(batch)]
            yb = self.y[list(batch)]
            h = xb @ self.w1
            a = np.maximum(h, 0.0)
            y = a @ self.w2
            residual = y - yb
            loss = float(np.mean(residual * residual))
            dy = 2.0 * residual / len(batch)
            dw2 = a.T @ dy
            da = dy @ self.w2.T
            dh = da * (h > 0.0)
            dw1 = xb.T @ dh
            self.m1 = self.config.momentum * self.m1 + dw1
            self.w1 = self.w1 - self.config.learning_rate * self.m1
            self.m2 = self.config.momentum * self.m2 + dw2
            self.w2 = self.w2 - self.config.learning_rate * self.m2
            self.loss_history.append(loss)
            self.step_index += 1
        return self


class TPPPSimulator:
    """A 2x2 TP/PP toy with rank migration and stage remapping."""

    def __init__(self, config: TPPPConfig | None = None) -> None:
        self.config = config or TPPPConfig()
        rng = np.random.default_rng(self.config.seed)
        self.x = rng.normal(
            size=(self.config.dataset_size, self.config.d_in)
        )
        w1_true = rng.normal(
            size=(self.config.d_in, self.config.hidden)
        )
        w2_true = rng.normal(
            size=(self.config.hidden, self.config.d_out)
        )
        noise = rng.normal(
            scale=0.01,
            size=(self.config.dataset_size, self.config.d_out),
        )
        self.y = np.maximum(self.x @ w1_true, 0.0) @ w2_true + noise
        self.w1_init = rng.normal(
            scale=self.config.init_scale,
            size=(self.config.d_in, self.config.hidden),
        )
        self.w2_init = rng.normal(
            scale=self.config.init_scale,
            size=(self.config.hidden, self.config.d_out),
        )
        h = self.config.hidden // self.config.tp
        self.shards: dict[int, np.ndarray] = {
            0: self.w1_init[:, :h].flatten().copy(),
            1: self.w1_init[:, h:].flatten().copy(),
            2: self.w2_init[:h, :].flatten().copy(),
            3: self.w2_init[h:, :].flatten().copy(),
        }
        self.momentum: dict[int, np.ndarray] = {
            rank: np.zeros_like(shard)
            for rank, shard in self.shards.items()
        }
        self.ranks: dict[int, RankState] = {
            rank: RankState(
                rank=rank,
                cluster=Cluster.A,
                phase=RankPhase.ACTIVE,
            )
            for rank in range(self.config.world_size)
        }
        self.ownership = OwnershipMap(
            {
                rank: rank
                for rank in range(self.config.world_size)
            }
        )
        self.delta_logs = {
            rank: DeltaLog(shard_id=rank)
            for rank in range(self.config.world_size)
        }
        self.tp_epoch = {0: 0, 1: 0}
        self.pp_epoch = {0: 0, 1: 0}
        self.collective_log: list[CollectiveRecord] = []
        self.pipeline_log: list[PipelineRecord] = []
        self.tp_proofs: list[TPMigrationProof] = []
        self.pp_proofs: list[PPStageProof] = []
        self.tp_migration: _TPRuntime | None = None
        self.pp_shadows: dict[int, dict[int, object]] = {}
        self._rng = np.random.default_rng(self.config.seed + 1)
        self._batches: dict[int, tuple[int, ...]] = {}
        self.step_index = 0
        self.loss_history: list[float] = []

    def tp_of(self, rank: int) -> int:
        return rank % self.config.tp

    def pp_of(self, rank: int) -> int:
        return rank // self.config.tp

    def stage_ranks(self, pp: int) -> tuple[int, ...]:
        return tuple(
            pp * self.config.tp + tp
            for tp in range(self.config.tp)
        )

    def stage_owners(self, pp: int) -> tuple[int, ...]:
        return tuple(
            self.ownership.owner_of(rank)
            for rank in self.stage_ranks(pp)
        )

    def _batch(self, step: int) -> tuple[int, ...]:
        if step not in self._batches:
            if step != len(self._batches):
                raise ValueError("batches must be requested in order")
            chosen = self._rng.choice(
                self.config.dataset_size,
                size=self.config.batch_size,
                replace=False,
            )
            self._batches[step] = tuple(int(value) for value in chosen)
        return self._batches[step]

    def reconstruct(self) -> tuple[np.ndarray, np.ndarray]:
        h = self.config.hidden // self.config.tp
        w1 = np.concatenate(
            [
                self.shards[0].reshape(self.config.d_in, h),
                self.shards[1].reshape(self.config.d_in, h),
            ],
            axis=1,
        )
        w2 = np.concatenate(
            [
                self.shards[2].reshape(h, self.config.d_out),
                self.shards[3].reshape(h, self.config.d_out),
            ],
            axis=0,
        )
        return w1, w2

    def run(self, steps: int) -> "TPPPSimulator":
        if steps < self.step_index:
            raise ValueError("steps must not move backwards")
        for step in range(self.step_index, steps):
            self._step(step)
            runtime = self.tp_migration
            if runtime is not None and step == runtime.spec.cutover_step:
                self._cutover_tp(step)
        return self

    def _step(self, step: int) -> None:
        for state in self.ranks.values():
            if state.phase is RankPhase.ACTIVE:
                state.renew(step, ttl=1)
        batch = self._batch(step)
        xb = self.x[list(batch)]
        yb = self.y[list(batch)]
        h = self.config.hidden // self.config.tp
        w1_0 = self.shards[0].reshape(self.config.d_in, h)
        w1_1 = self.shards[1].reshape(self.config.d_in, h)
        w2_0 = self.shards[2].reshape(h, self.config.d_out)
        w2_1 = self.shards[3].reshape(h, self.config.d_out)

        h_full = self._all_gather(
            0,
            {0: xb @ w1_0, 1: xb @ w1_1},
            step,
        )
        a = np.maximum(h_full, 0.0)
        self._log_pipeline(step, "forward", 0, 1)

        y = self._all_reduce(
            1,
            {2: a[:, :h] @ w2_0, 3: a[:, h:] @ w2_1},
            step,
        )
        residual = y - yb
        loss = float(np.mean(residual * residual))
        dy = 2.0 * residual / len(batch)
        g2 = a[:, :h].T @ dy
        g3 = a[:, h:].T @ dy
        da = self._all_gather(
            1,
            {2: dy @ w2_0.T, 3: dy @ w2_1.T},
            step,
        )
        self._log_pipeline(step, "backward", 1, 0)
        dh = da * (h_full > 0.0)
        grads = {
            0: xb.T @ dh[:, :h],
            1: xb.T @ dh[:, h:],
            2: g2,
            3: g3,
        }
        for rank in range(self.config.world_size):
            next_momentum = (
                self.config.momentum * self.momentum[rank]
                + grads[rank].flatten()
            )
            next_shard = (
                self.shards[rank]
                - self.config.learning_rate * next_momentum
            )
            owner = self.ownership.owner_of(rank)
            epoch = self.ownership.get(rank).epoch
            self._write_shard(
                rank,
                owner=owner,
                epoch=epoch,
                theta=next_shard,
                momentum=next_momentum,
                step=step,
            )
        self.loss_history.append(loss)
        self.step_index += 1

    def _all_gather(
        self,
        pp: int,
        parts: dict[int, np.ndarray],
        step: int,
    ) -> np.ndarray:
        logical = self.stage_ranks(pp)
        if set(parts) != set(logical):
            raise LrsError(f"missing TP parts for stage {pp}")
        owners = self.stage_owners(pp)
        for owner in owners:
            state = self.ranks.get(owner)
            if state is None or state.phase is not RankPhase.ACTIVE:
                raise LrsError(
                    f"TP owner {owner} is not active at step {step}"
                )
        result = np.concatenate(
            [parts[rank] for rank in logical],
            axis=1,
        )
        self.collective_log.append(
            CollectiveRecord(
                step=step,
                pp=pp,
                op="all_gather",
                logical=logical,
                owners=owners,
                tp_epoch=self.tp_epoch[pp],
                pp_epoch=self.pp_epoch[pp],
            )
        )
        return result

    def _all_reduce(
        self,
        pp: int,
        parts: dict[int, np.ndarray],
        step: int,
    ) -> np.ndarray:
        logical = self.stage_ranks(pp)
        if set(parts) != set(logical):
            raise LrsError(f"missing TP parts for stage {pp}")
        owners = self.stage_owners(pp)
        for owner in owners:
            state = self.ranks.get(owner)
            if state is None or state.phase is not RankPhase.ACTIVE:
                raise LrsError(
                    f"TP owner {owner} is not active at step {step}"
                )
        result = np.zeros_like(parts[logical[0]])
        for rank in logical:
            result = result + parts[rank]
        self.collective_log.append(
            CollectiveRecord(
                step=step,
                pp=pp,
                op="all_reduce",
                logical=logical,
                owners=owners,
                tp_epoch=self.tp_epoch[pp],
                pp_epoch=self.pp_epoch[pp],
            )
        )
        return result

    def _log_pipeline(
        self,
        step: int,
        direction: str,
        from_pp: int,
        to_pp: int,
    ) -> None:
        self.pipeline_log.append(
            PipelineRecord(
                step=step,
                direction=direction,
                from_pp=from_pp,
                to_pp=to_pp,
                from_owners=self.stage_owners(from_pp),
                to_owners=self.stage_owners(to_pp),
                pp_epoch=self.pp_epoch[to_pp],
            )
        )

    def _write_shard(
        self,
        shard_id: int,
        *,
        owner: int,
        epoch: int,
        theta: np.ndarray,
        momentum: np.ndarray,
        step: int,
    ) -> None:
        self.ownership.assert_write(shard_id, owner, epoch)
        state = self.ranks.get(owner)
        if state is None or state.phase is not RankPhase.ACTIVE:
            raise LrsError(f"owner {owner} is not active")
        if not state.lease_valid(step):
            raise LrsError(f"owner {owner} lease expired at step {step}")
        self.shards[shard_id] = theta
        self.momentum[shard_id] = momentum
        blob = ShardBlob.capture(
            step=step,
            shard_id=shard_id,
            epoch=epoch,
            theta=theta,
            momentum=momentum,
        )
        self.delta_logs[shard_id].append(blob)
        runtime = self.tp_migration
        if runtime is not None and runtime.spec.rank == shard_id:
            runtime.transfer.send(blob)
            runtime.target_state = blob

    def begin_tp_migration(
        self,
        rank: int,
        *,
        cutover_step: int,
    ) -> TPMigration:
        if self.tp_migration is not None:
            raise LrsError("another TP migration is already active")
        if rank not in self.ranks:
            raise LrsError(f"unknown rank {rank}")
        if self.step_index < 1:
            raise LrsError("TP migration requires a completed step")
        if cutover_step <= self.step_index:
            raise LrsError("cutover must be after the begin step")
        source = self.ranks[rank]
        if source.phase is not RankPhase.ACTIVE or source.cluster is not Cluster.A:
            raise LrsError(f"rank {rank} is not active on A")
        stage = self.stage_ranks(self.pp_of(rank))
        others = [item for item in stage if item != rank]
        if not others:
            raise LrsError("no TP peer for temporary ownership")
        temp_owner = others[0]
        peer = self.ranks.get(temp_owner)
        if peer is None or peer.phase is not RankPhase.ACTIVE:
            raise LrsError(f"TP peer {temp_owner} is not active")
        record = self.ownership.get(rank)
        if record.primary != rank:
            raise LrsError(f"rank {rank} does not own its shard")
        source_epoch = record.epoch
        spec = TPMigration(
            rank=rank,
            begin_step=self.step_index,
            cutover_step=cutover_step,
        )
        source.drain()
        source.mark_in_flight()
        target_epoch = self.ownership.transfer_primary(
            rank,
            from_owner=rank,
            to_owner=temp_owner,
            expected_epoch=source_epoch,
        )
        self.ownership.assign_shadow(rank, rank)
        last = self.step_index - 1
        blob = ShardBlob.capture(
            step=last,
            shard_id=rank,
            epoch=target_epoch,
            theta=self.shards[rank],
            momentum=self.momentum[rank],
        )
        transfer = InMemoryTransfer()
        transfer.send(blob)
        self.tp_migration = _TPRuntime(
            spec=spec,
            temp_owner=temp_owner,
            source_epoch=source_epoch,
            target_epoch=target_epoch,
            snapshot=blob,
            delta_log=self.delta_logs[rank],
            transfer=transfer,
            target_state=blob,
        )
        return spec

    def _cutover_tp(self, step: int) -> None:
        runtime = self.tp_migration
        if runtime is None or step != runtime.spec.cutover_step:
            raise LrsError("TP cutover does not match the active migration")
        if self.step_index != step + 1:
            raise LrsError("TP cutover is not at a stable boundary")
        rank = runtime.spec.rank
        live = ShardBlob.capture(
            step=step,
            shard_id=rank,
            epoch=runtime.target_epoch,
            theta=self.shards[rank],
            momentum=self.momentum[rank],
        )
        runtime.delta_log.replay(
            runtime.snapshot,
            step,
        ).assert_matches(live)
        runtime.target_state.assert_matches(live)
        self.ownership.transfer_primary(
            rank,
            from_owner=runtime.temp_owner,
            to_owner=rank,
            expected_epoch=runtime.target_epoch,
        )
        target = self.ranks[rank]
        target.activate(Cluster.B)
        target.fence_source()
        pp = self.pp_of(rank)
        self.tp_epoch[pp] += 1
        self.tp_proofs.append(
            TPMigrationProof(
                rank=rank,
                begin_step=runtime.spec.begin_step,
                cutover_step=runtime.spec.cutover_step,
                temp_owner=runtime.temp_owner,
                tp_epoch=self.tp_epoch[pp],
                pp_epoch=self.pp_epoch[pp],
                snapshot_digest=runtime.snapshot.digest,
                live_digest=live.digest,
                target_digest=runtime.target_state.digest,
                collective_owners_after=self.stage_owners(pp),
                steps_lost=0,
                global_stop=0,
            )
        )
        runtime.transfer.close()
        self.tp_migration = None

    def shadow_pp_stage(self, pp: int) -> None:
        if pp not in (0, 1):
            raise LrsError(f"unknown pipeline stage {pp}")
        if self.step_index < 1:
            raise LrsError("PP shadow requires a completed step")
        if pp in self.pp_shadows:
            raise LrsError(f"stage {pp} already has shadows")
        if self.tp_migration is not None:
            if self.pp_of(self.tp_migration.spec.rank) == pp:
                raise LrsError("stage has an active TP migration")
        logical = self.stage_ranks(pp)
        last = self.step_index - 1
        shadows: dict[int, object] = {}
        for index, rank in enumerate(logical):
            owner = self.config.world_size + index
            if owner not in self.ranks:
                self.ranks[owner] = RankState(
                    rank=owner,
                    cluster=Cluster.B,
                    phase=RankPhase.ARRIVED,
                )
            elif self.ranks[owner].cluster is not Cluster.B:
                raise LrsError(f"standby {owner} is not on B")
            blob = ShardBlob.capture(
                step=last,
                shard_id=rank,
                epoch=self.ownership.get(rank).epoch,
                theta=self.shards[rank],
                momentum=self.momentum[rank],
            )
            shadows[rank] = ShadowEntry(
                shard_id=rank,
                owner=owner,
                blob=blob,
            )
        self.pp_shadows[pp] = shadows

    def cutover_pp_stage(self, pp: int) -> None:
        shadows = self.pp_shadows.pop(pp, None)
        if shadows is None:
            raise LrsError(f"stage {pp} has no shadows")
        logical = self.stage_ranks(pp)
        if set(shadows) != set(logical):
            raise LrsError("PP shadow set does not match the stage")
        last = self.step_index - 1
        digests: list[tuple[int, str]] = []
        for rank in logical:
            shadow = shadows[rank]
            if shadow.blob.step != last:
                raise CoverageError(
                    f"shadow for rank {rank} is at step "
                    f"{shadow.blob.step}, expected {last}"
                )
            source = ShardBlob.capture(
                step=last,
                shard_id=rank,
                epoch=self.ownership.get(rank).epoch,
                theta=self.shards[rank],
                momentum=self.momentum[rank],
            )
            if shadow.blob.digest != source.digest:
                raise DigestMismatchError(
                    f"shadow for rank {rank} does not match the stable step"
                )
            digests.append((rank, shadow.blob.digest))
        old_owners = tuple(
            self.ownership.owner_of(rank) for rank in logical
        )
        for rank in logical:
            state = self.ranks[rank]
            if state.cluster is not Cluster.A:
                raise LrsError(f"rank {rank} is not on A")
            state.phase = RankPhase.DEAD
            state.source_fenced = True
        for rank in logical:
            shadow = shadows[rank]
            owner = shadow.owner
            state = self.ranks[owner]
            state.activate(Cluster.B)
            state.renew(self.step_index, ttl=1)
            self.ownership.force_takeover(rank, owner)
        self.tp_epoch[pp] += 1
        self.pp_epoch[pp] += 1
        self.pp_proofs.append(
            PPStageProof(
                pp=pp,
                step=self.step_index,
                logical_ranks=logical,
                old_owners=old_owners,
                new_owners=tuple(
                    self.ownership.owner_of(rank) for rank in logical
                ),
                tp_epoch=self.tp_epoch[pp],
                pp_epoch=self.pp_epoch[pp],
                digests=tuple(digests),
                steps_lost=0,
                global_stop=0,
            )
        )


@dataclass(frozen=True)
class ShadowEntry:
    shard_id: int
    owner: int
    blob: ShardBlob
