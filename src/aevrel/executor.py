from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from .identity import resolve_source, source_maps
from .planning.layout import DTYPE_BYTES, Region, local_region
from .planning.mesh import parse_mesh
from .planning import validate_plan
from .resources import resource_path
from .storage.file import FileStorage
from .storage.router import StorageRouter
from .targets import journal_path, staged_file_path, target_file_path
from .planning.tensor_io import (
    byte_runs,
    paired_run_batches,
    reduce_bytes,
    verify_source_checksums,
)
from .verification import verify


REPORT_SCHEMA_PATH = resource_path("report.schema.json")


class ExecutionError(RuntimeError):
    """Raised when a local transfer cannot be completed."""


@dataclass(frozen=True)
class TargetFile:
    tensor: str
    rank: int
    region: Region
    size_bytes: int
    final_path: Path
    stage_path: Path


class _Journal:
    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def create(cls, path: Path, plan_id: str) -> "_Journal":
        journal = cls(
            path,
            {
                "version": "1",
                "plan_id": plan_id,
                "status": "in_progress",
                "completed_nodes": {},
                "committed_files": [],
                "resumes": 0,
            },
        )
        journal.write()
        return journal

    @classmethod
    def load(cls, path: Path, plan_id: str) -> "_Journal":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExecutionError(f"could not read journal {path}: {exc}") from exc
        if data.get("version") != "1" or data.get("plan_id") != plan_id:
            raise ExecutionError(f"journal does not match plan: {path}")
        journal = cls(path, data)
        if journal.data.get("status") != "committed":
            journal.data["resumes"] = int(journal.data.get("resumes", 0)) + 1
            journal.write()
        return journal

    @property
    def completed(self) -> set[str]:
        return set(self.data.get("completed_nodes", {}))

    @property
    def bytes_moved(self) -> int:
        return sum(
            int(value)
            for value in self.data.get("completed_nodes", {}).values()
        )

    def mark_node(self, node_id: str, node_bytes: int) -> None:
        self.data["completed_nodes"][node_id] = node_bytes
        self.write()

    def mark_committed(self) -> None:
        self.data["status"] = "committed"
        self.write()

    def mark_verified(self, verification: dict[str, Any]) -> None:
        self.data["verification"] = verification
        self.data["status"] = "verified"
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise ExecutionError(f"could not write journal {self.path}: {exc}") from exc


