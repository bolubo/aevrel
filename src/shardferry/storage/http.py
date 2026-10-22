from __future__ import annotations

import hashlib
import http.client
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
        rate_controller=None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.token = token
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.use_proxy = use_proxy
        self.rate_controller = rate_controller
        self._connections: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
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
        parsed = urlsplit(url)
        if not self.use_proxy and parsed.scheme in {"http", "https"}:
            return self._open_pooled(method, url, parsed, headers)
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

    def _open_pooled(
        self,
        method: str,
        url: str,
        parsed,
        headers: dict[str, str],
    ):
        host = parsed.hostname
        if host is None:
            raise StorageError(f"HTTP URL has no host: {url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        key = (parsed.scheme, host, port)
        connection = self._connections.get(key)
        if connection is None:
            if parsed.scheme == "https":
                connection = http.client.HTTPSConnection(
                    host,
                    port,
                    timeout=self.timeout,
                )
            else:
                connection = http.client.HTTPConnection(
                    host,
                    port,
                    timeout=self.timeout,
                )
            self._connections[key] = connection
        headers["Connection"] = "keep-alive"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            self._connections.pop(key, None)
            try:
                connection.close()
            except OSError:
                pass
            raise StorageError(f"HTTP request failed: {url}: {exc}") from exc
        if response.status >= 400:
            status = response.status
            response.read()
            response.close()
            self._connections.pop(key, None)
            try:
                connection.close()
            except OSError:
                pass
            raise StorageError(f"HTTP {status}: {url}")
        return response

    def _request(self, method: str, url: str):
        last_error: StorageError | None = None
        for attempt in range(self.retries + 1):
            try:
                if self.rate_controller is not None:
                    self.rate_controller.acquire(1.0, timeout=self.timeout)
                response = self._open(method, url)
                if self.rate_controller is not None:
                    self.rate_controller.on_success()
                return response
            except StorageError as exc:
                if self.rate_controller is not None:
                    if "HTTP 429" in str(exc):
                        self.rate_controller.on_throttle()
                    elif "HTTP 5" in str(exc):
                        self.rate_controller.on_error()
                if "HTTP 404" in str(exc) or "HTTP 416" in str(exc):
                    raise
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(self.backoff * (2**attempt))
        if last_error is not None:
            raise last_error
        raise StorageError(f"HTTP request failed: {url}")
