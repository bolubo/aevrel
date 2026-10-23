from __future__ import annotations

import hashlib
import struct

import pytest

from aevrel.gpu_codec import (
    CodecError,
    IdentityCodec,
    NvcompCodec,
    ZlibCodec,
    decode_frame,
    encode_frame,
    make_codec,
    nvcomp_status,
)


def test_identity_frame_roundtrip_preserves_bytes() -> None:
    data = b"checkpoint-range-data" * 128
    frame = encode_frame(IdentityCodec(), data)

    assert frame.startswith(b"SFG1")
    assert decode_frame(frame, {"identity": IdentityCodec()}) == data


def test_zlib_frame_roundtrip_for_sparse_data() -> None:
    data = (b"checkpoint" * 1024) + bytes(1024 * 1024)
    codec = ZlibCodec(level=3)
    frame = encode_frame(codec, data)

    assert len(frame) < len(data)
    assert decode_frame(frame, {"zlib": codec}) == data


def test_decode_frame_rejects_corrupted_payload() -> None:
    codec = ZlibCodec(level=3)
    frame = bytearray(encode_frame(codec, b"payload" * 1024))
    frame[-1] ^= 0xFF

    with pytest.raises(CodecError, match="decode|checksum"):
        decode_frame(bytes(frame), {"zlib": codec})


def test_decode_frame_rejects_truncated_frame() -> None:
    codec = ZlibCodec(level=3)
    frame = encode_frame(codec, b"payload" * 128)

    with pytest.raises(CodecError, match="frame"):
        decode_frame(frame[:-1], {"zlib": codec})


def test_decode_frame_rejects_unknown_codec() -> None:
    frame = encode_frame(IdentityCodec(), b"payload")

    with pytest.raises(CodecError, match="codec"):
        decode_frame(frame, {})


def test_decode_frame_rejects_wrong_raw_checksum() -> None:
    codec = ZlibCodec(level=3)
    data = b"payload" * 64
    frame = bytearray(encode_frame(codec, data))
    # Header: magic, version, codec name length, raw size, raw SHA256, payload size.
    header = struct.Struct(">4sBBQ32sQ")
    frame[header.size - 8 - 32] ^= 0xFF

    with pytest.raises(CodecError, match="checksum"):
        decode_frame(bytes(frame), {"zlib": codec})


def test_make_codec_names_are_explicit() -> None:
    assert make_codec("identity").name == "identity"
    assert make_codec("zlib").name == "zlib"
    assert make_codec("nvcomp-zstd").name == "nvcomp-zstd"
    with pytest.raises(CodecError, match="unsupported"):
        make_codec("brotli")


def test_nvcomp_status_is_read_only_on_cpu() -> None:
    status = nvcomp_status()
    assert {
        "importable",
        "cuda_available",
        "driver_available",
        "driver_devices",
        "version",
        "error",
    } <= set(status)
    if not status["importable"]:
        assert status["error"]


def test_nvcomp_codec_does_not_claim_gpu_when_cuda_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aevrel.gpu_codec.nvcomp_status",
        lambda: {
            "importable": True,
            "cuda_available": False,
            "driver_available": False,
            "driver_devices": 0,
            "version": "6.0.0",
            "error": None,
        },
    )
    codec = NvcompCodec("Zstd")
    assert codec.available() is False


def test_nvcomp_codec_can_use_cuda_driver_without_torch_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aevrel.gpu_codec.nvcomp_status",
        lambda: {
            "importable": True,
            "cuda_available": False,
            "driver_available": True,
            "driver_devices": 1,
            "version": "6.0.0",
            "error": None,
        },
    )
    codec = NvcompCodec("Zstd")
    assert codec.available() is True


def test_frame_contains_raw_sha256() -> None:
    data = b"abc" * 100
    frame = encode_frame(IdentityCodec(), data)
    header = struct.Struct(">4sBBQ32sQ")
    _, version, _, raw_size, raw_sha256, payload_size = header.unpack_from(frame)

    assert version == 1
    assert raw_size == len(data)
    assert raw_sha256 == hashlib.sha256(data).digest()
    assert payload_size == len(data)
