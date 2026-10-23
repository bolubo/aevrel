from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
import pytest

from aevrel.agent import build_agent_server
from aevrel.dcp import (
    export_dcp,
    inventory_dcp,
    inventory_dcp_descriptors,
    read_dcp_chunk,
)
from aevrel.dcp.range import DcpRangeError, ZipTensorRange, inspect_zip_tensor
from aevrel.executor import apply
from aevrel.planning import plan
from tests.fixtures.generated import (
    checksum,
    generate_sharded_spec,
    materialize_source_files,
)


def _dcp_checkpoint(tmp_path: Path):
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2,),
        ("Shard(0)",),
        (1,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    output = tmp_path / "checkpoint.dcp"
    export_dcp(spec, output)
    return output


def _dcp_checkpoint_multi(tmp_path: Path):
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2,),
        ("Shard(0)",),
        (1,),
        ("Shard(0)",),
    )
    original_shards = deepcopy(spec["source"]["shards"])
    spec["checkpoint"]["tensors"].append(
        {"name": "b", "shape": [8, 4], "dtype": "float32"}
    )
    for shard in original_shards:
        extra = deepcopy(shard)
        extra["tensor"] = "b"
        extra["uri"] = shard["uri"].replace("rank", "b-rank")
        extra["checksum"] = checksum(
            arrays[("w", shard["rank"])] + np.float32(1)
        )
        spec["source"]["shards"].append(extra)
        arrays[("b", shard["rank"])] = (
            arrays[("w", shard["rank"])] + np.float32(1)
        )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    output = tmp_path / "checkpoint-multi.dcp"
    export_dcp(spec, output)
    return output


