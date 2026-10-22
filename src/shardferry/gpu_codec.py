from __future__ import annotations

import ctypes
import hashlib
import struct
import threading
import zlib
from typing import Any, Mapping, Protocol


class CodecError(ValueError):
    """Raised when a codec or framed payload is invalid."""


class Codec(Protocol):
    name: str

    def encode(self, data: bytes) -> bytes:
        ...

    def decode(self, data: bytes) -> bytes:
        ...


class IdentityCodec:
    name = "identity"

    def encode(self, data: bytes) -> bytes:
        return bytes(data)

    def decode(self, data: bytes) -> bytes:
        return bytes(data)


class ZlibCodec:
    name = "zlib"

    def __init__(self, level: int = 3) -> None:
        if level < 0 or level > 9:
            raise ValueError("zlib level must be between 0 and 9")
        self.level = level

    def encode(self, data: bytes) -> bytes:
        return zlib.compress(bytes(data), level=self.level)

    def decode(self, data: bytes) -> bytes:
        try:
            return zlib.decompress(bytes(data))
        except zlib.error as exc:
            raise CodecError(f"zlib decode failed: {exc}") from exc


def nvcomp_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "importable": False,
        "cuda_available": False,
        "driver_available": False,
        "driver_devices": 0,
        "version": None,
        "error": None,
    }
    try:
        import nvidia.nvcomp as nvcomp

        status["importable"] = True
        status["version"] = nvcomp.__version__
    except Exception as exc:
        status["error"] = str(exc)
    try:
        import torch

        status["cuda_available"] = bool(torch.cuda.is_available())
        status["device_count"] = int(torch.cuda.device_count())
        status["devices"] = [
            torch.cuda.get_device_name(index)
            for index in range(torch.cuda.device_count())
        ]
    except Exception as exc:
        status["torch_error"] = str(exc)
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
        cuda.cuInit.argtypes = [ctypes.c_uint]
        cuda.cuInit.restype = ctypes.c_int
        cuda.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        cuda.cuDeviceGetCount.restype = ctypes.c_int
        if cuda.cuInit(0) == 0:
            count = ctypes.c_int()
            if cuda.cuDeviceGetCount(ctypes.byref(count)) == 0:
                status["driver_devices"] = max(0, int(count.value))
                status["driver_available"] = status["driver_devices"] > 0
    except Exception as exc:
        status["driver_error"] = str(exc)
    return status


class NvcompCodec:
    def __init__(
        self,
        algorithm: str = "Zstd",
        *,
        device_id: int = 0,
        **options: Any,
    ) -> None:
        self.algorithm = algorithm
        self.device_id = device_id
        self.options = options
        self._codec = None
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return "nvcomp-" + self.algorithm.lower()

    def available(self) -> bool:
        status = nvcomp_status()
        return bool(
            status["importable"]
            and (
                status["cuda_available"]
                or status.get("driver_available", False)
            )
        )

    def _load(self):
        try:
            import nvidia.nvcomp as nvcomp
        except Exception as exc:
            raise CodecError(
                "nvidia-nvcomp-cu12 is not installed; "
                "install the optional gpu dependency"
            ) from exc
        status = nvcomp_status()
        if not (
            status["cuda_available"]
            or status.get("driver_available", False)
        ):
            raise CodecError("nvcomp requires a CUDA device")
        return nvcomp

    def _configured(self):
        if self._codec is None:
            nvcomp = self._load()
            self._codec = nvcomp.Codec(
                algorithm=self.algorithm,
                device_id=self.device_id,
                **self.options,
            )
        return self._codec

    def encode(self, data: bytes) -> bytes:
        raw = bytes(data)
        if not raw:
            raise CodecError("nvcomp cannot encode an empty buffer")
        with self._lock:
            nvcomp = self._load()
            codec = self._configured()
            source = nvcomp.as_array(raw).cuda()
            encoded = codec.encode(source)
            return bytes(encoded.cpu())

    def decode(self, data: bytes) -> bytes:
        encoded = bytes(data)
        if not encoded:
            raise CodecError("nvcomp cannot decode an empty buffer")
        with self._lock:
            nvcomp = self._load()
            codec = self._configured()
            source = nvcomp.as_array(encoded).cuda()
            decoded = codec.decode(source)
            return bytes(decoded.cpu())

    def synchronize(self) -> None:
        if not self.available():
            return
        try:
            import torch
        except Exception:
            return
        if torch.cuda.is_available():
            torch.cuda.synchronize(self.device_id)


