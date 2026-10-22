from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ..dcp_range import DcpRangeError, ZipTensorRange
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

    def exists(self, uri: str) -> bool:
        return self._path(uri).is_file()

    def size(self, uri: str) -> int:
        return self._range(uri).size

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        tensor_range = self._range(uri)
        try:
            return tensor_range.read(self._path(uri), offset, size)
        except DcpRangeError as exc:
            raise StorageError(
                f"could not read DCP range {uri}: {exc}"
            ) from exc

    def checksum(self, uri: str) -> str:
        tensor_range = self._range(uri)
        try:
            return tensor_range.checksum(self._path(uri))
        except DcpRangeError as exc:
            raise StorageError(
                f"could not checksum DCP range {uri}: {exc}"
            ) from exc

    def _path(self, uri: str) -> Path:
        parsed = urlsplit(uri)
        if parsed.scheme != URI_SCHEME:
            raise StorageError(f"not a dcpzip URI: {uri}")
        path = unquote(parsed.path)
        if parsed.netloc:
            path = unquote(parsed.netloc) + path
        return Path(path)

    def _range(self, uri: str) -> ZipTensorRange:
        parsed = urlsplit(uri)
        if parsed.scheme != URI_SCHEME:
            raise StorageError(f"not a dcpzip URI: {uri}")
        query = parse_qs(parsed.query)
        try:
            return ZipTensorRange(
                name=query["name"][0],
                data_offset=int(query["offset"][0]),
                size=int(query["size"][0]),
                compression=int(query["compression"][0]),
            )
        except (KeyError, ValueError) as exc:
            raise StorageError(f"invalid dcpzip URI: {uri}") from exc
