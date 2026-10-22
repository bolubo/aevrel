from __future__ import annotations

import os
import hashlib
from pathlib import Path
from urllib.parse import unquote, urlparse


class StorageError(ValueError):
    """Raised when a storage URI or range is invalid."""


class FileStorage:
    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root) if root else None

    def exists(self, uri: str) -> bool:
        return self.path(uri).is_file()

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise StorageError("file range must be non-negative")
        path = self.path(uri)
        try:
            with path.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(size)
        except OSError as exc:
            raise StorageError(f"could not read {path}: {exc}") from exc
        if len(data) != size:
            raise StorageError(
                f"short read from {path}: wanted {size} bytes, got {len(data)}"
            )
        return data

    def write_range(self, uri: str, offset: int, data: bytes) -> None:
        if offset < 0:
            raise StorageError("file range must be non-negative")
        path = self.path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = "r+b" if path.exists() else "wb"
            with path.open(mode) as stream:
                stream.seek(offset)
                stream.write(data)
        except OSError as exc:
            raise StorageError(f"could not write {path}: {exc}") from exc

    def copy_range(
        self,
        source_uri: str,
        target_uri: str,
        source_offset: int,
        target_offset: int,
        size: int,
    ) -> None:
        if source_offset < 0 or target_offset < 0 or size < 0:
            raise StorageError("file copy range must be non-negative")
        if not hasattr(os, "copy_file_range"):
            raise NotImplementedError("copy_file_range is unavailable")
        source = self.path(source_uri)
        target = self.path(target_uri)
        try:
            source_fd = os.open(source, os.O_RDONLY)
            target_fd = os.open(target, os.O_RDWR)
        except OSError as exc:
            raise StorageError(f"could not open copy range: {exc}") from exc
        try:
            remaining = size
            source_cursor = source_offset
            target_cursor = target_offset
            while remaining:
                copied = os.copy_file_range(
                    source_fd,
                    target_fd,
                    remaining,
                    source_cursor,
                    target_cursor,
                )
                if copied <= 0:
                    raise StorageError(
                        f"short copy_file_range from {source} to {target}"
                    )
                source_cursor += copied
                target_cursor += copied
                remaining -= copied
        except OSError as exc:
            raise StorageError(
                f"could not copy {source} to {target}: {exc}"
            ) from exc
        finally:
            os.close(source_fd)
            os.close(target_fd)

    def ensure_size(self, uri: str, size: int) -> None:
        if size < 0:
            raise StorageError("file size must be non-negative")
        path = self.path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a+b") as stream:
                stream.truncate(size)
        except OSError as exc:
            raise StorageError(f"could not size {path}: {exc}") from exc

    def size(self, uri: str) -> int:
        path = self.path(uri)
        try:
            return path.stat().st_size
        except OSError as exc:
            raise StorageError(f"could not stat {path}: {exc}") from exc

    def fsync(self, uri: str) -> None:
        path = self.path(uri)
        try:
            descriptor = os.open(path, os.O_RDWR)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise StorageError(f"could not fsync {path}: {exc}") from exc

    def checksum(self, uri: str) -> str:
        path = self.path(uri)
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise StorageError(f"could not hash {path}: {exc}") from exc
        return "sha256:" + digest.hexdigest()

    def path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise StorageError(f"unsupported storage URI: {uri}")
        if parsed.netloc not in {"", "localhost"}:
            raise StorageError(f"file URI must be local: {uri}")
        raw_path = unquote(parsed.path)
        if os.name == "nt" and _is_windows_drive_path(raw_path):
            raw_path = raw_path[1:]
        path = Path(raw_path)
        if not path.is_absolute():
            if self.root is None:
                raise StorageError(f"relative file URI needs a root: {uri}")
            path = self.root / path
        return path


def _is_windows_drive_path(path: str) -> bool:
    return len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha()
