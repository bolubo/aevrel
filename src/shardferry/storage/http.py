from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

from ..gpu_transport import (
    RANGE_ACCEPT_HEADER,
    RANGE_ENCODING,
    RANGE_ENCODING_HEADER,
    RANGE_RAW_LENGTH_HEADER,
    RangeCodec,
)
from ..gpu_batch import AdaptiveBatchCodec, BATCH_ENCODING
from .file import StorageError


class HttpRateLimitError(StorageError):
    """HTTP 429 with an optional Retry-After hint."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
        rate_registry=None,
        range_codec: RangeCodec | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.token = token
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = max(0.0, backoff)
        self.use_proxy = use_proxy
        self.rate_controller = rate_controller
        self.rate_registry = rate_registry
        self.range_codec = range_codec
        self.batch_codec = (
            AdaptiveBatchCodec(range_codec.adaptive)
            if range_codec is not None
            else None
        )
        self._connections: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
        self._ssl_context = _tls_context_from_env()
        handlers = []
        if self._ssl_context is not None:
            handlers.append(HTTPSHandler(context=self._ssl_context))
        if not use_proxy:
            handlers.append(ProxyHandler({}))
        self._direct_opener = build_opener(*handlers) if handlers else None

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
            encoding = response.headers.get(RANGE_ENCODING_HEADER)
            raw_length = response.headers.get(RANGE_RAW_LENGTH_HEADER)
        finally:
            response.close()
        if encoding:
            if self.range_codec is None:
                raise StorageError(
                    f"HTTP response uses unsupported range encoding: {encoding}"
                )
            if encoding != RANGE_ENCODING:
                raise StorageError(
                    f"HTTP response uses unknown range encoding: {encoding}"
                )
            try:
                data = self.range_codec.decode(data)
            except Exception as exc:
                raise StorageError(f"range decode failed: {exc}") from exc
            if raw_length is not None and int(raw_length) != len(data):
                raise StorageError(
                    f"decoded range length {len(data)} != {raw_length}"
                )
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

    @property
    def batch_enabled(self) -> bool:
        return self.batch_codec is not None

    def read_batch(
        self,
        requests: list[tuple[str, int, int]],
    ) -> list[bytes]:
        if self.batch_codec is None:
            raise StorageError("HTTP batch requires a range codec")
        if not requests:
            return []
        urls = [urlsplit(self._url(uri)) for uri, _, _ in requests]
        origins = {(url.scheme, url.netloc) for url in urls}
        if len(origins) != 1:
            raise StorageError("HTTP batch requires one source origin")
        body = {
            "ranges": [
                {
                    "chunk": url.path.rstrip("/").rsplit("/", 1)[-1],
                    "offset": offset,
                    "length": size,
                }
                for url, (_, offset, size) in zip(urls, requests)
            ]
        }
        origin = next(iter(origins))
        url = f"{origin[0]}://{origin[1]}/v1/shards/batch"
        response = self._request(
            "POST",
            url,
            json.dumps(body).encode("utf-8"),
        )
        try:
            data = response.read()
            encoding = response.headers.get(RANGE_ENCODING_HEADER)
        finally:
            response.close()
        if encoding != BATCH_ENCODING:
            raise StorageError(
                f"HTTP batch response uses unknown encoding: {encoding}"
            )
        try:
            chunks = self.batch_codec.decode(data)
        except Exception as exc:
            raise StorageError(f"batch decode failed: {exc}") from exc
        if len(chunks) != len(requests):
            raise StorageError(
                f"batch returned {len(chunks)} ranges, "
                f"expected {len(requests)}"
            )
        for chunk, (_, _, size) in zip(chunks, requests):
            if len(chunk) != size:
                raise StorageError(
                    f"batch range size {len(chunk)} != {size}"
                )
        return chunks

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

    def _open(self, method: str, url: str, body: bytes | None = None):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.range_codec is not None:
            headers[RANGE_ACCEPT_HEADER] = RANGE_ENCODING
        parsed = urlsplit(url)
        if not self.use_proxy and parsed.scheme in {"http", "https"}:
            return self._open_pooled(method, url, parsed, headers, body)
        request = Request(url, method=method, headers=headers, data=body)
        try:
            if self._direct_opener is not None:
                return self._direct_opener.open(request, timeout=self.timeout)
            return urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code == 429:
                raise HttpRateLimitError(
                    f"HTTP 429: {url}",
                    retry_after=_parse_retry_after(
                        exc.headers.get("Retry-After")
                    ),
                ) from exc
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
        body: bytes | None = None,
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
                    context=self._ssl_context,
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
            connection.request(method, path, body=body, headers=headers)
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
            retry_after = _parse_retry_after(
                response.getheader("Retry-After")
            )
            response.read()
            response.close()
            self._connections.pop(key, None)
            try:
                connection.close()
            except OSError:
                pass
            if status == 429:
                raise HttpRateLimitError(
                    f"HTTP 429: {url}",
                    retry_after=retry_after,
                )
            raise StorageError(f"HTTP {status}: {url}")
        return response

    def _request(self, method: str, url: str, body: bytes | None = None):
        last_error: StorageError | None = None
        retry_delay = 0.0
        limiter = self._rate_limiter(url)
        for attempt in range(self.retries + 1):
            try:
                if limiter is not None:
                    limiter.acquire(1.0, timeout=self.timeout)
                response = (
                    self._open(method, url, body)
                    if body is not None
                    else self._open(method, url)
                )
                if limiter is not None:
                    limiter.on_success()
                return response
            except StorageError as exc:
                if limiter is not None:
                    if "HTTP 429" in str(exc):
                        retry_after = (
                            exc.retry_after
                            if isinstance(exc, HttpRateLimitError)
                            else None
                        )
                        limiter.on_throttle(retry_after)
                        retry_delay = min(30.0, retry_after or 0.0)
                    elif "HTTP 5" in str(exc):
                        limiter.on_error()
                if "HTTP 404" in str(exc) or "HTTP 416" in str(exc):
                    raise
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(max(self.backoff * (2**attempt), retry_delay))
                retry_delay = 0.0
        if last_error is not None:
            raise last_error
        raise StorageError(f"HTTP request failed: {url}")

    def _rate_limiter(self, url: str):
        if self.rate_registry is not None:
            return self.rate_registry.for_url(url)
        if hasattr(self.rate_controller, "for_url"):
            return self.rate_controller.for_url(url)
        return self.rate_controller


def _tls_context_from_env() -> ssl.SSLContext | None:
    ca = os.environ.get("SHARDFERRY_TLS_CA", "")
    cert = os.environ.get("SHARDFERRY_TLS_CERT", "")
    key = os.environ.get("SHARDFERRY_TLS_KEY", "")
    if not (ca or cert or key):
        return None
    if bool(cert) != bool(key):
        raise StorageError("TLS cert and key are required together")
    context = ssl.create_default_context(cafile=ca or None)
    if cert:
        context.load_cert_chain(cert, key)
    return context


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
