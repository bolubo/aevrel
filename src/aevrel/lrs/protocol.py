"""LRS protocol primitives: ranks, shard ownership, leases, and quorum."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


class LrsError(RuntimeError):
    """Base class for LRS protocol violations."""


class StaleEpochError(LrsError):
    """A write carried an ownership epoch that is no longer current."""


class FencedError(LrsError):
    """A process tried to write after it was drained or fenced."""


class CoverageError(LrsError):
    """At least one logical shard has no primary owner."""


class QuorumError(LrsError):
    """A control-plane decision did not reach its required quorum."""


class TakeoverError(LrsError):
    """A source-death takeover cannot be completed safely."""


class DigestMismatchError(LrsError):
    """Source and target state digests differ at cutover."""


class Cluster(str, Enum):
    A = "A"
    B = "B"


class RankPhase(str, Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    IN_FLIGHT = "in_flight"
    ARRIVED = "arrived"
    FENCED = "fenced"
    DEAD = "dead"


class ShardPhase(str, Enum):
    OWNED = "owned"
    SHADOW = "shadow"
    COMMITTED = "committed"


@dataclass
class RankState:
    rank: int
    cluster: Cluster
    phase: RankPhase
    lease_epoch: int = 0
    lease_expires_step: int = -1
    source_fenced: bool = False

    def renew(self, step: int, ttl: int) -> None:
        if ttl < 1:
            raise ValueError("lease ttl must be positive")
        self.lease_epoch += 1
        self.lease_expires_step = step + ttl

    def lease_valid(self, step: int) -> bool:
        return self.lease_expires_step >= step

    def drain(self) -> None:
        if self.phase is not RankPhase.ACTIVE:
            raise LrsError(f"rank {self.rank} is not active")
        self.phase = RankPhase.DRAINING

    def mark_in_flight(self) -> None:
        if self.phase is not RankPhase.DRAINING:
            raise LrsError(f"rank {self.rank} is not draining")
        self.phase = RankPhase.IN_FLIGHT

    def activate(self, cluster: Cluster) -> None:
        if self.phase not in (RankPhase.IN_FLIGHT, RankPhase.ARRIVED):
            raise LrsError(f"rank {self.rank} cannot activate from {self.phase}")
        self.cluster = cluster
        self.phase = RankPhase.ACTIVE

    def fence_source(self) -> None:
        self.source_fenced = True


@dataclass
class ShardOwnership:
    shard_id: int
    primary: int
    epoch: int = 0
    shadow: int | None = None
    phase: ShardPhase = ShardPhase.OWNED

    def assert_write(self, owner: int, epoch: int) -> None:
        if self.shadow is not None and owner == self.shadow:
            raise FencedError(
                f"shard {self.shard_id} shadow {owner} cannot write before cutover"
            )
        if owner != self.primary:
            raise FencedError(
                f"shard {self.shard_id} is owned by {self.primary}, not {owner}"
            )
        if epoch != self.epoch:
            raise StaleEpochError(
                f"shard {self.shard_id} epoch is {self.epoch}, not {epoch}"
            )
        if self.phase is ShardPhase.COMMITTED:
            raise FencedError(f"shard {self.shard_id} is not writable")


class OwnershipMap:
    """Authoritative shard -> primary owner map with monotonic epochs."""

    def __init__(self, owners: Mapping[int, int]) -> None:
        if not owners:
            raise ValueError("ownership map must not be empty")
        if len(set(owners.values())) != len(owners):
            raise ValueError("initial shard owners must be distinct")
        self._shards = {
            shard_id: ShardOwnership(shard_id=shard_id, primary=owner)
            for shard_id, owner in sorted(owners.items())
        }

    def __len__(self) -> int:
        return len(self._shards)

    def shard_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._shards))

    def get(self, shard_id: int) -> ShardOwnership:
        try:
            return self._shards[shard_id]
        except KeyError as exc:
            raise LrsError(f"unknown shard {shard_id}") from exc

    def owner_of(self, shard_id: int) -> int:
        return self.get(shard_id).primary

    def assert_write(self, shard_id: int, owner: int, epoch: int) -> None:
        self.get(shard_id).assert_write(owner, epoch)

    def transfer_primary(
        self,
        shard_id: int,
        *,
        from_owner: int,
        to_owner: int,
        expected_epoch: int,
    ) -> int:
        record = self.get(shard_id)
        if record.primary != from_owner:
            raise FencedError(
                f"shard {shard_id} primary is {record.primary}, not {from_owner}"
            )
        if record.epoch != expected_epoch:
            raise StaleEpochError(
                f"shard {shard_id} epoch is {record.epoch}, not {expected_epoch}"
            )
        if record.shadow is not None and to_owner != record.shadow:
            raise LrsError(
                f"shard {shard_id} can only transfer to its shadow {record.shadow}"
            )
        record.primary = to_owner
        record.epoch += 1
        record.shadow = None
        record.phase = ShardPhase.OWNED
        return record.epoch

    def assign_shadow(self, shard_id: int, shadow: int) -> None:
        record = self.get(shard_id)
        if record.shadow is not None:
            raise LrsError(f"shard {shard_id} already has a shadow")
        if shadow == record.primary:
            raise LrsError("shadow must differ from primary")
        record.shadow = shadow
        record.phase = ShardPhase.SHADOW

    def force_takeover(self, shard_id: int, owner: int) -> int:
        """Move a shard to a committed B owner after source death."""
        record = self.get(shard_id)
        record.primary = owner
        record.epoch += 1
        record.shadow = None
        record.phase = ShardPhase.OWNED
        return record.epoch

    def coverage(self) -> frozenset[int]:
        return frozenset(
            shard_id
            for shard_id, record in self._shards.items()
            if record.primary is not None
        )

    def assert_coverage(self) -> None:
        expected = frozenset(self._shards)
        actual = self.coverage()
        if actual != expected:
            missing = sorted(expected - actual)
            raise CoverageError(f"shard coverage incomplete: missing {missing}")

    def assert_single_primary(self, known_ranks: Iterable[int]) -> None:
        known = frozenset(known_ranks)
        for shard_id, record in self._shards.items():
            if record.primary not in known:
                raise CoverageError(
                    f"shard {shard_id} has unknown primary {record.primary}"
                )
            if record.shadow is not None and record.shadow not in known:
                raise CoverageError(
                    f"shard {shard_id} has unknown shadow {record.shadow}"
                )

    def snapshot(self) -> dict[int, dict[str, int | str | None]]:
        return {
            shard_id: {
                "primary": record.primary,
                "epoch": record.epoch,
                "shadow": record.shadow,
                "phase": record.phase.value,
            }
            for shard_id, record in sorted(self._shards.items())
        }


@dataclass(frozen=True)
class QuorumDecision:
    members: frozenset[int]
    votes: frozenset[int]
    required: int

    @property
    def ok(self) -> bool:
        return len(self.votes) >= self.required


def commit_quorum(members: Iterable[int]) -> QuorumDecision:
    """A decision where every current member votes yes."""
    member_set = frozenset(members)
    if not member_set:
        raise QuorumError("quorum requires at least one member")
    required = len(member_set) // 2 + 1
    return QuorumDecision(members=member_set, votes=member_set, required=required)


def require_quorum(
    members: Iterable[int],
    votes: Iterable[int],
    *,
    required: int | None = None,
) -> QuorumDecision:
    member_set = frozenset(members)
    if not member_set:
        raise QuorumError("quorum requires at least one member")
    threshold = required if required is not None else len(member_set) // 2 + 1
    if threshold < 1:
        raise QuorumError("quorum threshold must be positive")
    decision = QuorumDecision(
        members=member_set,
        votes=frozenset(votes),
        required=threshold,
    )
    if not decision.ok:
        raise QuorumError(
            f"quorum not met: {len(decision.votes)}/{decision.required} votes"
        )
    return decision
