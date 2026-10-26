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



MARKER_NAME = ".aevrel-complete"
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ChunkingPolicy:
    """How checkpoint files are cut into content-addressed chunks.

    ``algorithm`` is ``"fixed"`` (``chunk_bytes``-sized pieces), ``"cdc"``
    (content-defined boundaries bounded by ``min_bytes``, ``avg_bytes`` and
    ``max_bytes``) or ``"file"`` (one chunk per file). Sizes the algorithm
    does not use are ignored when chunking, but the CDC bounds are still
    validated for ``"cdc"`` and ``"file"``.

    Raises:
        ValueError: on an unknown algorithm, a non-positive size, or CDC
            sizes that do not satisfy ``min <= avg <= max``.
    """

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


class CDPError(RuntimeError):
    """Raised when a checkpoint version or manifest is invalid."""


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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


@dataclass(frozen=True)
class VersionMarker:
    """The ``.aevrel-complete`` marker a source writes beside a version.

    A version directory is only considered for transfer when it carries
    this marker, and only when ``complete`` is true. ``metadata_hash`` must
    be a ``sha256:`` digest; ``from_dict`` raises ``CDPError`` when the
    payload is malformed or ``version_id``/``job_uid`` is empty.
    """

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
    """One marked version directory found by ``scan_versions``."""

    version_id: str
    path: Path
    marker: VersionMarker

    @property
    def complete(self) -> bool:
        return self.marker.complete


