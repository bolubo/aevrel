from __future__ import annotations


class S3Storage:
    def __init__(self, endpoint: str, bucket: str) -> None:
        self.endpoint = endpoint
        self.bucket = bucket

    def read_range(self, uri: str, offset: int, size: int) -> bytes:
        raise NotImplementedError("Phase 4: optional S3 relay read")

    def write_range(self, uri: str, offset: int, data: bytes) -> None:
        raise NotImplementedError("Phase 4: optional S3 relay write")
