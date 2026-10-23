from __future__ import annotations

import io
import json
import mmap
import pickle
import re
import shutil
import sys
import uuid
import warnings
import zipfile
from dataclasses import dataclass
from math import prod
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from urllib.parse import quote

import yaml

from .. import __version__
from ..defaults import (
    DEFAULT_MIGRATED_SUFFIX,
    DEFAULT_SOURCE_PLACEMENTS,
    DEFAULT_TARGET_PLACEMENTS,
    DEFAULT_WORK_SUFFIX,
)
from .range import DcpRangeError, ZipTensorRange, inspect_zip_tensor
from ..executor import apply as execute_plan
from ..identity import source_id
from ..planning.layout import DTYPE_BYTES, local_region
from ..manifest import validate_spec
from ..planning.mesh import parse_mesh
from ..planning import plan as build_plan
from ..storage.file import FileStorage
from ..targets import target_file_path


class DcpError(RuntimeError):
    """Raised when a DCP checkpoint cannot be read or written."""


@dataclass(frozen=True)
class DcpChunkRef:
    relative_path: str
    offset: int
    length: int
    checksum: str
    size: int
    data_offset: int = 0
    data_size: int = 0
    data_compression: int = -1
    data_name: str = ""


def _torch():
    try:
        import torch
    except ImportError as exc:
        raise DcpError(
            "PyTorch is required for DCP support; install with: "
            'pip install "shardferry[dcp]"'
        ) from exc
    return torch


def _dtype_name(torch_module, dtype) -> str:
    mapping = {
        torch_module.float32: "float32",
        torch_module.float16: "float16",
        torch_module.bfloat16: "bfloat16",
        torch_module.int64: "int64",
        torch_module.int32: "int32",
        torch_module.uint8: "uint8",
    }
    try:
        return mapping[dtype]
    except KeyError as exc:
        raise DcpError(f"unsupported DCP dtype: {dtype}") from exc


def _torch_dtype(torch_module, name: str):
    mapping = {
        "float32": torch_module.float32,
        "float16": torch_module.float16,
        "bfloat16": torch_module.bfloat16,
        "int64": torch_module.int64,
        "int32": torch_module.int32,
        "uint8": torch_module.uint8,
    }
    try:
        return mapping[name]
    except KeyError as exc:
        raise DcpError(f"unsupported DCP dtype: {name}") from exc


def _tensor_bytes(tensor) -> bytes:
    torch = _torch()
    contiguous = tensor.detach().cpu().contiguous()
    return contiguous.view(torch.uint8).numpy().tobytes()