def read_marker(path: str | Path) -> VersionMarker:
    """Load and validate a version marker.

    ``path`` is either the marker file itself or a version directory, in
    which case ``.aevrel-complete`` inside it is read.

    Raises:
        CDPError: if the file cannot be read, is not a JSON object, or
            fails ``VersionMarker`` validation.
    """
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
    """Atomically write ``marker`` as JSON and return the file written.

    When ``path`` is an existing directory the marker is written to
    ``.aevrel-complete`` inside it; otherwise ``path`` is the marker file.
    The file is written to a temporary name, fsynced and renamed into
    place, so readers never observe a partial marker.

    Raises:
        CDPError: if the marker cannot be written.
    """
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
    """List the marked versions directly under a source checkpoint root.

    Only immediate, non-hidden subdirectories that contain a marker file
    are considered; anything else is ignored. Versions whose marker says
    ``complete: false`` are skipped unless ``include_incomplete`` is set.
    The result is ordered by marker ``created_at``, then ``version_id``.

    Raises:
        CDPError: if ``root`` is not a directory, a marker is invalid, or
            two directories claim the same ``version_id`` (complete or
            not).
    """
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
    """A byte range of one checkpoint-relative file, stored by digest.

    ``file`` is the path relative to the version root, ``offset`` and
    ``length`` locate the range in that file, and ``digest`` names the CAS
    object holding its bytes. ``size`` must equal ``length``;
    ``from_dict`` raises ``CDPError`` when it does not, when a size is
    negative, or when the digest is not ``sha256:``.
    """

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
    """The chunk list and identity of one checkpoint version.

    ``root`` is the ``manifest_root`` digest over the identity fields and
    the chunk list, so it changes whenever any chunk or field changes.
    ``from_dict`` recomputes it and raises ``CDPError`` on a mismatch or a
    malformed payload.
    """

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
    """Compute the content digest that identifies a version manifest.

    The digest is SHA-256 over canonical JSON (sorted keys, no whitespace)
    of the manifest format version, the identity fields and the chunk
    list sorted by ``(file, offset, length, digest)``. The input order of
    ``chunks`` therefore does not affect the result. Returns the digest as
    ``"sha256:<hex>"``.
    """
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
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    hasher.update(block)
        except OSError as exc:
            raise CDPError(f"could not read chunk {digest}: {exc}") from exc
        if "sha256:" + hasher.hexdigest() != digest:
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
    """The cutover fence recorded in ``.aevrel/fence.json`` on a target.

    ``FenceStore.acquire`` issues a token with a fresh ``nonce``; a later
    write that presents an older epoch, another owner or another nonce is
    rejected as stale. ``from_dict`` raises ``CDPError`` on a malformed
    payload.
    """

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
    """The ``current`` pointer of a target: the promoted version and root.

    ``from_dict`` raises ``CDPError`` on a malformed payload.
    """

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
    """What ``VersionTarget.prune`` kept and reclaimed.

    ``deleted_chunks`` and ``freed_bytes`` count CAS objects that no
    retained manifest referenced any more.
    """

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
    """Per-version outcome of ``CheckpointFollower.sync_once``.

    ``staged`` is false when the version was already on the target; its
    bytes are then all reported as reused. ``delta_ratio`` is
    ``missing_bytes / total_bytes``, or ``0.0`` for an empty version.
    """

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
    """Chunk a complete source version into ``chunk_store``.

    Every regular file under the version directory is read, except the
    marker file and anything under a path component starting with
    ``.aevrel``. Chunks are written to ``chunk_store`` by the native
    chunker as a side effect. ``chunking`` overrides the policy; without
    it, fixed ``chunk_bytes`` chunks are used. Returns the manifest.

    Raises:
        CDPError: if the version is not complete, ``chunk_bytes`` is not
            positive for fixed chunking, a file path is invalid, or the
            native aevrel-fast binary is unavailable or fails.
    """
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
    In-memory inputs are staged to a temporary directory and ingested through
    the native chunker; there is no Python chunking implementation.
    """
    policy = chunking or ChunkingPolicy.fixed()
    with tempfile.TemporaryDirectory(prefix="aevrel-cdp-") as staging:
        root = Path(staging)
        staged: dict[str, Path] = {}
        for file_name in sorted(files):
            _validate_relative_file(file_name)
            path = root / file_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[file_name])
            staged[file_name] = path
        return build_manifest_from_paths(
            version_id=version_id,
            parent_id=parent_id,
            metadata_hash=metadata_hash,
            created_at=created_at,
            files=staged,
            chunk_store=chunk_store,
            chunking=policy,
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
    """Build a manifest without loading target-layout files into memory.

    All chunking, hashing and content-addressed storage is performed by the
    native aevrel-fast binary; this function only orchestrates policy grouping,
    chunk reuse and manifest assembly. There is no Python fallback.
    """
    from .native import build_manifest_native

    policy = chunking or ChunkingPolicy.fixed()
    reuse = dict(reuse_chunks or {})
    groups: dict[ChunkingPolicy, dict[str, Path]] = {}
    for file_name in sorted(files):
        _validate_relative_file(file_name)
        path = Path(files[file_name])
        if not path.is_file():
            raise CDPError(f"target-layout file not found: {path}")
        if file_name in reuse:
            continue
        file_policy = (
            chunking_for_file(file_name, path)
            if chunking_for_file is not None
            else policy
        )
        groups.setdefault(file_policy, {})[file_name] = path
    chunks: list[ChunkRef] = []
    for group_policy, group_files in groups.items():
        ingested = build_manifest_native(
            version_id=version_id,
            parent_id=parent_id,
            metadata_hash=metadata_hash,
            created_at=created_at,
            files=group_files,
            chunk_store=chunk_store,
            chunking=group_policy,
            hardlink=hardlink,
        )
        chunks.extend(ingested.chunks)
    for file_name in sorted(reuse):
        chunks.extend(reuse[file_name])
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
    """The chunks a target lacks when moving from one manifest to another.

    Produced by ``delta_manifest``. Chunks are deduplicated by digest, so
    byte counts are for unique chunk contents. ``delta_ratio`` is
    ``missing_bytes / total_bytes``, or ``0.0`` for an empty version.
    """

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
    """Compare ``current`` with ``previous`` by chunk digest.

    A digest of ``current`` is ``reused`` when ``previous`` also contains
    it, wherever it sits, and ``missing`` otherwise; with no ``previous``
    every chunk is missing. Both tuples hold one ``ChunkRef`` per unique
    digest, sorted by digest. Pure computation: no store is read.
    """
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
    """Check a manifest against its root and the chunks in ``chunk_store``.

    The root is recomputed, then every unique chunk is re-hashed from the
    store and its size compared with the manifest. Returns the total size
    in bytes of the unique chunks.

    Raises:
        CDPError: if the root does not match, or a chunk is missing,
            unreadable, has the wrong digest or the wrong size.
    """
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
    """Write the files of a verified manifest under ``target_root``.

    The manifest is checked with ``verify_manifest`` first. Each file is
    assembled in a temporary file and renamed into place. When
    ``base_manifest`` and ``base_root`` describe an earlier materialized
    version, unchanged files are hard-linked or cloned from it and changed
    files are patched chunk by chunk; a file made of a single whole chunk
    is linked or cloned from the store. Returns the number of bytes copied
    out of the chunk store; linked and cloned data is not counted.

    Raises:
        CDPError: if verification fails, a file already exists and
            ``overwrite`` is false, a chunk path is invalid, a file's
            chunks are not contiguous, or a write fails.
    """
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
