from __future__ import annotations

import os
from urllib.parse import urlsplit

from ..adaptive import AdaptiveRateRegistry
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
    ) -> None:
        if rate_controller is None and rate_registry is None:
            configured = os.environ.get("SHARDFERRY_RATE_LIMIT_RPS", "")
            if configured:
                try:
                    rate_registry = AdaptiveRateRegistry(rate=float(configured))
                except ValueError:
                    rate_registry = None
        self.file = FileStorage()
        self.dcpzip = DcpZipStorage()
        self.http = HttpStorage(
            base_url=source_base,
            token=token,
            rate_controller=rate_controller,
            rate_registry=rate_registry,
        )

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        return self._storage(uri).read_range(uri, offset, size)

    def exists(self, uri: str) -> bool:
        return self._storage(uri).exists(uri)

    def size(self, uri: str) -> int:
        return self._storage(uri).size(uri)

    def checksum(self, uri: str) -> str:
        return self._storage(uri).checksum(uri)

    def _storage(self, uri: str):
        scheme = urlsplit(uri).scheme
        if scheme == "file":
            return self.file
        if scheme == "dcpzip":
            return self.dcpzip
        if scheme in {"http", "https"}:
            return self.http
        if self.http.base_url is not None:
            return self.http
        raise StorageError(f"unsupported storage URI: {uri}")