def _save_mmap_tensor(
    source_path: Path,
    output_path: Path,
    dtype,
    shape: tuple[int, ...],
) -> None:
    torch = _torch()
    tensor = None
    mapped = None
    stream = None
    try:
        if prod(shape) == 0:
            tensor = torch.empty(shape, dtype=dtype)
            data_pkl = _pickle_tensor(torch, tensor)
        else:
            stream = source_path.open("rb")
            mapped = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_COPY)
            tensor = torch.frombuffer(mapped, dtype=dtype).reshape(shape)
            data_pkl = _pickle_tensor(torch, tensor)
        _write_dcp_zip(
            output_path,
            prefix=output_path.stem,
            data_pkl=data_pkl,
            source_path=source_path,
            size=prod(shape) * tensor.element_size(),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise DcpError(f"could not stream DCP tensor {source_path}: {exc}") from exc
    finally:
        del tensor
        if mapped is not None:
            mapped.close()
        if stream is not None:
            stream.close()


def _pickle_tensor(torch, tensor) -> bytes:
    storage_classes = {
        torch.float32: torch.FloatStorage,
        torch.float16: torch.HalfStorage,
        torch.bfloat16: torch.BFloat16Storage,
        torch.int64: torch.LongStorage,
        torch.int32: torch.IntStorage,
        torch.uint8: torch.ByteStorage,
    }

    class _StoragePickler(pickle.Pickler):
        def persistent_id(self, obj):
            if isinstance(obj, torch.storage.TypedStorage):
                storage_class = storage_classes.get(obj.dtype)
                if storage_class is None:
                    raise DcpError(f"unsupported DCP storage dtype: {obj.dtype}")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    storage_elements = obj.nbytes() // obj.element_size()
                return (
                    "storage",
                    storage_class,
                    "0",
                    "cpu",
                    storage_elements,
                )
            return None

    output = io.BytesIO()
    _StoragePickler(output, protocol=2).dump(tensor)
    return output.getvalue()


def _write_dcp_zip(
    output_path: Path,
    *,
    prefix: str,
    data_pkl: bytes,
    source_path: Path,
    size: int,
) -> None:
    serialization_id = str(uuid.uuid4().int)[:40].ljust(40, "0")
    with zipfile.ZipFile(
        output_path,
        "w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as archive:
        archive.writestr(f"{prefix}/data.pkl", data_pkl)
        archive.writestr(f"{prefix}/.format_version", "1")
        archive.writestr(f"{prefix}/.storage_alignment", "64")
        archive.writestr(f"{prefix}/byteorder", sys.byteorder)
        archive.writestr(f"{prefix}/version", "3\n")
        archive.writestr(f"{prefix}/.data/serialization_id", serialization_id)
        with archive.open(
            f"{prefix}/data/0",
            "w",
            force_zip64=True,
        ) as target:
            remaining = size
            if remaining == 0:
                return
            with source_path.open("rb") as source:
                while remaining:
                    chunk = source.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        raise DcpError(
                            f"short source read for {source_path}: "
                            f"{remaining} bytes missing"
                        )
                    target.write(chunk)
                    remaining -= len(chunk)


def _read_storage_tensor(checkpoint: Path, info) -> Any:
    torch = _torch()
    path = checkpoint / info.relative_path
    try:
        with path.open("rb") as stream:
            stream.seek(info.offset)
            data = stream.read(info.length)
    except OSError as exc:
        raise DcpError(f"could not read DCP storage {path}: {exc}") from exc
    if len(data) != info.length:
        raise DcpError(f"short DCP storage read: {path}")
    try:
        return torch.load(
            io.BytesIO(data),
            map_location="cpu",
            weights_only=False,
        )
    except Exception as exc:
        raise DcpError(f"could not deserialize DCP chunk {path}: {exc}") from exc


def _rank_from_path(relative_path: str) -> int:
    name = Path(relative_path).name
    match = re.match(r"__(\d+)_", name)
    if not match:
        raise DcpError(f"cannot infer rank from DCP file: {relative_path}")
    return int(match.group(1))


def read_dcp_metadata(checkpoint: str | Path):
    torch = _torch()
    from torch.distributed.checkpoint import FileSystemReader

    del torch
    path = Path(checkpoint).resolve()
    if not path.is_dir():
        raise DcpError(f"DCP checkpoint directory not found: {path}")
    try:
        return FileSystemReader(str(path)).read_metadata()
    except Exception as exc:
        raise DcpError(f"could not read DCP metadata from {path}: {exc}") from exc


def local_dcp_ranks(
    checkpoint: str | Path,
    *,
    metadata_dir: str | Path | None = None,
    allow_missing_metadata: bool = False,
) -> list[int]:
    """Return ranks whose DCP storage files exist under this checkpoint root."""
    checkpoint_path = Path(checkpoint).resolve()
    metadata_path = Path(metadata_dir or checkpoint_path).resolve()
    if allow_missing_metadata and not (metadata_path / ".metadata").is_file():
        return _local_dcp_ranks_from_files(checkpoint_path)
    metadata = read_dcp_metadata(metadata_path)
    ranks: set[int] = set()
    for info in (metadata.storage_data or {}).values():
        relative = getattr(info, "relative_path", "")
        if not relative:
            continue
        if not (checkpoint_path / relative).is_file():
            continue
        try:
            ranks.add(_rank_from_path(relative))
        except DcpError:
            continue
    if not ranks:
        raise DcpError(
            f"no local DCP rank files found under {checkpoint_path}"
        )
    return sorted(ranks)


def _local_dcp_ranks_from_files(checkpoint: str | Path) -> list[int]:
    """Best-effort rank discovery before the metadata owner is available."""
    checkpoint_path = Path(checkpoint).resolve()
    ranks: set[int] = set()
    for path in checkpoint_path.rglob("*.distcp"):
        match = re.match(r"__(\d+)_", path.name)
        if match:
            ranks.add(int(match.group(1)))
    if not ranks:
        raise DcpError(
            f"no local DCP rank files found under {checkpoint_path}"
        )
    return sorted(ranks)


def inspect_dcp(
    checkpoint: str | Path,
    work_dir: str | Path | None = None,
    *,
    source_mesh_shape: tuple[int, ...] | None = None,
    source_placements: tuple[str, ...] = DEFAULT_SOURCE_PLACEMENTS,
    target_world_size: int | None = None,
    target_mesh_shape: tuple[int, ...] | None = None,
    target_placements: tuple[str, ...] = DEFAULT_TARGET_PLACEMENTS,
) -> dict[str, Any]:
    torch = _torch()
    checkpoint_path = Path(checkpoint).resolve()
    work_path = (
        Path(work_dir).resolve()
        if work_dir is not None
        else checkpoint_path.with_name(
            checkpoint_path.name + DEFAULT_WORK_SUFFIX
        )
    )
    source_dir = work_path / "source"
    target_dir = work_path / "target"
    source_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    metadata = read_dcp_metadata(checkpoint_path)
    storage_data = metadata.storage_data or {}
    tensor_rows: dict[str, dict[str, Any]] = {}
    source_shards: list[dict[str, Any]] = []
    ranks: set[int] = set()

    for fqn, tensor_metadata in metadata.state_dict_metadata.items():
        if (
            not hasattr(tensor_metadata, "chunks")
            or len(tensor_metadata.size) == 0
        ):
            continue
        dtype = _dtype_name(torch, tensor_metadata.properties.dtype)
        global_shape = tuple(int(value) for value in tensor_metadata.size)
        tensor_rows[fqn] = {
            "name": fqn,
            "shape": list(global_shape),
            "dtype": dtype,
        }
        seen_chunks: set[int] = set()
        for index, chunk in enumerate(tensor_metadata.chunks):
            candidates = [
                (metadata_index, info)
                for metadata_index, info in storage_data.items()
                if metadata_index.fqn == fqn
            ]
            matching = [
                item for item in candidates if item[0].index == index
            ]
            if not matching:
                matching = [
                    item
                    for item in candidates
                    if tuple(item[0].offset or ()) == tuple(chunk.offsets)
                ]
            if not matching:
                raise DcpError(f"no storage entry for DCP chunk {fqn}/{index}")
            metadata_index, info = matching[0]
            rank = _rank_from_path(info.relative_path)
            key = (fqn, rank)
            if rank in seen_chunks:
                raise DcpError(
                    f"multiple DCP chunks for {fqn}/rank{rank} are not supported"
                )
            seen_chunks.add(rank)
            ranks.add(rank)
            chunk_tensor = _read_storage_tensor(checkpoint_path, info)
            if tuple(chunk_tensor.shape) != tuple(chunk.sizes):
                raise DcpError(
                    f"DCP chunk shape mismatch for {fqn}/{rank}: "
                    f"{chunk_tensor.shape} != {chunk.sizes}"
                )
            raw_path = (
                source_dir
                / quote(fqn, safe="")
                / f"rank{rank}.bin"
            )
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_bytes = _tensor_bytes(chunk_tensor)
            raw_path.write_bytes(raw_bytes)
            source_shards.append(
                {
                    "tensor": fqn,
                    "rank": rank,
                    "uri": raw_path.as_uri(),
                    "offset": [int(value) for value in chunk.offsets],
                    "size": [int(value) for value in chunk.sizes],
                    "checksum": "sha256:" + _sha256(raw_bytes),
                }
            )

    if not source_shards:
        raise DcpError("DCP checkpoint has no tensor chunks")
    source_world = max(ranks) + 1
    if source_mesh_shape is None:
        source_mesh_shape = (source_world,)
    if len(source_mesh_shape) != len(source_placements):
        raise DcpError("source mesh shape and placements have different ranks")
    if target_world_size is None:
        target_world_size = source_world
    if target_mesh_shape is None:
        target_mesh_shape = (target_world_size,)
    if len(target_mesh_shape) != len(target_placements):
        raise DcpError("target mesh shape and placements have different ranks")

    spec = {
        "version": "1",
        "checkpoint": {
            "format": "dcp",
            "tensors": list(tensor_rows.values()),
        },
        "source": {
            "mesh": _mesh_payload(source_mesh_shape, source_placements),
            "storage": {"uri": source_dir.as_uri()},
            "shards": sorted(source_shards, key=lambda row: (row["tensor"], row["rank"])),
        },
        "target": {
            "mesh": _mesh_payload(target_mesh_shape, target_placements),
            "storage": {"uri": target_dir.as_uri()},
        },
    }
    validate_spec(spec)
    (work_path / "migration.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False),
        encoding="utf-8",
    )
    return spec


def inventory_dcp(
    checkpoint: str | Path,
    rank: int,
    *,
    metadata_dir: str | Path | None = None,
    file_prefix: str = "dcp",
) -> tuple[dict[str, Any], dict[str, bytes]]:
    inventory, refs = inventory_dcp_descriptors(
        checkpoint,
        rank,
        metadata_dir=metadata_dir,
        file_prefix=file_prefix,
    )
    return (
        inventory,
        {
            key: read_dcp_chunk(checkpoint, ref)
            for key, ref in refs.items()
        },
    )


def inventory_dcp_descriptors(
    checkpoint: str | Path,
    rank: int,
    *,
    metadata_dir: str | Path | None = None,
    file_prefix: str = "dcp",
) -> tuple[dict[str, Any], dict[str, DcpChunkRef]]:
    """Build an inventory plus lazy chunk descriptors for one local rank."""
    if rank < 0:
        raise DcpError("DCP rank must be non-negative")
    torch = _torch()
    checkpoint_path = Path(checkpoint).resolve()
    metadata_path = (
        Path(metadata_dir).resolve()
        if metadata_dir is not None
        else checkpoint_path
    )
    metadata = read_dcp_metadata(metadata_path)
    metadata_file = metadata_path / ".metadata"
    try:
        metadata_bytes = metadata_file.read_bytes()
    except OSError as exc:
        raise DcpError(
            f"could not read DCP metadata {metadata_file}: {exc}"
        ) from exc

    storage_data = metadata.storage_data or {}
    tensors: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    refs: dict[str, DcpChunkRef] = {}
    for fqn, tensor_metadata in metadata.state_dict_metadata.items():
        if (
            not hasattr(tensor_metadata, "chunks")
            or len(tensor_metadata.size) == 0
        ):
            continue
        tensors.append(
            {
                "name": fqn,
                "shape": [int(value) for value in tensor_metadata.size],
                "dtype": _dtype_name(
                    torch,
                    tensor_metadata.properties.dtype,
                ),
            }
        )
        for index, chunk in enumerate(tensor_metadata.chunks):
            candidates = [
                (metadata_index, info)
                for metadata_index, info in storage_data.items()
                if metadata_index.fqn == fqn
            ]
            matching = [
                item for item in candidates if item[0].index == index
            ]
            if not matching:
                matching = [
                    item
                    for item in candidates
                    if tuple(item[0].offset or ()) == tuple(chunk.offsets)
                ]
            if not matching:
                raise DcpError(
                    f"no storage entry for DCP chunk {fqn}/{index}"
                )
            _, info = matching[0]
            if _rank_from_path(info.relative_path) != rank:
                continue
            storage_path = checkpoint_path / info.relative_path
            try:
                tensor_range = inspect_zip_tensor(
                    storage_path,
                    base_offset=int(info.offset),
                    length=int(info.length),
                )
            except DcpRangeError as exc:
                raise DcpError(
                    f"could not inspect DCP zip {storage_path}: {exc}"
                ) from exc
            expected_size = (
                int(prod(chunk.sizes))
                * DTYPE_BYTES[
                    _dtype_name(torch, tensor_metadata.properties.dtype)
                ]
            )
            if tensor_range.size < expected_size:
                raise DcpError(
                    f"DCP chunk size mismatch for {fqn}/rank{rank}: "
                    f"{tensor_range.size} != {expected_size}"
                )
            raw = tensor_range.read(storage_path, 0, expected_size)
            digest = "sha256:" + _sha256(raw)
            refs[digest] = DcpChunkRef(
                relative_path=info.relative_path,
                offset=int(info.offset),
                length=int(info.length),
                checksum=digest,
                size=expected_size,
                data_offset=tensor_range.data_offset,
                data_size=expected_size,
                data_compression=tensor_range.compression,
                data_name=tensor_range.name,
            )
            chunks.append(
                {
                    "id": source_id(
                        fqn,
                        rank,
                        chunk.offsets,
                        chunk.sizes,
                        digest,
                    ),
                    "tensor": fqn,
                    "rank": rank,
                    "file": (
                        f"{file_prefix.rstrip('/')}/"
                        f"{quote(digest, safe=':')}"
                    ),
                    "tensor_offset": [
                        int(value) for value in chunk.offsets
                    ],
                    "tensor_size": [int(value) for value in chunk.sizes],
                    "byte_ranges": [[0, expected_size]],
                    "checksum": digest,
                }
            )
    return (
        {
            "version": "1",
            "rank": rank,
            "format": "dcp",
            "metadata_hash": "sha256:" + _sha256(metadata_bytes),
            "tensors": tensors,
            "chunks": chunks,
            "agent_version": __version__,
            "torch_version": str(torch.__version__),
        },
        refs,
    )


def read_dcp_chunk(
    checkpoint: str | Path,
    ref: DcpChunkRef,
) -> bytes:
    if ref.data_size:
        raw = read_dcp_chunk_range(checkpoint, ref, 0, ref.size)
        digest = "sha256:" + _sha256(raw)
        if digest != ref.checksum:
            raise DcpError(
                f"DCP chunk checksum changed for {ref.relative_path}: "
                f"{digest} != {ref.checksum}"
            )
        return raw
    chunk_tensor = _read_storage_tensor(
        Path(checkpoint).resolve(),
        SimpleNamespace(
            relative_path=ref.relative_path,
            offset=ref.offset,
            length=ref.length,
        ),
    )
    raw = _tensor_bytes(chunk_tensor)
    if len(raw) != ref.size:
        raise DcpError(
            f"DCP chunk size changed for {ref.relative_path}: "
            f"{len(raw)} != {ref.size}"
        )
    digest = "sha256:" + _sha256(raw)
    if digest != ref.checksum:
        raise DcpError(
            f"DCP chunk checksum changed for {ref.relative_path}: "
            f"{digest} != {ref.checksum}"
        )
    return raw


def read_dcp_tensor_chunk(
    checkpoint: str | Path,
    ref: DcpChunkRef,
):
    """Deserialize one local DCP chunk without loading a global tensor."""
    return _read_storage_tensor(
        Path(checkpoint).resolve(),
        SimpleNamespace(
            relative_path=ref.relative_path,
            offset=ref.offset,
            length=ref.length,
        ),
    )


def read_dcp_chunk_range(
    checkpoint: str | Path,
    ref: DcpChunkRef,
    offset: int,
    length: int,
) -> bytes:
    if offset < 0 or length < 0 or offset + length > ref.size:
        raise DcpError(
            f"DCP chunk range is out of bounds: {offset}+{length} > {ref.size}"
        )
    if not ref.data_size:
        return read_dcp_chunk(checkpoint, ref)[offset : offset + length]
    tensor_range = ZipTensorRange(
        name=ref.data_name,
        data_offset=ref.data_offset,
        size=ref.data_size,
        compression=ref.data_compression,
    )
    try:
        return tensor_range.read(
            Path(checkpoint).resolve() / ref.relative_path,
            offset,
            length,
        )
    except DcpRangeError as exc:
        raise DcpError(
            f"could not read DCP range {ref.relative_path}: {exc}"
        ) from exc


def export_dcp(
    spec: dict[str, Any],
    output_dir: str | Path,
    *,
    force: bool = False,
    source_checkpoint: str | Path | None = None,
) -> Path:
    torch = _torch()
    from torch.distributed.checkpoint.filesystem import _StorageInfo
    from torch.distributed.checkpoint.metadata import (
        ChunkStorageMetadata,
        Metadata,
        MetadataIndex,
        StorageMeta,
        TensorProperties,
        TensorStorageMetadata,
    )

    validate_spec(spec)
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()) and not force:
        raise DcpError(f"output DCP directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    target_storage = FileStorage().path(spec["target"]["storage"]["uri"])
    target_mesh = parse_mesh(spec["target"]["mesh"])
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    storage_data = {}
    state_dict_metadata = {}
    planner_data = {}

    for tensor_index, (fqn, tensor) in enumerate(tensors.items()):
        torch_dtype = _torch_dtype(torch, tensor["dtype"])
        shape = tuple(tensor["shape"])
        chunks = []
        for rank in range(target_mesh.world_size):
            region = local_region(
                target_mesh,
                target_mesh.placements,
                shape,
                rank,
                allow_partial=True,
            )
            raw_path = target_file_path(spec, fqn, rank)
            if not raw_path.is_file():
                raise DcpError(f"target shard not found: {raw_path}")
            expected = region.element_count * DTYPE_BYTES[tensor["dtype"]]
            actual_size = raw_path.stat().st_size
            if actual_size != expected:
                raise DcpError(
                    f"target shard size mismatch for {fqn}/rank{rank}: "
                    f"{actual_size} != {expected}"
                )
            file_name = f"__{rank}_{tensor_index}.distcp"
            chunk_path = output / file_name
            _save_mmap_tensor(
                raw_path,
                chunk_path,
                torch_dtype,
                region.size,
            )
            storage_data[
                MetadataIndex(
                    fqn=fqn,
                    offset=torch.Size(region.offset),
                    index=rank,
                )
            ] = _StorageInfo(
                relative_path=file_name,
                offset=0,
                length=chunk_path.stat().st_size,
            )
            chunks.append(
                ChunkStorageMetadata(
                    offsets=torch.Size(region.offset),
                    sizes=torch.Size(region.size),
                )
            )
        state_dict_metadata[fqn] = TensorStorageMetadata(
            properties=TensorProperties(dtype=torch_dtype),
            size=torch.Size(shape),
            chunks=chunks,
        )
        planner_data[fqn] = (fqn,)

    metadata = Metadata(
        state_dict_metadata=state_dict_metadata,
        planner_data=planner_data,
        storage_data=storage_data,
        storage_meta=StorageMeta(
            checkpoint_id=output,
            save_id=uuid.uuid4().hex,
        ),
        version="1.0.0",
    )
    with (output / ".metadata").open("wb") as stream:
        pickle.dump(metadata, stream)
    _copy_bytes_metadata(source_checkpoint, output)
    return output


def _copy_bytes_metadata(
    source_checkpoint: str | Path | None,
    output: str | Path,
) -> None:
    """Preserve DCP scalar/bytes entries that do not have tensor chunks."""
    if source_checkpoint is None:
        return
    from torch.distributed.checkpoint.metadata import BytesStorageMetadata

    source = Path(source_checkpoint).resolve()
    target = Path(output).resolve()
    source_metadata = read_dcp_metadata(source)
    target_metadata = read_dcp_metadata(target)
    preserved = []
    for fqn, value in source_metadata.state_dict_metadata.items():
        if isinstance(value, BytesStorageMetadata):
            preserved.append((fqn, value))
        elif hasattr(value, "chunks") and len(value.size) == 0:
            preserved.append((fqn, value))
    if not preserved:
        return
    storage_data = target_metadata.storage_data or {}
    copy_index = 0
    for fqn, value in preserved:
        entries = [
            (metadata_index, info)
            for metadata_index, info in (source_metadata.storage_data or {}).items()
            if metadata_index.fqn == fqn
        ]
        if not entries:
            raise DcpError(f"no storage entry for preserved DCP entry {fqn}")
        target_metadata.state_dict_metadata[fqn] = value
        for key, info in entries:
            source_path = source / info.relative_path
            try:
                with source_path.open("rb") as stream:
                    stream.seek(int(info.offset))
                    raw = stream.read(int(info.length))
            except OSError as exc:
                raise DcpError(
                    f"could not read preserved DCP entry {fqn}: {exc}"
                ) from exc
            if len(raw) != int(info.length):
                raise DcpError(f"short preserved DCP entry: {fqn}")
            file_name = f"__preserved_{copy_index}.distcp"
            copy_index += 1
            (target / file_name).write_bytes(raw)
            storage_data[
                type(key)(
                    fqn=key.fqn,
                    offset=key.offset,
                    index=key.index,
                )
            ] = type(info)(
                relative_path=file_name,
                offset=0,
                length=len(raw),
                transform_descriptors=None,
            )
    target_metadata.storage_data = storage_data
    with (target / ".metadata").open("wb") as stream:
        pickle.dump(target_metadata, stream)


def export_dcp_local(
    spec: dict[str, Any],
    output_dir: str | Path,
    *,
    raw_root: str | Path | None = None,
    ranks: Iterable[int] | None = None,
    force: bool = False,
) -> Path:
    """Export a loadable DCP directory for a subset of target ranks.

    This is the local-disk target-agent variant: each node owns a subset of
    target ranks, so its metadata intentionally contains only the chunks that
    are present under that node's target root.
    """
    torch = _torch()
    from torch.distributed.checkpoint.filesystem import _StorageInfo
    from torch.distributed.checkpoint.metadata import (
        ChunkStorageMetadata,
        Metadata,
        MetadataIndex,
        StorageMeta,
        TensorProperties,
        TensorStorageMetadata,
    )

    validate_spec(spec)
    output = Path(output_dir).resolve()
    raw = Path(raw_root).resolve() if raw_root is not None else output
    if output.exists() and any(output.iterdir()) and not force:
        raise DcpError(f"output DCP directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    target_mesh = parse_mesh(spec["target"]["mesh"])
    selected = sorted(set(ranks or range(target_mesh.world_size)))
    for rank in selected:
        if not 0 <= rank < target_mesh.world_size:
            raise DcpError(f"target rank {rank} is out of range")
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    storage_data = {}
    state_dict_metadata = {}
    planner_data = {}
    for fqn, tensor in tensors.items():
        torch_dtype = _torch_dtype(torch, tensor["dtype"])
        chunks = []
        for rank in selected:
            region = local_region(
                target_mesh,
                target_mesh.placements,
                tuple(tensor["shape"]),
                rank,
                allow_partial=True,
            )
            raw_path = target_file_path(spec, fqn, rank, root=raw)
            if not raw_path.is_file():
                raise DcpError(f"target shard not found: {raw_path}")
            expected = region.element_count * DTYPE_BYTES[tensor["dtype"]]
            actual = raw_path.stat().st_size
            if actual != expected:
                raise DcpError(
                    f"target shard size mismatch for {fqn}/rank{rank}: "
                    f"{actual} != {expected}"
                )
            tensor_index = list(tensors).index(fqn)
            file_name = f"__{rank}_{tensor_index}.distcp"
            chunk_path = output / file_name
            _save_mmap_tensor(
                raw_path,
                chunk_path,
                torch_dtype,
                region.size,
            )
            storage_data[
                MetadataIndex(
                    fqn=fqn,
                    offset=torch.Size(region.offset),
                    index=rank,
                )
            ] = _StorageInfo(
                relative_path=file_name,
                offset=0,
                length=chunk_path.stat().st_size,
            )
            chunks.append(
                ChunkStorageMetadata(
                    offsets=torch.Size(region.offset),
                    sizes=torch.Size(region.size),
                )
            )
        state_dict_metadata[fqn] = TensorStorageMetadata(
            properties=TensorProperties(dtype=torch_dtype),
            size=torch.Size(tensor["shape"]),
            chunks=chunks,
        )
        planner_data[fqn] = (fqn,)
    metadata = Metadata(
        state_dict_metadata=state_dict_metadata,
        planner_data=planner_data,
        storage_data=storage_data,
        storage_meta=StorageMeta(
            checkpoint_id=output,
            save_id=uuid.uuid4().hex,
        ),
        version="1.0.0",
    )
    with (output / ".metadata").open("wb") as stream:
        pickle.dump(metadata, stream)
    return output


def migrate_dcp(
    checkpoint: str | Path,
    output: str | Path | None = None,
    work_dir: str | Path | None = None,
    *,
    source_mesh_shape: tuple[int, ...] | None = None,
    source_placements: tuple[str, ...] = DEFAULT_SOURCE_PLACEMENTS,
    target_world_size: int | None = None,
    target_mesh_shape: tuple[int, ...] | None = None,
    target_placements: tuple[str, ...] = DEFAULT_TARGET_PLACEMENTS,
    force: bool = False,
    keep_work: bool = False,
    progress: Callable[[str], None] | None = None,
    progress_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def notify(message: str) -> None:
        if progress is not None:
            progress(message)

    checkpoint_path = Path(checkpoint).resolve()
    work_path = (
        Path(work_dir).resolve()
        if work_dir is not None
        else checkpoint_path.with_name(
            checkpoint_path.name + DEFAULT_WORK_SUFFIX
        )
    )
    output_path = (
        Path(output).resolve()
        if output is not None
        else checkpoint_path.with_name(
            checkpoint_path.name + DEFAULT_MIGRATED_SUFFIX
        )
    )
    if _is_within(work_path, checkpoint_path):
        raise DcpError(
            "work directory must be outside the source checkpoint: "
            f"{work_path}"
        )
    if (
        _is_within(output_path, checkpoint_path)
        or _is_within(checkpoint_path, output_path)
    ):
        raise DcpError(
            "output directory must not overlap the source checkpoint: "
            f"{output_path}"
        )
    if (
        _is_within(output_path, work_path)
        or _is_within(work_path, output_path)
    ):
        raise DcpError(
            "output and work directories must be separate: "
            f"{output_path} vs {work_path}"
        )
    if output_path.exists() and any(output_path.iterdir()) and not force:
        raise DcpError(f"output DCP directory is not empty: {output_path}")
    notify("reading DCP metadata and indexing local shards")
    from .local import build_local_range_spec

    spec = build_local_range_spec(
        checkpoint_path,
        target_storage_uri=(work_path / "target").as_uri(),
        source_mesh_shape=source_mesh_shape,
        source_placements=source_placements,
        target_world_size=target_world_size,
        target_mesh_shape=target_mesh_shape,
        target_placements=target_placements,
    )
    source_world = int(spec["source"]["mesh"]["world_size"])
    target_world = int(spec["target"]["mesh"]["world_size"])
    notify(f"planning {source_world} -> {target_world} target ranks")
    plan_payload = build_plan(spec)
    notify(
        f"transferring {len(plan_payload['nodes'])} chunks "
        f"({plan_payload['total_bytes']} bytes)"
    )
    report = execute_plan(plan_payload, spec, progress=progress_event)
    (work_path / "plan.json").write_text(
        json.dumps(plan_payload, indent=2),
        encoding="utf-8",
    )
    (work_path / "report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    notify("verification passed; writing target DCP")
    export_dcp(
        spec,
        output_path,
        force=force,
        source_checkpoint=checkpoint_path,
    )
    if not keep_work:
        target_work = (work_path / "target").resolve()
        if target_work.exists() and target_work.is_dir():
            shutil.rmtree(target_work)
    notify("migration complete")
    return {
        "spec": spec,
        "plan": plan_payload,
        "report": report,
        "output": str(output_path),
        "work_dir": str(work_path),
    }


def _mesh_payload(
    shape: tuple[int, ...],
    placements: tuple[str, ...],
) -> dict[str, Any]:
    from math import prod

    return {
        "shape": list(shape),
        "world_size": prod(shape),
        "placements": list(placements),
    }


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
