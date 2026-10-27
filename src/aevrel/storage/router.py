from __future__ import annotations

import os
from urllib.parse import urlsplit

from ..adaptive import AdaptiveRateRegistry
from ..gpu_transport import make_range_codec_from_env
from .file import FileStorage, StorageError
from .http import HttpStorage
from .dcpzip import DcpZipStorage


class StorageRouter:
    """Dispatch source operations to file:// or http(s):// storage."""

    def __init__(
        self,
        source_base: str | None = None,
        token: str | None = None,
        rate_controller=None,
        rate_registry: AdaptiveRateRegistry | None = None,
        range_codec=None,
    ) -> None:
        if rate_controller is None and rate_registry is None:
            configured = os.environ.get("AEVREL_RATE_LIMIT_RPS", "")
            if configured:
                try:
                    rate_registry = AdaptiveRateRegistry(rate=float(configured))
                except ValueError:
                    rate_registry = None
        self.file = FileStorage()
        self.dcpzip = DcpZipStorage()
        self.range_codec = (
            range_codec
            if range_codec is not None
            else make_range_codec_from_env()
        )
        self.http = HttpStorage(
            base_url=source_base,
            token=token,
            rate_controller=rate_controller,
            rate_registry=rate_registry,
            range_codec=self.range_codec,
        )

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        return self._storage(uri).read_range(uri, offset, size)

    @property
    def batch_enabled(self) -> bool:
        return bool(self.range_codec and self.http.batch_enabled)

    def read_batch(
        self,
        requests: list[tuple[str, int, int]],
    ) -> list[bytes]:
        if not self.batch_enabled:
            raise StorageError("batch reads are not configured")
        if not requests:
            return []
        if any(
            urlsplit(uri).scheme not in {"http", "https"}
            for uri, _, _ in requests
        ):
            raise StorageError("batch reads require HTTP source URIs")
        return self.http.read_batch(requests)

    def exists(self, uri: str) -> bool:
        return self._storage(uri).exists(uri)

    def size(self, uri: str) -> int:
        return self._storage(uri).size(uri)

    def checksum(self, uri: str) -> str:
        return self._storage(uri).checksum(uri)

    def _storage(self, uri: str):
        parts = urlsplit(uri)
        scheme = parts.scheme
        if scheme == "file":
            return self.file
        if scheme == "dcpzip":
            return self.dcpzip
        if scheme in {"http", "https"}:
            return self.http
        # A scheme-less relative reference is the only thing a configured
        # source base may resolve — that is what the base is for. Everything
        # else (s3://, ftp://, a protocol-relative //host/path, an empty URI)
        # is sent to nobody: handing it to the HTTP backend would build a
        # request against that base and fail far away from the actual mistake.
        if (
            scheme == ""
            and not parts.netloc
            and parts.path
            and self.http.base_url is not None
        ):
            return self.http
        raise StorageError(f"unsupported storage URI: {uri}")