FRAME_MAGIC = b"SFG1"
FRAME_VERSION = 1
FRAME_HEADER = struct.Struct(">4sBBQ32sQ")
MAX_CODEC_NAME_BYTES = 64
DEFAULT_MAX_RAW_BYTES = 512 * 1024 * 1024


def encode_frame(codec: Codec, data: bytes) -> bytes:
    raw = bytes(data)
    codec_name = codec.name.encode("utf-8")
    if not codec_name or len(codec_name) > MAX_CODEC_NAME_BYTES:
        raise CodecError(f"invalid codec name: {codec.name!r}")
    payload = bytes(codec.encode(raw))
    return (
        FRAME_HEADER.pack(
            FRAME_MAGIC,
            FRAME_VERSION,
            len(codec_name),
            len(raw),
            hashlib.sha256(raw).digest(),
            len(payload),
        )
        + codec_name
        + payload
    )


def decode_frame(
    frame: bytes,
    codecs: Mapping[str, Codec],
    *,
    max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
) -> bytes:
    raw_frame = bytes(frame)
    if len(raw_frame) < FRAME_HEADER.size:
        raise CodecError("truncated codec frame")
    (
        magic,
        version,
        codec_name_length,
        raw_size,
        expected_sha256,
        payload_size,
    ) = FRAME_HEADER.unpack_from(raw_frame)
    if magic != FRAME_MAGIC:
        raise CodecError("invalid codec frame magic")
    if version != FRAME_VERSION:
        raise CodecError(f"unsupported codec frame version: {version}")
    if codec_name_length < 1 or codec_name_length > MAX_CODEC_NAME_BYTES:
        raise CodecError("invalid codec name length")
    if raw_size > max_raw_bytes:
        raise CodecError(
            f"raw payload is too large: {raw_size} > {max_raw_bytes}"
        )
    expected_size = FRAME_HEADER.size + codec_name_length + payload_size
    if len(raw_frame) != expected_size:
        raise CodecError(
            f"invalid codec frame size: {len(raw_frame)} != {expected_size}"
        )
    name_start = FRAME_HEADER.size
    name_end = name_start + codec_name_length
    try:
        codec_name = raw_frame[name_start:name_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodecError("codec name is not valid UTF-8") from exc
    codec = codecs.get(codec_name)
    if codec is None:
        raise CodecError(f"unknown codec: {codec_name}")
    payload = raw_frame[name_end:]
    try:
        decoded = bytes(codec.decode(payload))
    except CodecError:
        raise
    except Exception as exc:
        raise CodecError(f"codec {codec_name!r} decode failed: {exc}") from exc
    if len(decoded) != raw_size:
        raise CodecError(
            f"decoded size mismatch: {len(decoded)} != {raw_size}"
        )
    actual_sha256 = hashlib.sha256(decoded).digest()
    if actual_sha256 != expected_sha256:
        raise CodecError("decoded checksum mismatch")
    return decoded


def make_codec(name: str) -> Codec:
    normalized = name.strip().lower()
    if normalized == "identity":
        return IdentityCodec()
    if normalized == "zlib":
        return ZlibCodec()
    algorithms = {
        "nvcomp-zstd": "Zstd",
        "nvcomp-lz4": "LZ4",
        "nvcomp-gdeflate": "GDeflate",
    }
    algorithm = algorithms.get(normalized)
    if algorithm is not None:
        return NvcompCodec(algorithm)
    raise CodecError(f"unsupported codec: {name}")