def apply(
    plan: dict[str, Any],
    spec: dict[str, Any],
    source: str | None = None,
    dry_run: bool = False,
    *,
    target_rank: int | None = None,
    target_root: str | Path | None = None,
    commit: bool = True,
    token: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    def emit(event: dict[str, Any]) -> None:
        if progress is not None:
            progress(event)

    validate_plan(plan, spec)
    target_mesh = parse_mesh(spec["target"]["mesh"])
    if target_rank is not None and not 0 <= target_rank < target_mesh.world_size:
        raise ExecutionError(f"target rank {target_rank} is out of range")
    if source is not None and not source.startswith(
        ("file://", "http://", "https://")
    ):
        raise ExecutionError("source must be a file or HTTP URI")

    source_storage = StorageRouter(source_base=source, token=token)
    target_storage = FileStorage()
    selected_nodes = _selected_nodes(plan, target_rank)
    selected_bytes = _selected_bytes(spec, selected_nodes)
    targets = _target_files(
        spec,
        plan["migration_id"],
        target_rank=target_rank,
        target_root=target_root,
    )
    journal_file = journal_path(
        spec, plan["migration_id"], root=target_root
    )
    if not dry_run:
        _preflight_targets(targets, journal_file)

    verify_source_checksums(spec, plan, source_storage, target_rank=target_rank)
    if dry_run:
        result = {
            "version": "1",
            "plan_id": plan["migration_id"],
            "status": "success",
            "meshes": _meshes(spec),
            "tensors": _tensor_report(spec, selected_nodes),
            "chunks": len(selected_nodes),
            "bytes_planned": selected_bytes,
            "bytes_moved": 0,
            "duration_seconds": time.perf_counter() - started,
            "retries": 0,
            "verification": {
                "passed": True,
                "method": "checksum",
                "details": "dry run: source checksums and plan verified",
            },
        }
        if target_rank is not None:
            result["target_rank"] = target_rank
        _validate_report(result)
        return result

    target_map = {(target.tensor, target.rank): target for target in targets}

    if journal_file.is_file():
        journal = _Journal.load(journal_file, plan["migration_id"])
    else:
        existing = [
            target.final_path
            for target in targets
            if target.final_path.exists()
        ]
        if existing:
            raise ExecutionError(
                f"target already exists without a journal: {existing[0]}"
            )
        for target in targets:
            target.stage_path.parent.mkdir(parents=True, exist_ok=True)
            target_storage.ensure_size(
                target.stage_path.as_uri(), target.size_bytes
            )
        journal = _Journal.create(journal_file, plan["migration_id"])

    if journal.data.get("status") == "committed":
        verification = verify(
            spec,
            plan,
            target_rank=target_rank,
            target_root=target_root,
            token=token,
        )
        result = _report(
            spec, plan, journal, verification, started, target_rank=target_rank
        )
        _validate_report(result)
        return result

    completed_count = sum(
        1 for node in selected_nodes if node["id"] in journal.completed
    )
    moved_bytes = sum(
        int(journal.data["completed_nodes"].get(node["id"], 0))
        for node in selected_nodes
    )
    emit(
        {
            "phase": "transfer",
            "completed_nodes": completed_count,
            "total_nodes": len(selected_nodes),
            "bytes_moved": moved_bytes,
            "bytes_planned": selected_bytes,
            "resumed": completed_count > 0,
        }
    )

    for target in targets:
        active = _active_path(target)
        if not active.is_file():
            raise ExecutionError(
                f"staging file is missing for {target.tensor}/rank{target.rank}"
            )
        if target_storage.size(active.as_uri()) != target.size_bytes:
            raise ExecutionError(
                f"staging size mismatch for {target.tensor}/rank{target.rank}"
            )

    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    completed = journal.completed
    for node in selected_nodes:
        if node["id"] in completed:
            continue
        target = target_map[(node["tensor"], node["target_rank"])]
        active = _active_path(target)
        node_bytes = _apply_node(
            source_storage,
            target_storage,
            active,
            source_by_id,
            source_by_rank,
            tensors[node["tensor"]],
            node,
            target,
        )
        target_storage.fsync(active.as_uri())
        journal.mark_node(node["id"], node_bytes)
        completed_count += 1
        moved_bytes += node_bytes
        emit(
            {
                "phase": "transfer",
                "completed_nodes": completed_count,
                "total_nodes": len(selected_nodes),
                "bytes_moved": moved_bytes,
                "bytes_planned": selected_bytes,
                "current_tensor": node["tensor"],
                "current_rank": node["target_rank"],
                "current_bytes": node_bytes,
            }
        )

    emit(
        {
            "phase": "verifying",
            "completed_nodes": completed_count,
            "total_nodes": len(selected_nodes),
            "bytes_moved": moved_bytes,
            "bytes_planned": selected_bytes,
        }
    )
    active_paths = {
        key: _active_path(target) for key, target in target_map.items()
    }
    verification = verify(
        spec,
        plan,
        target_rank=target_rank,
        target_paths=active_paths,
        token=token,
    )
    journal.mark_verified(verification)
    emit(
        {
            "phase": "verified",
            "completed_nodes": completed_count,
            "total_nodes": len(selected_nodes),
            "bytes_moved": moved_bytes,
            "bytes_planned": selected_bytes,
        }
    )
    if not commit:
        result = _report(
            spec,
            plan,
            journal,
            verification,
            started,
            target_rank=target_rank,
            status="staged",
        )
        _validate_report(result)
        return result

    emit(
        {
            "phase": "committing",
            "completed_nodes": completed_count,
            "total_nodes": len(selected_nodes),
            "bytes_moved": moved_bytes,
            "bytes_planned": selected_bytes,
        }
    )
    for target in targets:
        if target.stage_path.is_file():
            target.final_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(target.stage_path, target.final_path)
            except OSError as exc:
                raise ExecutionError(
                    f"could not commit {target.final_path}: {exc}"
                ) from exc
        elif not target.final_path.is_file():
            raise ExecutionError(
                f"neither staging nor final target exists for "
                f"{target.tensor}/rank{target.rank}"
            )
    journal.mark_committed()
    emit(
        {
            "phase": "committed",
            "completed_nodes": completed_count,
            "total_nodes": len(selected_nodes),
            "bytes_moved": moved_bytes,
            "bytes_planned": selected_bytes,
        }
    )

    result = _report(
        spec, plan, journal, verification, started, target_rank=target_rank
    )
    _validate_report(result)
    return result


def commit_staged(
    plan: dict[str, Any],
    spec: dict[str, Any],
    *,
    target_rank: int | None = None,
    target_root: str | Path | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    validate_plan(plan, spec)
    target_mesh = parse_mesh(spec["target"]["mesh"])
    if target_rank is not None and not 0 <= target_rank < target_mesh.world_size:
        raise ExecutionError(f"target rank {target_rank} is out of range")

    targets = _target_files(
        spec,
        plan["migration_id"],
        target_rank=target_rank,
        target_root=target_root,
    )
    journal_file = journal_path(spec, plan["migration_id"], root=target_root)
    if not journal_file.is_file():
        raise ExecutionError(f"journal not found: {journal_file}")
    journal = _Journal.load(journal_file, plan["migration_id"])
    verification = journal.data.get("verification")
    if journal.data.get("status") == "committed":
        if not isinstance(verification, dict):
            verification = verify(
                spec,
                plan,
                target_rank=target_rank,
                target_root=target_root,
                token=token,
            )
        result = _report(
            spec, plan, journal, verification, started, target_rank=target_rank
        )
        _validate_report(result)
        return result
    if journal.data.get("status") != "verified":
        raise ExecutionError("target is not staged and verified")

    selected_nodes = _selected_nodes(plan, target_rank)
    missing = [
        node["id"]
        for node in selected_nodes
        if node["id"] not in journal.completed
    ]
    if missing:
        raise ExecutionError(
            "staging is incomplete for nodes: " + ", ".join(missing[:3])
        )
    if not isinstance(verification, dict):
        raise ExecutionError("journal has no verification evidence")

    target_storage = FileStorage()
    for target in targets:
        if target.final_path.exists():
            continue
        if not target.stage_path.is_file():
            raise ExecutionError(
                f"neither staging nor final target exists for "
                f"{target.tensor}/rank{target.rank}"
            )
        if target_storage.size(target.stage_path.as_uri()) != target.size_bytes:
            raise ExecutionError(
                f"staging size mismatch for {target.tensor}/rank{target.rank}"
            )
        target.final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(target.stage_path, target.final_path)
        except OSError as exc:
            raise ExecutionError(
                f"could not commit {target.final_path}: {exc}"
            ) from exc

    journal.mark_committed()
    result = _report(
        spec, plan, journal, verification, started, target_rank=target_rank
    )
    _validate_report(result)
    return result


def _target_files(
    spec: dict[str, Any],
    plan_id: str,
    *,
    target_rank: int | None = None,
    target_root: str | Path | None = None,
) -> list[TargetFile]:
    target_mesh = parse_mesh(spec["target"]["mesh"])
    targets: list[TargetFile] = []
    for tensor in spec["checkpoint"]["tensors"]:
        shape = tuple(tensor["shape"])
        item_size = DTYPE_BYTES[tensor["dtype"]]
        for rank in range(target_mesh.world_size):
            if target_rank is not None and rank != target_rank:
                continue
            region = local_region(
                target_mesh,
                target_mesh.placements,
                shape,
                rank,
                allow_partial=True,
            )
            targets.append(
                TargetFile(
                    tensor=tensor["name"],
                    rank=rank,
                    region=region,
                    size_bytes=region.element_count * item_size,
                    final_path=target_file_path(
                        spec, tensor["name"], rank, root=target_root
                    ),
                    stage_path=staged_file_path(
                        spec, plan_id, tensor["name"], rank, root=target_root
                    ),
                )
            )
    return targets


def _active_path(target: TargetFile) -> Path:
    if target.stage_path.is_file():
        return target.stage_path
    if target.final_path.is_file():
        return target.final_path
    return target.stage_path


def _preflight_targets(
    targets: list[TargetFile],
    journal_file: Path,
) -> None:
    if journal_file.is_file():
        return
    for target in targets:
        parent = target.final_path.parent
        if parent.exists() and not parent.is_dir():
            raise ExecutionError(
                f"target parent is not a directory: {parent}"
            )
        if target.final_path.exists():
            raise ExecutionError(
                f"target already exists without a journal: {target.final_path}"
            )


def _apply_node(
    source_storage: StorageRouter,
    target_storage: FileStorage,
    target_path: Path,
    source_by_id: dict[str, dict[str, Any]],
    source_by_rank: dict[tuple[str, int], list[dict[str, Any]]],
    tensor: dict[str, Any],
    node: dict[str, Any],
    target: TargetFile,
) -> int:
    if node["op"] == "zeros":
        return 0
    bytes_per_item = DTYPE_BYTES[tensor["dtype"]]
    target_local_offset = tuple(
        value - base
        for value, base in zip(node["target_offset"], target.region.offset)
    )
    target_runs = byte_runs(
        target.region.size,
        target_local_offset,
        tuple(node["size"]),
        bytes_per_item,
    )
    source_run_lists = []
    for source_input in node["inputs"]:
        source_shard = resolve_source(
            source_by_id,
            source_by_rank,
            node["tensor"],
            source_input,
        )
        source_local_offset = tuple(
            value - base
            for value, base in zip(
                source_input["source_offset"], source_shard["offset"]
            )
        )
        source_run_lists.append(
            byte_runs(
                tuple(source_shard["size"]),
                source_local_offset,
                tuple(node["size"]),
                bytes_per_item,
            )
        )

    batches = paired_run_batches(target_runs, source_run_lists)
    if node["op"] == "copy" and len(node["inputs"]) == 1:
        source_shard = resolve_source(
            source_by_id,
            source_by_rank,
            node["tensor"],
            node["inputs"][0],
        )
        copied = _copy_local_ranges(
            source_storage,
            target_storage,
            source_shard["uri"],
            target_path,
            batches,
        )
        if copied is not None:
            return copied

    requests: list[tuple[str, int, int]] = []
    batch_plan: list[
        tuple[tuple[int, int], list[int]]
    ] = []
    for target_range, source_ranges in batches:
        indices = []
        for source_input, (source_offset, source_size) in zip(
            node["inputs"], source_ranges
        ):
            shard = resolve_source(
                source_by_id,
                source_by_rank,
                node["tensor"],
                source_input,
            )
            indices.append(len(requests))
            requests.append((shard["uri"], source_offset, source_size))
        batch_plan.append((target_range, indices))

    can_batch = (
        getattr(source_storage, "batch_enabled", False)
        and len(requests) > 1
        and all(
            uri.startswith(("http://", "https://"))
            for uri, _, _ in requests
        )
    )
    if can_batch:
        fetched = source_storage.read_batch(requests)
    else:
        fetched = [
            source_storage.read_range(uri, offset, size)
            for uri, offset, size in requests
        ]

    for target_range, indices in batch_plan:
        target_offset, run_size = target_range
        source_data = [fetched[index] for index in indices]
        data = reduce_bytes(source_data, node["op"], tensor["dtype"])
        target_storage.write_range(target_path.as_uri(), target_offset, data)
    return prod(node["size"]) * bytes_per_item * len(node["inputs"])


def _copy_local_ranges(
    source_storage: StorageRouter,
    target_storage: FileStorage,
    source_uri: str,
    target_path: Path,
    batches: list[tuple[tuple[int, int], list[tuple[int, int]]]],
) -> int | None:
    """Use kernel copy_file_range for local copy nodes when available."""
    if os.environ.get("AEVREL_COPY_FILE_RANGE", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return None
    if not hasattr(target_storage, "copy_range") or not source_uri.startswith("file:"):
        return None
    file_storage = getattr(source_storage, "file", None)
    if file_storage is None:
        return None
    written = 0
    try:
        for target_range, source_ranges in batches:
            target_offset, target_size = target_range
            source_offset, source_size = source_ranges[0]
            if source_size != target_size:
                return None
            target_storage.copy_range(
                source_uri,
                target_path.as_uri(),
                source_offset,
                target_offset,
                target_size,
            )
            written += target_size
    except NotImplementedError:
        return None
    return written


def _report(
    spec: dict[str, Any],
    plan: dict[str, Any],
    journal: _Journal,
    verification: dict[str, Any],
    started: float,
    *,
    target_rank: int | None = None,
    status: str = "success",
) -> dict[str, Any]:
    selected_nodes = _selected_nodes(plan, target_rank)
    result = {
        "version": "1",
        "plan_id": plan["migration_id"],
        "status": status,
        "meshes": _meshes(spec),
        "tensors": _tensor_report(spec, selected_nodes),
        "chunks": len(selected_nodes),
        "bytes_planned": _selected_bytes(spec, selected_nodes),
        "bytes_moved": journal.bytes_moved,
        "duration_seconds": time.perf_counter() - started,
        "retries": int(journal.data.get("resumes", 0)),
        "verification": verification,
    }
    if target_rank is not None:
        result["target_rank"] = target_rank
    return result




def _meshes(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": spec["source"]["mesh"],
        "target": spec["target"]["mesh"],
    }


def _tensor_report(
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts = {
        tensor["name"]: 0
        for tensor in spec["checkpoint"]["tensors"]
    }
    for node in nodes:
        counts[node["tensor"]] += 1
    return [
        {
            "name": tensor["name"],
            "shape": tensor["shape"],
            "dtype": tensor["dtype"],
            "chunks": counts[tensor["name"]],
        }
        for tensor in spec["checkpoint"]["tensors"]
    ]


def _selected_nodes(
    plan: dict[str, Any],
    target_rank: int | None,
) -> list[dict[str, Any]]:
    if target_rank is None:
        return plan["nodes"]
    return [
        node for node in plan["nodes"]
        if node["target_rank"] == target_rank
    ]


def _selected_bytes(
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> int:
    item_sizes = {
        tensor["name"]: DTYPE_BYTES[tensor["dtype"]]
        for tensor in spec["checkpoint"]["tensors"]
    }
    return sum(
        prod(node["size"])
        * item_sizes[node["tensor"]]
        * len(node["inputs"])
        for node in nodes
    )


def _validate_report(report: dict[str, Any]) -> None:
    schema = json.loads(REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema).iter_errors(report),
        key=lambda error: list(error.path),
    )
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ExecutionError(f"report schema validation failed: {details}")
