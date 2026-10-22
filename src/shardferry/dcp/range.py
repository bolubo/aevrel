from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path


class DcpRangeError(RuntimeError):
    """Raised when a DCP storage entry cannot be read as a raw range."""


@dataclass(frozen=True)
class ZipTensorRange:
    name: str
    data_offset: int
    size: int
    compression: int

    def read(self, path: str | Path, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > self.size:
            raise DcpRangeError(
                f"range {offset}+{length} is outside {self.name} ({self.size})"
            )
        if self.compression != zipfile.ZIP_STORED:
            return _read_compressed(path, self.name, offset, length)
        with Path(path).open("rb") as stream:
            stream.seek(self.data_offset + offset)
            data = stream.read(length)
        if len(data) != length:
            raise DcpRangeError(
                f"short DCP range read from {path}: {len(data)} != {length}"
            )
        return data

    def checksum(self, path: str | Path, chunk_size: int = 4 * 1024 * 1024) -> str:
        digest = hashlib.sha256()
        for offset in range(0, self.size, chunk_size):
            digest.update(
                self.read(
                    path,
                    offset,
                    min(chunk_size, self.size - offset),
                )
            )
        return "sha256:" + digest.hexdigest()


def inspect_zip_tensor(path: str | Path) -> ZipTensorRange:
    archive_path = Path(path)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = [
                entry
                for entry in archive.infolist()
                if entry.filename.split("/")[-2:-1] == ["data"]
            ]
            if len(entries) != 1:
                raise DcpRangeError(
                    f"{archive_path} must contain exactly one data/* entry"
                )
            entry = entries[0]
            header_offset = entry.header_offset
            compression = entry.compress_type
            size = entry.file_size
            name = entry.filename
    except (OSError, zipfile.BadZipFile) as exc:
        raise DcpRangeError(f"could not inspect DCP zip {path}: {exc}") from exc

    try:
        with archive_path.open("rb") as stream:
            stream.seek(header_offset)
            header = stream.read(30)
    except OSError as exc:
        raise DcpRangeError(f"could not read DCP zip header {path}: {exc}") from exc
    if len(header) != 30:
        raise DcpRangeError(f"short DCP zip header: {path}")
    fields = struct.unpack("<IHHHHHIIIHH", header)
    if fields[0] != 0x04034B50:
        raise DcpRangeError(f"invalid DCP zip local header: {path}")
    name_length, extra_length = fields[9], fields[10]
    data_offset = header_offset + 30 + name_length + extra_length
    return ZipTensorRange(
        name=name,
        data_offset=data_offset,
        size=int(size),
        compression=int(compression),
    )


def _read_compressed(
    path: str | Path,
    name: str,
    offset: int,
    length: int,
) -> bytes:
    try:
        with zipfile.ZipFile(path) as archive, archive.open(name) as stream:
            if offset:
                stream.seek(offset)
            return stream.read(length)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise DcpRangeError(
            f"could not read compressed DCP range {path}:{name}: {exc}"
        ) from exc
