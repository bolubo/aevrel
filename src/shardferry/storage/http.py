from __future__ import annotations

import hashlib
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .file import StorageError


class HttpStorage:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 0.1,
        use_proxy: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.token = token
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.use_proxy = use_proxy
        self._direct_opener = (
            None if use_proxy else build_opener(ProxyHandler({}))
        )

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise StorageError("HTTP range must be non-negative")
        url = self._url(uri)
        parsed = urlsplit(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((("offset", str(offset)), ("length", str(size))))
        request_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )
        response = self._request("GET", request_url)
        try:
            data = response.read()
            expected_range = response.headers.get("X-Range-SHA256")
        finally:
            response.close()
        if len(data) != size:
            raise StorageError(
                f"short HTTP range from {uri}: wanted {size}, got {len(data)}"
            )
        if expected_range is not None:
            actual = "sha256:" + hashlib.sha256(data).hexdigest()
            if actual != expected_range:
                raise StorageError(
                    f"HTTP range checksum mismatch from {uri}: "
                    f"{actual} != {expected_range}"
                )
        return data

    def exists(self, uri: str) -> bool:
        try:
            response = self._request("HEAD", self._url(uri))
        except StorageError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
        response.close()
        return True

    def size(self, uri: str) -> int:
        response = self._request("HEAD", self._url(uri))
        try:
            value = response.headers.get("Content-Length")
        finally:
            response.close()
        if value is None:
            raise StorageError(f"HTTP response has no Content-Length: {uri}")
        return int(value)

    def checksum(self, uri: str) -> str:
        response = self._request("HEAD", self._url(uri))
        try:
            value = response.headers.get("X-Shard-SHA256")
            if value:
                return value
            size = int(response.headers["Content-Length"])
        finally:
            response.close()
        data = self.read_range(uri, 0, size)
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def _url(self, uri: str) -> str:
        if uri.startswith(("http://", "https://")):
            return uri
        if self.base_url is None:
            raise StorageError(f"HTTP URI is not absolute: {uri}")
        return self.base_url + "/" + uri.lstrip("/")

    def _open(self, method: str, url: str):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(url, method=method, headers=headers)
        try:
            if self._direct_opener is not None:
                return self._direct_opener.open(request, timeout=self.timeout)
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            raise StorageError(f"HTTP {exc.code}: {url}") from exc
        except URLError as exc:
            raise StorageError(f"HTTP request failed: {url}: {exc.reason}") from exc
        except OSError as exc:
            raise StorageError(f"HTTP request failed: {url}: {exc}") from exc

    def _request(self, method: str, url: str):
        last_error: StorageError | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._open(method, url)
            except StorageError as exc:
                if "HTTP 404" in str(exc) or "HTTP 416" in str(exc):
                    raise
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(self.backoff * (2**attempt))
        if last_error is not None:
            raise last_error
        raise StorageError(f"HTTP request failed: {url}")
