from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None


MARKER_NAME = ".aevrel-complete"
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
_GEAR = tuple(
    int.from_bytes(hashlib.sha256(bytes([value])).digest()[:8], "big")
    for value in range(256)
)
_MASK64 = (1 << 64) - 1


@dataclass(frozen=True)
class ChunkingPolicy:
    algorithm: str = "fixed"
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    min_bytes: int = 32 * 1024
    avg_bytes: int = 128 * 1024
    max_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if self.algorithm not in {"fixed", "cdc", "file"}:
            raise ValueError(f"unsupported chunking algorithm: {self.algorithm}")
        if self.algorithm == "fixed":
            if self.chunk_bytes < 1:
                raise ValueError("chunk_bytes must be positive")
            return
        if self.min_bytes < 1:
            raise ValueError("min_bytes must be positive")
        if not self.min_bytes <= self.avg_bytes <= self.max_bytes:
            raise ValueError("chunking sizes must satisfy min <= avg <= max")

    @classmethod
    def fixed(cls, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ChunkingPolicy:
        return cls(algorithm="fixed", chunk_bytes=chunk_bytes)

    @classmethod
    def content_defined(
        cls,
        *,
        min_bytes: int = 32 * 1024,
        avg_bytes: int = 128 * 1024,
        max_bytes: int = 512 * 1024,
    ) -> ChunkingPolicy:
        return cls(
            algorithm="cdc",
            min_bytes=min_bytes,
            avg_bytes=avg_bytes,
            max_bytes=max_bytes,
        )

    @classmethod
    def file(cls) -> ChunkingPolicy:
        return cls(algorithm="file")

    def offsets(self, data: bytes) -> tuple[int, ...]:
        if self.algorithm == "file":
            return (0, len(data))
        if self.algorithm == "fixed":
            if not data:
                return (0,)
            return tuple(range(0, len(data), self.chunk_bytes)) + (len(data),)
        return _content_defined_offsets(
            data,
            min_bytes=self.min_bytes,
            avg_bytes=self.avg_bytes,
            max_bytes=self.max_bytes,
        )

    def iter_chunks(self, stream) -> Iterable[bytes]:
        if self.algorithm == "file":
            data = stream.read()
            if data:
                yield data
            return
        if self.algorithm == "fixed":
            while True:
                block = stream.read(self.chunk_bytes)
                if not block:
                    return
                yield block
            return
        yield from _iter_content_defined_chunks(
            stream,
            min_bytes=self.min_bytes,
            avg_bytes=self.avg_bytes,
            max_bytes=self.max_bytes,
        )


def _content_defined_offsets(
    data: bytes,
    *,
    min_bytes: int,
    avg_bytes: int,
    max_bytes: int,
) -> tuple[int, ...]:
    if not data:
        return (0,)
    if len(data) <= min_bytes:
        return (0, len(data))
    mask_bits = max(1, avg_bytes.bit_length() - 1)
    mask = (1 << mask_bits) - 1
    offsets = [0]
    start = 0
    while start < len(data):
        end = min(start + max_bytes, len(data))
        cut = end
        rolling = 0
        for position in range(start, end):
            rolling = ((rolling << 1) + _GEAR[data[position]]) & _MASK64
            length = position - start + 1
            if length >= min_bytes and (rolling & mask) == 0:
                cut = position + 1
                break
        if cut <= start:
            cut = end
        offsets.append(cut)
        start = cut
    return tuple(offsets)


def _iter_content_defined_chunks(
    stream,
    *,
    min_bytes: int,
    avg_bytes: int,
    max_bytes: int,
) -> Iterable[bytes]:
    mask_bits = max(1, avg_bytes.bit_length() - 1)
    mask = (1 << mask_bits) - 1
    buffer = bytearray()
    eof = False
    while True:
        while not eof and len(buffer) < max_bytes:
            block = stream.read(max_bytes)
            if not block:
                eof = True
                break
            buffer.extend(block)
        if not buffer:
            return
        limit = min(len(buffer), max_bytes)
        cut = None
        rolling = 0
        for position in range(limit):
            rolling = ((rolling << 1) + _GEAR[buffer[position]]) & _MASK64
            length = position + 1
            if length >= min_bytes and (rolling & mask) == 0:
                cut = length
                break
        if cut is None:
            if eof:
                cut = len(buffer)
            elif len(buffer) >= max_bytes:
                cut = max_bytes
            else:
                block = stream.read(max_bytes)
                if not block:
                    eof = True
                else:
                    buffer.extend(block)
                continue
        chunk = bytes(buffer[:cut])
        del buffer[:cut]
        if chunk:
            yield chunk


class CDPError(RuntimeError):
    """Raised when a checkpoint version or manifest is invalid."""


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise CDPError(f"could not write {path}: {exc}") from exc


class TargetLock:
    """Cross-process lock for one target root's fence and current pointer."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / ".aevrel" / "target.lock"
        self._stream = None

    def __enter__(self) -> "TargetLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        if fcntl is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            self._stream.seek(0)
            if self._stream.read(1) == b"":
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._stream is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._stream.close()
            self._stream = None


class TargetLock:
    """Cross-process lock for one target root's fence and current pointer."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / ".aevrel" / "target.lock"
        self._stream = None

    def __enter__(self) -> "TargetLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        if fcntl is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            self._stream.seek(0)
            if self._stream.read(1) == b"":
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._stream is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._stream.close()
            self._stream = None


@dataclass(frozen=True)
class VersionMarker:
    version: str
    version_id: str
    job_uid: str
    step: int
    parent_id: str | None
    metadata_hash: str
    created_at: str
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "version_id": self.version_id,
            "job_uid": self.job_uid,
            "step": self.step,
            "parent_id": self.parent_id,
            "metadata_hash": self.metadata_hash,
            "created_at": self.created_at,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> VersionMarker:
        try:
            marker = cls(
                version=str(payload["version"]),
                version_id=str(payload["version_id"]),
                job_uid=str(payload["job_uid"]),
                step=int(payload["step"]),
                parent_id=(
                    str(payload["parent_id"])
                    if payload.get("parent_id") is not None
                    else None
                ),
                metadata_hash=str(payload["metadata_hash"]),
                created_at=str(payload["created_at"]),
                complete=bool(payload["complete"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CDPError(f"invalid checkpoint version marker: {exc}") from exc
        if not marker.version_id:
            raise CDPError("checkpoint version_id must not be empty")
        if not marker.job_uid:
            raise CDPError("checkpoint job_uid must not be empty")
        if not marker.metadata_hash.startswith("sha256:"):
            raise CDPError("checkpoint metadata_hash must be a sha256 digest")
        return marker


@dataclass(frozen=True)
class VersionDescriptor:
    version_id: str
    path: Path
    marker: VersionMarker

    @property
    def complete(self) -> bool:
        return self.marker.complete


def read_marker(path: str | Path) -> VersionMarker:
    marker_path = Path(path)
    if marker_path.is_dir():
        marker_path = marker_path / MARKER_NAME
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CDPError(f"could not read checkpoint marker {marker_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CDPError(f"checkpoint marker must be an object: {marker_path}")
    return VersionMarker.from_dict(payload)


def write_marker(path: str | Path, marker: VersionMarker) -> Path:
    marker_path = Path(path)
    if marker_path.is_dir():
        marker_path = marker_path / MARKER_NAME
    _atomic_write(
        marker_path,
        json.dumps(marker.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    return marker_path


def scan_versions(
    root: str | Path,
    *,
    include_incomplete: bool = False,
) -> list[VersionDescriptor]:
    source = Path(root).resolve()
    if not source.is_dir():
        raise CDPError(f"checkpoint root not found: {source}")
    versions: list[VersionDescriptor] = []
    seen: set[str] = set()
    for candidate in sorted(source.iterdir()):
        if not candidate.is_dir() or candidate.name.startswith("."):
            continue
        marker_path = candidate / MARKER_NAME
        if not marker_path.is_file():
            continue
        marker = read_marker(marker_path)
        if marker.version_id in seen:
            raise CDPError(f"duplicate checkpoint version: {marker.version_id}")
        seen.add(marker.version_id)
        descriptor = VersionDescriptor(
            version_id=marker.version_id,
            path=candidate,
            marker=marker,
        )
        if marker.complete or include_incomplete:
            versions.append(descriptor)
    return sorted(
        versions,
        key=lambda item: (item.marker.created_at, item.version_id),
    )


@dataclass(frozen=True)
class ChunkRef:
    digest: str
    size: int
    file: str
    offset: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size": self.size,
            "file": self.file,
            "offset": self.offset,
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChunkRef:
        try:
            ref = cls(
                digest=str(payload["digest"]),
                size=int(payload["size"]),
                file=str(payload["file"]),
                offset=int(payload["offset"]),
                length=int(payload["length"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CDPError(f"invalid chunk reference: {exc}") from exc
        if not ref.digest.startswith("sha256:"):
            raise CDPError("chunk digest must be a sha256 digest")
        if ref.size < 0 or ref.offset < 0 or ref.length < 0:
            raise CDPError("chunk sizes must be non-negative")
        if ref.length != ref.size:
            raise CDPError("chunk length must equal chunk size")
        return ref


@dataclass(frozen=True)
class VersionManifest:
    version: str
    version_id: str
    parent_id: str | None
    metadata_hash: str
    created_at: str
    root: str
    chunks: tuple[ChunkRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "version_id": self.version_id,
            "parent_id": self.parent_id,
            "metadata_hash": self.metadata_hash,
            "created_at": self.created_at,
            "root": self.root,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> VersionManifest:
        try:
            manifest = cls(
                version=str(payload["version"]),
                version_id=str(payload["version_id"]),
                parent_id=(
                    str(payload["parent_id"])
                    if payload.get("parent_id") is not None
                    else None
                ),
                metadata_hash=str(payload["metadata_hash"]),
                created_at=str(payload["created_at"]),
                root=str(payload["root"]),
                chunks=tuple(
                    ChunkRef.from_dict(chunk)
                    for chunk in payload["chunks"]
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CDPError(f"invalid checkpoint manifest: {exc}") from exc
        expected = manifest_root(
            version_id=manifest.version_id,
            parent_id=manifest.parent_id,
            metadata_hash=manifest.metadata_hash,
            created_at=manifest.created_at,
            chunks=manifest.chunks,
        )
        if manifest.root != expected:
            raise CDPError("checkpoint manifest root does not match")
        return manifest


def manifest_root(
    *,
    version_id: str,
    parent_id: str | None,
    metadata_hash: str,
    created_at: str,
    chunks: tuple[ChunkRef, ...],
) -> str:
    ordered = sorted(
        chunks,
        key=lambda chunk: (
            chunk.file,
            chunk.offset,
            chunk.length,
            chunk.digest,
        ),
    )
    payload = {
        "version": "1",
        "version_id": version_id,
        "parent_id": parent_id,
        "metadata_hash": metadata_hash,
        "created_at": created_at,
        "chunks": [chunk.to_dict() for chunk in ordered],
    }
    return _sha256(_canonical_json(payload))


class ChunkStore:
    """Small, durable content-addressed store for checkpoint chunks."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.cas = self.root / "cas"

    def path(self, digest: str) -> Path:
        if not digest.startswith("sha256:"):
            raise CDPError(f"invalid chunk digest: {digest}")
        value = digest.split(":", 1)[1]
        if len(value) != 64:
            raise CDPError(f"invalid sha256 digest: {digest}")
        return self.cas / "sha256" / value[:2] / value[2:]

    def has(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def put_bytes(self, data: bytes) -> str:
        digest = _sha256(data)
        path = self.path(digest)
        if path.is_file():
            if path.stat().st_size != len(data):
                raise CDPError(f"existing chunk has wrong size: {digest}")
            return digest
        _atomic_write(path, data)
        return digest

    def put_file(
        self,
        source: str | Path,
        *,
        hardlink: bool = False,
    ) -> tuple[str, int]:
        source_path = Path(source)
        size = source_path.stat().st_size
        digest = _sha256_file(source_path)
        path = self.path(digest)
        if path.is_file():
            if path.stat().st_size != size:
                raise CDPError(f"existing chunk has wrong size: {digest}")
            return digest, size
        path.parent.mkdir(parents=True, exist_ok=True)
        if hardlink:
            try:
                os.link(source_path, path)
                _fsync_directory(path.parent)
                return digest, size
            except OSError:
                pass
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source_path, temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            _unlink_quietly(temporary)
            raise CDPError(f"could not store chunk {digest}: {exc}") from exc
        return digest, size

    def read(self, digest: str) -> bytes:
        path = self.path(digest)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CDPError(f"could not read chunk {digest}: {exc}") from exc

    def verify(self, digest: str) -> int:
        path = self.path(digest)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise CDPError(f"could not stat chunk {digest}: {exc}") from exc
        if _sha256_file(path) != digest:
            raise CDPError(f"chunk digest mismatch: {digest}")
        return size

    def collect_garbage(self, referenced: Iterable[str]) -> tuple[int, int]:
        """Delete CAS chunks not referenced by a retained manifest."""
        keep = set(referenced)
        base = self.cas / "sha256"
        if not base.is_dir():
            return 0, 0
        deleted = 0
        freed_bytes = 0
        hex_digits = set("0123456789abcdef")
        for prefix in sorted(base.iterdir()):
            if not prefix.is_dir():
                continue
            prefix_name = prefix.name
            if len(prefix_name) != 2 or any(
                char not in hex_digits for char in prefix_name
            ):
                continue
            for path in sorted(prefix.iterdir()):
                if not path.is_file():
                    continue
                name = path.name
                if len(name) != 62 or any(
                    char not in hex_digits for char in name
                ):
                    continue
                digest = "sha256:" + prefix_name + name
                if digest in keep:
                    continue
                try:
                    size = path.stat().st_size
                    path.unlink()
                except OSError as exc:
                    raise CDPError(
                        f"could not delete chunk {digest}: {exc}"
                    ) from exc
                deleted += 1
                freed_bytes += size
            try:
                prefix.rmdir()
            except OSError:
                pass
        return deleted, freed_bytes


class ManifestStore:
    """Persistent version manifests, separate from checkpoint data."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.versions = self.root / "versions"

    def path(self, version_id: str) -> Path:
        return self.versions / (quote(version_id, safe="") + ".json")

    def write(self, manifest: VersionManifest) -> Path:
        path = self.path(manifest.version_id)
        _atomic_write(
            path,
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True).encode("utf-8")
            + b"\n",
        )
        return path

    def read(self, version_id: str) -> VersionManifest:
        path = self.path(version_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CDPError(f"could not read manifest {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CDPError(f"manifest must be an object: {path}")
        return VersionManifest.from_dict(payload)

    def list(self) -> list[VersionManifest]:
        if not self.versions.is_dir():
            return []
        manifests = []
        for path in sorted(self.versions.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                manifests.append(VersionManifest.from_dict(payload))
            except (OSError, ValueError) as exc:
                raise CDPError(f"could not read manifest {path}: {exc}") from exc
        return manifests

    def delete(self, version_id: str) -> bool:
        path = self.path(version_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CDPError(f"could not delete manifest {path}: {exc}") from exc
        _fsync_directory(self.versions)
        return True


@dataclass(frozen=True)
class FenceToken:
    epoch: int
    owner: str
    version_id: str
    nonce: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "owner": self.owner,
            "version_id": self.version_id,
            "nonce": self.nonce,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FenceToken:
        try:
            return cls(
                epoch=int(payload["epoch"]),
                owner=str(payload["owner"]),
                version_id=str(payload["version_id"]),
                nonce=str(payload["nonce"]),
                created_at=str(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CDPError(f"invalid fence token: {exc}") from exc


class FenceStore:
    """Durable epoch fencing for cutover and target writes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / ".aevrel" / "fence.json"

    def current(self) -> FenceToken | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CDPError(f"could not read fence token: {exc}") from exc
        if not isinstance(payload, dict):
            raise CDPError("fence token must be an object")
        return FenceToken.from_dict(payload)

    def acquire(self, owner: str, epoch: int, version_id: str) -> FenceToken:
        if not owner:
            raise CDPError("fence owner must not be empty")
        if epoch < 1:
            raise CDPError("fence epoch must be positive")
        with TargetLock(self.root):
            current = self.current()
            if current is not None:
                if epoch < current.epoch:
                    raise CDPError(
                        f"stale fence epoch {epoch}; current epoch is {current.epoch}"
                    )
                if epoch == current.epoch and current.owner != owner:
                    raise CDPError(
                        f"fence epoch {epoch} is owned by {current.owner}"
                    )
            token = FenceToken(
                epoch=epoch,
                owner=owner,
                version_id=version_id,
                nonce=uuid.uuid4().hex,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            _atomic_write(
                self.path,
                json.dumps(token.to_dict(), indent=2, sort_keys=True).encode("utf-8")
                + b"\n",
            )
            return token

    def validate(self, token: FenceToken | None) -> None:
        if token is None:
            raise CDPError("fence token is required")
        current = self.current()
        if current is None:
            raise CDPError("fence token has not been acquired")
        if (
            current.epoch != token.epoch
            or current.owner != token.owner
            or current.nonce != token.nonce
        ):
            raise CDPError("fence token is stale")


@dataclass(frozen=True)
class CurrentVersion:
    version: str
    version_id: str
    root: str

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "version_id": self.version_id,
            "root": self.root,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CurrentVersion:
        try:
            return cls(
                version=str(payload["version"]),
                version_id=str(payload["version_id"]),
                root=str(payload["root"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CDPError(f"invalid current version pointer: {exc}") from exc


class VersionTarget:
    """A target-side CAS plus version manifests and atomic promotion."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.chunks = ChunkStore(self.root)
        self.manifests = ManifestStore(self.root)
        self.shadow = self.root / "shadow"
        self.current_path = self.root / "current"

    def _shadow_path(self, version_id: str) -> Path:
        return self.shadow / quote(version_id, safe="")

    def stage(self, manifest: VersionManifest) -> Path:
        with TargetLock(self.root):
            return self._stage_locked(manifest)

    def _stage_locked(self, manifest: VersionManifest) -> Path:
        verify_manifest(manifest, self.chunks)
        self.root.mkdir(parents=True, exist_ok=True)
        shadow = self._shadow_path(manifest.version_id)
        if shadow.exists():
            existing = self.manifests.read(manifest.version_id)
            if existing.root != manifest.root:
                raise CDPError(
                    f"shadow version already exists with a different root: "
                    f"{manifest.version_id}"
                )
            self.manifests.write(manifest)
        else:
            self.manifests.write(manifest)
            self.shadow.mkdir(parents=True, exist_ok=True)
            base_manifest = None
            base_root = None
            if manifest.parent_id:
                parent_shadow = self._shadow_path(manifest.parent_id)
                if parent_shadow.is_dir():
                    try:
                        base_manifest = self.manifests.read(manifest.parent_id)
                        base_root = parent_shadow
                    except CDPError:
                        base_manifest = None
                        base_root = None
            temporary = Path(
                tempfile.mkdtemp(prefix=".promote-", dir=self.root)
            )
            try:
                materialize_manifest(
                    manifest,
                    self.chunks,
                    temporary,
                    base_manifest=base_manifest,
                    base_root=base_root,
                )
                os.replace(temporary, shadow)
            except Exception as exc:
                shutil.rmtree(temporary, ignore_errors=True)
                if isinstance(exc, CDPError):
                    raise
                raise CDPError(f"could not publish shadow version: {exc}") from exc
        return shadow

    def promote(
        self,
        manifest: VersionManifest,
        *,
        fence: FenceToken | None = None,
    ) -> CurrentVersion:
        with TargetLock(self.root):
            if fence is not None:
                FenceStore(self.root).validate(fence)
            self._stage_locked(manifest)
            pointer = CurrentVersion(
                version="1",
                version_id=manifest.version_id,
                root=manifest.root,
            )
            _atomic_write(
                self.current_path,
                json.dumps(pointer.to_dict(), indent=2, sort_keys=True).encode("utf-8")
                + b"\n",
            )
            return pointer

    def current(self) -> CurrentVersion | None:
        if not self.current_path.is_file():
            return None
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CDPError(f"could not read current version: {exc}") from exc
        if not isinstance(payload, dict):
            raise CDPError("current version pointer must be an object")
        return CurrentVersion.from_dict(payload)

    def rollback(
        self,
        version_id: str,
        *,
        fence: FenceToken | None = None,
    ) -> CurrentVersion:
        with TargetLock(self.root):
            if fence is not None:
                FenceStore(self.root).validate(fence)
            manifest = self.manifests.read(version_id)
            shadow = self._shadow_path(version_id)
            if not shadow.is_dir():
                raise CDPError(f"shadow version not found: {version_id}")
            pointer = CurrentVersion(
                version="1",
                version_id=manifest.version_id,
                root=manifest.root,
            )
            _atomic_write(
                self.current_path,
                json.dumps(pointer.to_dict(), indent=2, sort_keys=True).encode("utf-8")
                + b"\n",
            )
            return pointer

    def clear_current(self, *, fence: FenceToken | None = None) -> None:
        with TargetLock(self.root):
            if fence is not None:
                FenceStore(self.root).validate(fence)
            try:
                self.current_path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise CDPError(f"could not clear current version: {exc}") from exc
            _fsync_directory(self.current_path.parent)

    def prune(self, retain_versions: int) -> RetentionResult:
        """Keep recent versions plus current/fenced versions and reclaim storage."""
        if retain_versions < 1:
            raise ValueError("retain_versions must be positive")
        with TargetLock(self.root):
            manifests = self.manifests.list()
            if not manifests:
                return RetentionResult((), (), 0, 0)
            by_id = {
                manifest.version_id: manifest
                for manifest in manifests
            }
            current = self.current()
            if current is not None and current.version_id not in by_id:
                raise CDPError(
                    f"current version manifest is missing: "
                    f"{current.version_id}"
                )
            protected = set()
            if current is not None:
                protected.add(current.version_id)
            fence = FenceStore(self.root).current()
            if fence is not None and fence.version_id in by_id:
                protected.add(fence.version_id)
            ordered = sorted(
                manifests,
                key=lambda manifest: (
                    manifest.created_at,
                    manifest.version_id,
                ),
            )
            protected.update(
                manifest.version_id
                for manifest in ordered[-retain_versions:]
            )
            for version_id in protected:
                manifest = by_id[version_id]
                if not self._shadow_path(version_id).is_dir():
                    raise CDPError(
                        f"shadow version not found: {version_id}"
                    )
                for chunk in manifest.chunks:
                    if not self.chunks.has(chunk.digest):
                        raise CDPError(
                            f"missing chunk {chunk.digest} for retained "
                            f"version {version_id}"
                        )
            deleted_versions = tuple(
                manifest.version_id
                for manifest in ordered
                if manifest.version_id not in protected
            )
            for version_id in deleted_versions:
                shadow = self._shadow_path(version_id)
                try:
                    shutil.rmtree(shadow)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise CDPError(
                        f"could not delete shadow version {version_id}: {exc}"
                    ) from exc
                self.manifests.delete(version_id)
            referenced = {
                chunk.digest
                for manifest in ordered
                if manifest.version_id in protected
                for chunk in manifest.chunks
            }
            deleted_chunks, freed_bytes = self.chunks.collect_garbage(referenced)
            retained_versions = tuple(
                manifest.version_id
                for manifest in ordered
                if manifest.version_id in protected
            )
            return RetentionResult(
                retained_versions=retained_versions,
                deleted_versions=deleted_versions,
                deleted_chunks=deleted_chunks,
                freed_bytes=freed_bytes,
            )


@dataclass(frozen=True)
class RetentionResult:
    retained_versions: tuple[str, ...]
    deleted_versions: tuple[str, ...]
    deleted_chunks: int
    freed_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "retained_versions": list(self.retained_versions),
            "deleted_versions": list(self.deleted_versions),
            "deleted_chunks": self.deleted_chunks,
            "freed_bytes": self.freed_bytes,
        }


@dataclass(frozen=True)
class SyncResult:
    version_id: str
    parent_id: str | None
    total_bytes: int
    missing_bytes: int
    reused_bytes: int
    staged: bool

    @property
    def delta_ratio(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return self.missing_bytes / self.total_bytes


class CheckpointFollower:
    """Catch up complete source versions into a target shadow store.

    This deliberately does not promote. Promotion is a separate cutover action.
    """

    def __init__(
        self,
        source_root: str | Path,
        target: VersionTarget,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        chunking: ChunkingPolicy | None = None,
        file_provider: Callable[[VersionDescriptor], Mapping[str, bytes]] | None = None,
        manifest_provider: (
            Callable[
                [VersionDescriptor, ChunkStore, ChunkingPolicy],
                VersionManifest,
            ]
            | None
        ) = None,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.target = target
        self.chunk_bytes = chunk_bytes
        self.chunking = chunking
        self.file_provider = file_provider
        self.manifest_provider = manifest_provider

    def sync_once(self, *, max_versions: int | None = None) -> list[SyncResult]:
        if max_versions is not None and max_versions < 1:
            raise CDPError("max_versions must be positive")
        with TargetLock(self.target.root):
            descriptors = scan_versions(self.source_root)
            existing = {
                manifest.version_id: manifest
                for manifest in self.target.manifests.list()
            }
            results: list[SyncResult] = []
            staged_count = 0
            for descriptor in descriptors:
                if max_versions is not None and staged_count >= max_versions:
                    break
                current = existing.get(descriptor.version_id)
                if current is not None:
                    if current.metadata_hash != descriptor.marker.metadata_hash:
                        raise CDPError(
                            f"source version changed after being staged: "
                            f"{descriptor.version_id}"
                        )
                    if not self.target._shadow_path(descriptor.version_id).is_dir():
                        self.target._stage_locked(current)
                    results.append(
                        SyncResult(
                            version_id=descriptor.version_id,
                            parent_id=descriptor.marker.parent_id,
                            total_bytes=sum(
                                chunk.size for chunk in current.chunks
                            ),
                            missing_bytes=0,
                            reused_bytes=sum(
                                chunk.size for chunk in current.chunks
                            ),
                            staged=False,
                        )
                    )
                    continue

                previous: VersionManifest | None = None
                parent_id = descriptor.marker.parent_id
                if parent_id is not None:
                    previous = existing.get(parent_id)
                    if previous is None:
                        raise CDPError(
                            f"parent version has not been staged: {parent_id}"
                        )
                policy = self.chunking or ChunkingPolicy.fixed(self.chunk_bytes)
                if self.manifest_provider is not None:
                    manifest = self.manifest_provider(
                        descriptor,
                        self.target.chunks,
                        policy,
                    )
                elif self.file_provider is None:
                    manifest = build_manifest(
                        descriptor,
                        self.target.chunks,
                        chunk_bytes=self.chunk_bytes,
                        chunking=self.chunking,
                    )
                else:
                    files = self.file_provider(descriptor)
                    manifest = build_manifest_from_files(
                        version_id=descriptor.version_id,
                        parent_id=descriptor.marker.parent_id,
                        metadata_hash=descriptor.marker.metadata_hash,
                        created_at=descriptor.marker.created_at,
                        files=files,
                        chunk_store=self.target.chunks,
                        chunking=policy,
                    )
                delta = delta_manifest(previous, manifest)
                self.target._stage_locked(manifest)
                existing[manifest.version_id] = manifest
                staged_count += 1
                results.append(
                    SyncResult(
                        version_id=manifest.version_id,
                        parent_id=manifest.parent_id,
                        total_bytes=delta.total_bytes,
                        missing_bytes=delta.missing_bytes,
                        reused_bytes=delta.reused_bytes,
                        staged=True,
                    )
                )
            return results


def build_manifest(
    descriptor: VersionDescriptor,
    chunk_store: ChunkStore,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    chunking: ChunkingPolicy | None = None,
) -> VersionManifest:
    if not descriptor.complete:
        raise CDPError(f"checkpoint version is incomplete: {descriptor.version_id}")
    policy = chunking or ChunkingPolicy.fixed(chunk_bytes)
    if policy.algorithm == "fixed" and chunk_bytes < 1:
        raise CDPError("chunk_bytes must be positive")
    files: dict[str, bytes] = {}
    for path in sorted(descriptor.path.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(descriptor.path)
        if any(part.startswith(".aevrel") for part in relative.parts):
            continue
        if relative.name == MARKER_NAME:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return build_manifest_from_files(
        version_id=descriptor.version_id,
        parent_id=descriptor.marker.parent_id,
        metadata_hash=descriptor.marker.metadata_hash,
        created_at=descriptor.marker.created_at,
        files=files,
        chunk_store=chunk_store,
        chunking=policy,
    )


def build_manifest_from_files(
    *,
    version_id: str,
    parent_id: str | None,
    metadata_hash: str,
    created_at: str,
    files: Mapping[str, bytes],
    chunk_store: ChunkStore,
    chunking: ChunkingPolicy | None = None,
) -> VersionManifest:
    """Build a manifest for an already target-layout file set.

    The caller is responsible for materializing the target layout first. Chunk
    identities therefore do not depend on source file names or source offsets.
    """
    policy = chunking or ChunkingPolicy.fixed()
    chunks: list[ChunkRef] = []
    for file_name in sorted(files):
        _validate_relative_file(file_name)
        data = files[file_name]
        if not data:
            digest = chunk_store.put_bytes(b"")
            chunks.append(
                ChunkRef(
                    digest=digest,
                    size=0,
                    file=file_name,
                    offset=0,
                    length=0,
                )
            )
            continue
        offsets = policy.offsets(data)
        for start, end in zip(offsets, offsets[1:]):
            block = data[start:end]
            if not block:
                continue
            digest = chunk_store.put_bytes(block)
            chunks.append(
                ChunkRef(
                    digest=digest,
                    size=len(block),
                    file=file_name,
                    offset=start,
                    length=len(block),
                )
            )
    ordered = tuple(
        sorted(
            chunks,
            key=lambda chunk: (
                chunk.file,
                chunk.offset,
                chunk.length,
                chunk.digest,
            ),
        )
    )
    root = manifest_root(
        version_id=version_id,
        parent_id=parent_id,
        metadata_hash=metadata_hash,
        created_at=created_at,
        chunks=ordered,
    )
    return VersionManifest(
        version="1",
        version_id=version_id,
        parent_id=parent_id,
        metadata_hash=metadata_hash,
        created_at=created_at,
        root=root,
        chunks=ordered,
    )


def build_manifest_from_paths(
    *,
    version_id: str,
    parent_id: str | None,
    metadata_hash: str,
    created_at: str,
    files: Mapping[str, Path],
    chunk_store: ChunkStore,
    chunking: ChunkingPolicy | None = None,
    hardlink: bool = False,
    reuse_chunks: Mapping[str, tuple[ChunkRef, ...]] | None = None,
    chunking_for_file: Callable[[str, Path], ChunkingPolicy] | None = None,
) -> VersionManifest:
    """Build a manifest without loading target-layout files into memory."""
    policy = chunking or ChunkingPolicy.fixed()
    chunks: list[ChunkRef] = []
    for file_name in sorted(files):
        _validate_relative_file(file_name)
        path = Path(files[file_name])
        if not path.is_file():
            raise CDPError(f"target-layout file not found: {path}")
        if reuse_chunks is not None and file_name in reuse_chunks:
            chunks.extend(reuse_chunks[file_name])
            continue
        file_policy = (
            chunking_for_file(file_name, path)
            if chunking_for_file is not None
            else policy
        )
        if file_policy.algorithm == "file":
            digest, size = chunk_store.put_file(path, hardlink=hardlink)
            chunks.append(
                ChunkRef(
                    digest=digest,
                    size=size,
                    file=file_name,
                    offset=0,
                    length=size,
                )
            )
            continue
        file_chunk_count = 0
        file_offset = 0
        with path.open("rb") as stream:
            for block in file_policy.iter_chunks(stream):
                digest = chunk_store.put_bytes(block)
                chunks.append(
                    ChunkRef(
                        digest=digest,
                        size=len(block),
                        file=file_name,
                        offset=file_offset,
                        length=len(block),
                    )
                )
                file_chunk_count += 1
                file_offset += len(block)
        if file_chunk_count == 0:
            digest = chunk_store.put_bytes(b"")
            chunks.append(
                ChunkRef(
                    digest=digest,
                    size=0,
                    file=file_name,
                    offset=0,
                    length=0,
                )
            )
    ordered = tuple(
        sorted(
            chunks,
            key=lambda chunk: (
                chunk.file,
                chunk.offset,
                chunk.length,
                chunk.digest,
            ),
        )
    )
    root = manifest_root(
        version_id=version_id,
        parent_id=parent_id,
        metadata_hash=metadata_hash,
        created_at=created_at,
        chunks=ordered,
    )
    return VersionManifest(
        version="1",
        version_id=version_id,
        parent_id=parent_id,
        metadata_hash=metadata_hash,
        created_at=created_at,
        root=root,
        chunks=ordered,
    )


@dataclass(frozen=True)
class ManifestDelta:
    base_version: str | None
    version_id: str
    missing: tuple[ChunkRef, ...]
    reused: tuple[ChunkRef, ...]
    total_bytes: int
    missing_bytes: int
    reused_bytes: int

    @property
    def delta_ratio(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return self.missing_bytes / self.total_bytes


def delta_manifest(
    previous: VersionManifest | None,
    current: VersionManifest,
) -> ManifestDelta:
    previous_by_digest = (
        {chunk.digest: chunk for chunk in previous.chunks}
        if previous is not None
        else {}
    )
    current_by_digest: dict[str, ChunkRef] = {}
    for chunk in current.chunks:
        current_by_digest.setdefault(chunk.digest, chunk)
    missing = tuple(
        current_by_digest[digest]
        for digest in sorted(current_by_digest.keys() - previous_by_digest.keys())
    )
    reused = tuple(
        current_by_digest[digest]
        for digest in sorted(current_by_digest.keys() & previous_by_digest.keys())
    )
    total_bytes = sum(chunk.size for chunk in current_by_digest.values())
    missing_bytes = sum(chunk.size for chunk in missing)
    return ManifestDelta(
        base_version=previous.version_id if previous is not None else None,
        version_id=current.version_id,
        missing=missing,
        reused=reused,
        total_bytes=total_bytes,
        missing_bytes=missing_bytes,
        reused_bytes=total_bytes - missing_bytes,
    )


def verify_manifest(manifest: VersionManifest, chunk_store: ChunkStore) -> int:
    expected = manifest_root(
        version_id=manifest.version_id,
        parent_id=manifest.parent_id,
        metadata_hash=manifest.metadata_hash,
        created_at=manifest.created_at,
        chunks=manifest.chunks,
    )
    if manifest.root != expected:
        raise CDPError("manifest root does not match")
    unique: dict[str, ChunkRef] = {}
    for chunk in manifest.chunks:
        unique.setdefault(chunk.digest, chunk)
    for digest, chunk in unique.items():
        actual_size = chunk_store.verify(digest)
        if actual_size != chunk.size:
            raise CDPError(
                f"chunk size mismatch for {digest}: {actual_size} != {chunk.size}"
            )
    return sum(chunk.size for chunk in unique.values())


def materialize_manifest(
    manifest: VersionManifest,
    chunk_store: ChunkStore,
    target_root: str | Path,
    *,
    overwrite: bool = False,
    base_manifest: VersionManifest | None = None,
    base_root: str | Path | None = None,
) -> int:
    verify_manifest(manifest, chunk_store)
    root = Path(target_root).resolve()
    base_path = Path(base_root).resolve() if base_root is not None else None
    by_file: dict[str, list[ChunkRef]] = {}
    for chunk in manifest.chunks:
        _validate_relative_file(chunk.file)
        by_file.setdefault(chunk.file, []).append(chunk)
    base_by_file: dict[str, list[ChunkRef]] = {}
    if base_manifest is not None:
        for chunk in base_manifest.chunks:
            base_by_file.setdefault(chunk.file, []).append(chunk)
    written = 0
    for file_name, chunks in sorted(by_file.items()):
        path = root / file_name
        if path.exists() and not overwrite:
            raise CDPError(f"target file already exists: {path}")
        ordered = sorted(chunks, key=lambda chunk: chunk.offset)
        expected_size = _validate_file_chunks(file_name, ordered)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.name + ".",
            suffix=".tmp",
            dir=path.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        _unlink_quietly(temporary)
        base_file = base_path / file_name if base_path is not None else None
        base_chunks = base_by_file.get(file_name, [])
        base_size = None
        if base_file is not None:
            try:
                base_size = base_file.stat().st_size
            except OSError:
                base_file = None
        try:
            unchanged = (
                base_file is not None
                and base_size == expected_size
                and _chunk_layout(base_chunks) == _chunk_layout(ordered)
            )
            if unchanged:
                if not _link_or_clone(base_file, temporary):
                    _write_full_file(file_name, ordered, chunk_store, temporary)
                    written += sum(chunk.length for chunk in ordered)
            elif (
                len(ordered) == 1
                and ordered[0].offset == 0
                and ordered[0].length == expected_size
            ):
                _link_or_clone(
                    chunk_store.path(ordered[0].digest),
                    temporary,
                )
            elif (
                base_file is not None
                and base_size == expected_size
                and base_chunks
                and _clone_file(base_file, temporary)
            ):
                written += _patch_file(
                    temporary,
                    ordered,
                    base_chunks,
                    chunk_store,
                )
            else:
                _write_full_file(file_name, ordered, chunk_store, temporary)
                written += sum(chunk.length for chunk in ordered)
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            _unlink_quietly(temporary)
            raise CDPError(f"could not materialize {path}: {exc}") from exc
        except Exception:
            _unlink_quietly(temporary)
            raise
    return written


def _validate_file_chunks(
    file_name: str,
    chunks: list[ChunkRef],
) -> int:
    expected_offset = 0
    for chunk in chunks:
        if chunk.offset != expected_offset:
            raise CDPError(
                f"non-contiguous chunks for {file_name} at "
                f"{chunk.offset}, expected {expected_offset}"
            )
        expected_offset += chunk.length
    return expected_offset


def _chunk_layout(chunks: list[ChunkRef]) -> tuple[tuple[int, int, str], ...]:
    return tuple(
        (chunk.offset, chunk.length, chunk.digest)
        for chunk in sorted(chunks, key=lambda item: item.offset)
    )


def _write_full_file(
    file_name: str,
    chunks: list[ChunkRef],
    chunk_store: ChunkStore,
    path: Path,
) -> None:
    with path.open("wb") as stream:
        for chunk in chunks:
            data = chunk_store.read(chunk.digest)
            if len(data) != chunk.length:
                raise CDPError(f"chunk length mismatch for {chunk.digest}")
            stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _patch_file(
    path: Path,
    chunks: list[ChunkRef],
    base_chunks: list[ChunkRef],
    chunk_store: ChunkStore,
) -> int:
    base = {
        (chunk.offset, chunk.length, chunk.digest)
        for chunk in base_chunks
    }
    written = 0
    with path.open("r+b") as stream:
        for chunk in chunks:
            key = (chunk.offset, chunk.length, chunk.digest)
            if key in base:
                continue
            data = chunk_store.read(chunk.digest)
            if len(data) != chunk.length:
                raise CDPError(f"chunk length mismatch for {chunk.digest}")
            stream.seek(chunk.offset)
            stream.write(data)
            written += len(data)
        stream.truncate(sum(chunk.length for chunk in chunks))
        stream.flush()
        os.fsync(stream.fileno())
    return written


def _link_or_clone(source: Path, destination: Path) -> bool:
    try:
        os.link(source, destination)
        return True
    except OSError:
        return _clone_file(source, destination)


def _clone_file(source: Path, destination: Path) -> bool:
    _unlink_quietly(destination)
    if fcntl is not None:
        try:
            with source.open("rb") as src, destination.open("wb") as dst:
                fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())
            return True
        except OSError:
            _unlink_quietly(destination)
    if hasattr(os, "copy_file_range"):
        try:
            with source.open("rb") as src, destination.open("wb") as dst:
                while True:
                    copied = os.copy_file_range(
                        src.fileno(),
                        dst.fileno(),
                        8 * 1024 * 1024,
                    )
                    if copied == 0:
                        break
            return True
        except OSError:
            _unlink_quietly(destination)
    try:
        shutil.copyfile(source, destination)
        return True
    except OSError:
        _unlink_quietly(destination)
        return False


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _validate_relative_file(file_name: str) -> None:
    path = Path(file_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CDPError(f"invalid checkpoint-relative file: {file_name}")
