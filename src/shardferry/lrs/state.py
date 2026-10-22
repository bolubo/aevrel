"""Stable-step shard snapshots, delta logs, and the M1 in-memory link."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from .protocol import DigestMismatchError, LrsError


def _digest(values: tuple[float, ...], aux: bytes = b"") -> str:
    payload = struct.pack(f"<{len(values)}d", *values)
    if aux:
        payload += b"|aux|" + aux
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ShardBlob:
    """A complete shard state at one stable step.

    M1 deliberately stores the full shard state as the delta. This keeps the
    semantic proof free of compression/partial-update bugs; later milestones
    can replace the payload with a parameter diff without changing the log
    contract.
    """

    step: int
    shard_id: int
    epoch: int
    theta: tuple[float, ...]
    momentum: tuple[float, ...]
    aux: bytes = b""

    def __post_init__(self) -> None:
        if self.step < 0:
            raise ValueError("step must be non-negative")
        if self.shard_id < 0:
            raise ValueError("shard_id must be non-negative")
        if self.epoch < 0:
            raise ValueError("epoch must be non-negative")
        if len(self.theta) != len(self.momentum):
            raise ValueError("theta and momentum must have equal length")
        if not isinstance(self.aux, bytes):
            raise ValueError("aux must be bytes")

    @property
    def digest(self) -> str:
        return _digest(self.theta + self.momentum, self.aux)

    def to_bytes(self) -> bytes:
        header = struct.pack("<QQQ", self.step, self.shard_id, self.epoch)
        aux_header = struct.pack("<I", len(self.aux))
        body = struct.pack(
            f"<{len(self.theta) + len(self.momentum)}d",
            *(self.theta + self.momentum),
        )
        return header + aux_header + body + self.aux

    @classmethod
    def capture(
        cls,
        *,
        step: int,
        shard_id: int,
        epoch: int,
        theta,
        momentum,
        aux: bytes = b"",
    ) -> "ShardBlob":
        return cls(
            step=int(step),
            shard_id=int(shard_id),
            epoch=int(epoch),
            theta=tuple(float(value) for value in theta),
            momentum=tuple(float(value) for value in momentum),
            aux=bytes(aux),
        )

    def assert_matches(self, other: "ShardBlob") -> None:
        if self.digest != other.digest:
            raise DigestMismatchError(
                f"shard {self.shard_id} digest mismatch at step "
                f"{self.step}/{other.step}: {self.digest} != {other.digest}"
            )


@dataclass
class DeltaLog:
    """Per-shard, contiguous state history."""

    shard_id: int
    records: list[ShardBlob] = field(default_factory=list)

    def append(self, blob: ShardBlob) -> None:
        if blob.shard_id != self.shard_id:
            raise LrsError("delta log shard mismatch")
        if self.records:
            last = self.records[-1]
            if blob.step != last.step + 1:
                raise LrsError(
                    f"delta log steps must be contiguous: "
                    f"{last.step} -> {blob.step}"
                )
            if blob.epoch < last.epoch:
                raise LrsError("delta epoch must not go backwards")
        self.records.append(blob)

    def replay(self, snapshot: ShardBlob, upto_step: int) -> ShardBlob:
        if snapshot.shard_id != self.shard_id:
            raise LrsError("snapshot shard mismatch")
        state = snapshot
        for record in self.records:
            if record.step <= snapshot.step:
                continue
            if record.step > upto_step:
                break
            if record.step != state.step + 1:
                raise LrsError(
                    f"delta gap at step {state.step + 1} "
                    f"for shard {self.shard_id}"
                )
            state = record
        if state.step != upto_step:
            raise LrsError(
                f"delta log ended at {state.step}, expected {upto_step}"
            )
        return state

    def records_between(self, start: int, end: int) -> tuple[ShardBlob, ...]:
        return tuple(
            record for record in self.records if start <= record.step <= end
        )


class InMemoryTransfer:
    """M1/M2 transport: ordered, lossless, in-process, immediate ack."""

    def __init__(self) -> None:
        self._blobs: dict[int, ShardBlob] = {}
        self._last_acked_step = -1
        self._last_acked_digest: str | None = None
        self._closed = False

    def send(self, blob: ShardBlob) -> None:
        if self._closed:
            raise LrsError("in-memory transport is closed")
        self._blobs[blob.step] = blob
        if blob.step > self._last_acked_step:
            self._last_acked_step = blob.step
            self._last_acked_digest = blob.digest

    def wait_for(self, step: int) -> None:
        if self._last_acked_step < step:
            raise LrsError(f"in-memory transport has not acked step {step}")

    def acked_digest(self, step: int) -> str:
        blob = self._blobs.get(step)
        if blob is None:
            raise LrsError(f"in-memory transport has no blob for step {step}")
        return blob.digest

    def acked_blob(self, step: int) -> ShardBlob:
        blob = self._blobs.get(step)
        if blob is None or self._last_acked_step < step:
            raise LrsError(
                f"in-memory transport has not acked step {step}"
            )
        return blob

    @property
    def last_acked_step(self) -> int:
        return self._last_acked_step

    @property
    def last_acked_digest(self) -> str | None:
        return self._last_acked_digest

    def close(self) -> None:
        self._closed = True