def test_zip_tensor_range_matches_full_dcp_chunk(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _dcp_checkpoint(tmp_path)
    _, refs = inventory_dcp_descriptors(checkpoint, 0)
    ref = next(iter(refs.values()))
    storage_path = checkpoint / ref.relative_path

    tensor_range = inspect_zip_tensor(storage_path)
    raw = read_dcp_chunk(checkpoint, ref)

    assert tensor_range.size == ref.size
    assert tensor_range.checksum(storage_path) == ref.checksum
    assert tensor_range.read(storage_path, 1, 17) == raw[1:18]


def test_zip_tensor_range_reads_a_segment_inside_a_concatenated_file(
    tmp_path: Path,
) -> None:
    def zip_segment(data: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            archive.writestr("__0_0/data/0", data)
        return buffer.getvalue()

    first = zip_segment(b"first")
    second = zip_segment(b"second!")
    path = tmp_path / "segments.distcp"
    path.write_bytes(first + second)

    first_range = inspect_zip_tensor(path, base_offset=0, length=len(first))
    second_range = inspect_zip_tensor(
        path,
        base_offset=len(first),
        length=len(second),
    )

    assert first_range.read(path, 0, 5) == b"first"
    assert second_range.read(path, 0, 7) == b"second!"


def test_inventory_does_not_require_torch_chunk_deserialization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pytest.importorskip("torch")
    checkpoint = _dcp_checkpoint(tmp_path)

    def fail_deserialize(*args, **kwargs):
        raise AssertionError("inventory must not deserialize a DCP chunk")

    monkeypatch.setattr(
        "aevrel.dcp.checkpoint._read_storage_tensor",
        fail_deserialize,
    )
    inventory, refs = inventory_dcp(checkpoint, 0)
    assert inventory["chunks"]
    assert refs


def _local_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def test_agent_http_multi_chunk_range_checksum(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _dcp_checkpoint_multi(tmp_path)
    server = build_agent_server(
        "0.0.0.0:0",
        "secret",
        tmp_path / "agent-state",
        dcp_checkpoint=checkpoint,
        public_url="http://source",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{_local_ip()}:{server.server_address[1]}"
        inventory_request = Request(
            base + "/v1/inventory?rank=0",
            headers={"Authorization": "Bearer secret"},
        )
        inventory = json.loads(urlopen(inventory_request).read())
        assert len(inventory["chunks"]) >= 2
        for chunk in inventory["chunks"]:
            chunk_url = chunk["file"].replace("http://source", base, 1)
            request = Request(
                chunk_url + "?offset=0&length=1",
                headers={"Authorization": "Bearer secret"},
            )
            with urlopen(request) as response:
                data = response.read()
                expected = response.headers["X-Range-SHA256"]
            assert data
            assert expected == "sha256:" + hashlib.sha256(data).hexdigest()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_zip_range_failure_modes(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _dcp_checkpoint(tmp_path)
    _, refs = inventory_dcp_descriptors(checkpoint, 0)
    ref = next(iter(refs.values()))
    storage_path = checkpoint / ref.relative_path
    tensor_range = inspect_zip_tensor(storage_path)

    with pytest.raises(DcpRangeError, match="outside"):
        tensor_range.read(storage_path, ref.size, 1)

    broken = tmp_path / "broken.distcp"
    broken.write_bytes(b"not a zip")
    with pytest.raises(DcpRangeError, match="inspect DCP zip"):
        inspect_zip_tensor(broken)


def test_inventory_multiple_chunks_for_one_rank(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _dcp_checkpoint_multi(tmp_path)
    inventory, refs = inventory_dcp(checkpoint, 0)
    assert len(inventory["chunks"]) >= 2
    assert len(refs) >= 2
    assert len({chunk["id"] for chunk in inventory["chunks"]}) == len(
        inventory["chunks"]
    )


def test_inventory_handles_real_gpu_concatenated_dcp() -> None:
    pytest.importorskip("torch")
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "gpu-b-4to3-source"
    )
    for rank in range(4):
        inventory, refs = inventory_dcp_descriptors(fixture, rank)
        assert inventory["tensors"]
        assert inventory["chunks"]
        assert refs


def test_compressed_zip_has_explicit_fallback(tmp_path: Path) -> None:
    path = tmp_path / "compressed.distcp"
    payload = b"abcdefgh"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__0_0/data/0", payload)

    tensor_range = inspect_zip_tensor(path)
    assert tensor_range.compression == zipfile.ZIP_DEFLATED
    assert tensor_range.read(path, 2, 3) == payload[2:5]


def test_inspect_zip_tensor_reads_storage_segment(tmp_path: Path) -> None:
    def archive_bytes(payload: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("__0_0/data/0", payload)
        return buffer.getvalue()

    first = archive_bytes(b"first!!!")
    second = archive_bytes(b"second!!")
    path = tmp_path / "concatenated.distcp"
    path.write_bytes(first + second)

    tensor_range = inspect_zip_tensor(
        path,
        base_offset=len(first),
        length=len(second),
    )
    assert tensor_range.read(path, 0, 8) == b"second!!"


def test_export_dcp_does_not_read_target_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pytest.importorskip("torch")
    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2,),
        ("Shard(0)",),
        (1,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)

    def fail_read_bytes(self):
        raise AssertionError("export must not read the whole target shard")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    output = tmp_path / "streamed.dcp"
    export_dcp(spec, output)
    assert (output / ".metadata").is_file()
    assert list(output.glob("*.distcp"))


def test_short_stored_range_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "short.bin"
    path.write_bytes(b"short")
    tensor_range = ZipTensorRange(
        name="data/0",
        data_offset=0,
        size=10,
        compression=zipfile.ZIP_STORED,
    )
    with pytest.raises(DcpRangeError, match="short DCP range"):
        tensor_range.read(path, 0, 10)


def test_chunk_content_change_is_detected_by_checksum(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    checkpoint = _dcp_checkpoint(tmp_path)
    _, refs = inventory_dcp_descriptors(checkpoint, 0)
    ref = next(iter(refs.values()))
    storage_path = checkpoint / ref.relative_path
    tensor_range = inspect_zip_tensor(storage_path)
    before = tensor_range.checksum(storage_path)

    with storage_path.open("r+b") as stream:
        stream.seek(tensor_range.data_offset)
        original = stream.read(1)
        stream.seek(tensor_range.data_offset)
        stream.write(bytes([original[0] ^ 0xFF]))

    try:
        after = tensor_range.checksum(storage_path)
        assert after != before
        with pytest.raises(Exception, match="checksum changed"):
            read_dcp_chunk(checkpoint, ref)
    finally:
        with storage_path.open("r+b") as stream:
            stream.seek(tensor_range.data_offset)
            stream.write(original)
