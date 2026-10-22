from __future__ import annotations

import hashlib
import os
import threading
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ..dcp.range import DcpRangeError, ZipTensorRange
from .file import StorageError


URI_SCHEME = "dcpzip"


def dcpzip_uri(
    path: str | Path,
    *,
    name: str,
    data_offset: int,
    size: int,
    compression: int,
) -> str:
    query = (
        f"name={quote(name, safe='')}"
        f"&offset={int(data_offset)}"
        f"&size={int(size)}"
        f"&compression={int(compression)}"
    )
    return f"{URI_SCHEME}://{quote(str(Path(path)), safe='/:')}?{query}"


class DcpZipStorage:
    """Read tensor ranges directly from a DCP `.distcp` ZIP."""

    def __init__(self) -> None:
        self._ranges: dict[str, tuple[Path, ZipTensorRange]] = {}
        self._fds: dict[Path, int] = {}
        self._lock = threading.Lock()

    def exists(self, uri: str) -> bool:
        return self._resolve(uri)[0].is_file()

    def size(self, uri: str) -> int:
        return self._resolve(uri)[1].size

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        path, tensor_range = self._resolve(uri)
        try:
            if tensor_range.compression != zipfile.ZIP_STORED:
                return tensor_range.read(path, offset, size)
            if not hasattr(os, "pread"):
                return tensor_range.read(path, offset, size)
            absolute = tensor_range.data_offset + offset
            if offset < 0 or size < 0 or offset + size > tensor_range.size:
                raise DcpRangeError(
                    f"range {offset}+{size} is outside {tensor_range.name}"
                )
            with self._lock:
                fd = self._fds.get(path)
                if fd is None:
                    fd = os.open(path, os.O_RDONLY)
                    self._fds[path] = fd
            data = os.pread(fd, size, absolute)
            if len(data) != size:
                raise DcpRangeError(
                    f"short DCP range read from {path}: {len(data)} != {size}"
                )
            return data
        except DcpRangeError as exc:
            raise StorageError(
                f"could not read DCP range {uri}: {exc}"
            ) from exc
        except OSError as exc:
            raise StorageError(
                f"could not read DCP file {path}: {exc}"
            ) from exc

    def checksum(self, uri: str) -> str:
        path, tensor_range = self._resolve(uri)
        try:
            if tensor_range.compression != zipfile.ZIP_STORED:
                return tensor_range.checksum(path)
            if not hasattr(os, "pread"):
                return tensor_range.checksum(path)
            digest = hashlib.sha256()
            for offset in range(0, tensor_range.size, 4 * 1024 * 1024):
                digest.update(
                    self.read_range(
                        uri,
                        offset,
                        min(4 * 1024 * 1024, tensor_range.size - offset),
                    )
                )
            return "sha256:" + digest.hexdigest()
        except DcpRangeError as exc:
            raise StorageError(
                f"could not checksum DCP range {uri}: {exc}"
            ) from exc

    def _resolve(self, uri: str) -> tuple[Path, ZipTensorRange]:
        cached = self._ranges.get(uri)
        if cached is not None:
            return cached
        parsed = urlsplit(uri)
        if parsed.scheme != URI_SCHEME:
            raise StorageError(f"not a dcpzip URI: {uri}")
        path = unquote(parsed.path)
        if parsed.netloc:
            path = unquote(parsed.netloc) + path
        query = parse_qs(parsed.query)
        try:
            result = (
                Path(path),
                ZipTensorRange(
                    name=query["name"][0],
                    data_offset=int(query["offset"][0]),
                    size=int(query["size"][0]),
                    compression=int(query["compression"][0]),
                ),
            )
        except (KeyError, ValueError) as exc:
            raise StorageError(f"invalid dcpzip URI: {uri}") from exc
        self._ranges[uri] = result
        return result
