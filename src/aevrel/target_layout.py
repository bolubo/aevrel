from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import quote

from .cdp import (
    ChunkRef,
    ChunkStore,
    ChunkingPolicy,
    VersionDescriptor,
    VersionManifest,
    VersionTarget,
    build_manifest_from_paths,
)
from .cas_executor import apply_rank_to_staging
from .executor import apply
from .manifest import validate_spec
from .planning.mesh import parse_mesh
from .native import build_manifest_native
from .policy import AdaptiveChunkingPolicy
from .targets import target_file_path


class TargetLayoutError(RuntimeError):
    """Raised when a target-layout provider cannot materialize a version."""


class ApplyFileProvider:
    """Adapt the existing planner/executor into a streaming CDP provider.

    The factory must return a validated ``(spec, plan)`` for the requested
    version. The provider writes the plan into a temporary target root, then
    builds the manifest directly from those files without loading the whole
    checkpoint into memory.
    """

    def __init__(
        self,
        factory: Callable[[VersionDescriptor], tuple[dict, dict]],
        *,
        work_parent: str | Path | None = None,
        source_token: str | None = None,
    ) -> None:
        self.factory = factory
        self.source_token = source_token
        self.work_parent = (
            Path(work_parent).resolve() if work_parent is not None else None
        )

    def __call__(
        self,
        descriptor: VersionDescriptor,
        chunk_store: ChunkStore,
        chunking: ChunkingPolicy,
    ) -> VersionManifest:
        spec, plan_payload = self.factory(descriptor)
        validate_spec(spec)
        parent = self.work_parent
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        try:
            with TemporaryDirectory(prefix="aevrel-layout-", dir=parent) as tmp:
                root = Path(tmp)
                apply(
                    plan_payload,
                    spec,
                    target_root=root,
                    token=self.source_token,
                )
                files: dict[str, Path] = {}
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(root)
                    if any(part.startswith(".aevrel") for part in relative.parts):
                        continue
                    files[relative.as_posix()] = path
                return build_manifest_from_paths(
                    version_id=descriptor.version_id,
                    parent_id=descriptor.marker.parent_id,
                    metadata_hash=descriptor.marker.metadata_hash,
                    created_at=descriptor.marker.created_at,
                    files=files,
                    chunk_store=chunk_store,
                    chunking=chunking,
                )
        except Exception as exc:
            raise TargetLayoutError(
                f"could not materialize target layout for {descriptor.version_id}: {exc}"
            ) from exc


class IncrementalApplyFileProvider:
    """Apply only changed target ranks, reusing previous target files.

    This is still an adapter over the existing executor. Production can replace
    it with a direct target-agent delta executor, but it gives the CDP layer a
    real incremental planner/executor path for benchmarks and integration.
    """

    def __init__(
        self,
        factory: Callable[[VersionDescriptor], tuple[dict, dict]],
        *,
        cache_root: str | Path,
        source_token: str | None = None,
    ) -> None:
        self.factory = factory
        self.source_token = source_token
        self.cache_root = Path(cache_root).resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._plans: dict[str, dict] = {}
        self._roots: dict[str, Path] = {}

    def __call__(
        self,
        descriptor: VersionDescriptor,
        chunk_store: ChunkStore,
        chunking: ChunkingPolicy,
    ) -> VersionManifest:
        spec, plan_payload = self.factory(descriptor)
        validate_spec(spec)
        version_root = self.cache_root / quote(descriptor.version_id, safe="")
        version_root.mkdir(parents=True, exist_ok=True)
        previous_id = descriptor.marker.parent_id
        previous_plan = self._plans.get(previous_id)
        previous_root = self._roots.get(previous_id)

        if previous_plan is None or previous_root is None:
            apply(
                plan_payload,
                spec,
                target_root=version_root,
                token=self.source_token,
            )
        else:
            current_signatures = _target_signatures(plan_payload)
            previous_signatures = _target_signatures(previous_plan)
            target_world = parse_mesh(spec["target"]["mesh"]).world_size
            for rank in range(target_world):
                changed = (
                    current_signatures.get(rank, ())
                    != previous_signatures.get(rank, ())
                )
                if changed or not _reuse_rank(
                    spec,
                    previous_root,
                    version_root,
                    rank,
                ):
                    self._apply_rank(plan_payload, spec, version_root, rank)

        files: dict[str, Path] = {}
        for path in sorted(version_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(version_root)
            if any(part.startswith(".aevrel") for part in relative.parts):
                continue
            files[relative.as_posix()] = path
        manifest = build_manifest_from_paths(
            version_id=descriptor.version_id,
            parent_id=descriptor.marker.parent_id,
            metadata_hash=descriptor.marker.metadata_hash,
            created_at=descriptor.marker.created_at,
            files=files,
            chunk_store=chunk_store,
            chunking=chunking,
        )
        self._plans[descriptor.version_id] = plan_payload
        self._roots[descriptor.version_id] = version_root
        return manifest

    def _apply_rank(
        self,
        plan_payload: dict,
        spec: dict,
        version_root: Path,
        rank: int,
    ) -> None:
        with TemporaryDirectory(prefix=".cdp-rank-", dir=self.cache_root) as tmp:
            rank_root = Path(tmp)
            apply(
                plan_payload,
                spec,
                target_rank=rank,
                target_root=rank_root,
                token=self.source_token,
            )
            for tensor in spec["checkpoint"]["tensors"]:
                source = target_file_path(
                    spec,
                    tensor["name"],
                    rank,
                    root=rank_root,
                )
                destination = target_file_path(
                    spec,
                    tensor["name"],
                    rank,
                    root=version_root,
                )
                if not source.is_file():
                    raise TargetLayoutError(
                        f"executor did not produce target file: {source}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                _link_or_copy(source, destination, overwrite=True)


class DirectIncrementalApplyFileProvider:
    """Incremental executor provider that uses target shadow as its base.

    Unlike the cache-backed adapter, this keeps no persistent provider cache.
    Only changed target ranks are written to a short-lived temporary root; all
    unchanged files come directly from the previous target shadow.
    """

    def __init__(
        self,
        factory: Callable[[VersionDescriptor], tuple[dict, dict]],
        *,
        target: VersionTarget,
        source_token: str | None = None,
        adaptive: AdaptiveChunkingPolicy | None = None,
        use_cas_staging: bool = True,
    ) -> None:
        self.factory = factory
        self.source_token = source_token
        self.target = target
        self.adaptive = adaptive
        self.use_cas_staging = use_cas_staging
        self._selected_chunking: ChunkingPolicy | None = None
        self.target.root.mkdir(parents=True, exist_ok=True)
        self._plans: dict[str, dict] = {}

    def __call__(
        self,
        descriptor: VersionDescriptor,
        chunk_store: ChunkStore,
        chunking: ChunkingPolicy,
    ) -> VersionManifest:
        spec, plan_payload = self.factory(descriptor)
        validate_spec(spec)
        previous_id = descriptor.marker.parent_id
        previous_plan = self._plans.get(previous_id)
        previous_root = (
            self.target._shadow_path(previous_id)
            if previous_id is not None
            else None
        )
        if (
            previous_plan is None
            or previous_root is None
            or not previous_root.is_dir()
        ):
            with TemporaryDirectory(
                prefix=".cdp-full-",
                dir=self.target.root,
            ) as tmp:
                temp_root = Path(tmp)
                target_world = parse_mesh(spec["target"]["mesh"]).world_size
                per_rank_bytes = (
                    int(plan_payload.get("total_bytes", 0)) // target_world
                    if target_world
                    else 0
                )
                files: dict[str, Path] = {}

                def apply_rank(rank: int) -> dict[str, Path]:
                    rank_root = temp_root / f"rank-{rank}"
                    apply(
                        plan_payload,
                        spec,
                        target_rank=rank,
                        target_root=rank_root,
                        token=self.source_token,
                    )
                    return _rank_files(spec, rank_root, rank)

                # Small target ranks are faster in one executor call. Large
                # ranks bypass apply()'s target tree and go straight to CAS
                # staging; parallel workers are only used very large ranks.
                if self.use_cas_staging and per_rank_bytes >= 128 * 1024 * 1024:
                    for rank in range(target_world):
                        files.update(
                            apply_rank_to_staging(
                                spec,
                                plan_payload,
                                rank,
                                temp_root / f"rank-{rank}",
                                source_token=self.source_token,
                            )
                        )
                elif per_rank_bytes >= 512 * 1024 * 1024:
                    workers = min(target_world, 4)
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        futures = [
                            executor.submit(apply_rank, rank)
                            for rank in range(target_world)
                        ]
                        for future in as_completed(futures):
                            files.update(future.result())
                else:
                    full_root = temp_root / "full"
                    apply(
                        plan_payload,
                        spec,
                        target_root=full_root,
                        token=self.source_token,
                    )
                    files.update(_files_from_root(full_root))
                if self.adaptive is not None:
                    if self._selected_chunking is None:
                        self._selected_chunking = self.adaptive.choose(
                            total_bytes=int(plan_payload.get("total_bytes", 0)),
                            target_ranks=target_world,
                            changed_ranks=target_world,
                        )
                    manifest_chunking = self._selected_chunking
                else:
                    manifest_chunking = chunking
                manifest = self._build_manifest(
                    version_id=descriptor.version_id,
                    parent_id=descriptor.marker.parent_id,
                    metadata_hash=descriptor.marker.metadata_hash,
                    created_at=descriptor.marker.created_at,
                    files=files,
                    chunk_store=chunk_store,
                    chunking=manifest_chunking,
                )
            self._plans[descriptor.version_id] = plan_payload
            return manifest

        current_signatures = _target_signatures(plan_payload)
        previous_signatures = _target_signatures(previous_plan)
        target_world = parse_mesh(spec["target"]["mesh"]).world_size
        changed_rank_set = {
            rank
            for rank in range(target_world)
            if current_signatures.get(rank, ()) != previous_signatures.get(rank, ())
        }
        if self.adaptive is not None:
            if self._selected_chunking is None:
                self._selected_chunking = self.adaptive.choose(
                    total_bytes=int(plan_payload.get("total_bytes", 0)),
                    target_ranks=target_world,
                    changed_ranks=len(changed_rank_set),
                )
            manifest_chunking = self._selected_chunking
        else:
            manifest_chunking = chunking
        with TemporaryDirectory(
            prefix=".cdp-delta-",
            dir=self.target.root,
        ) as tmp:
            temp_root = Path(tmp)
            files: dict[str, Path] = {}
            for rank in range(target_world):
                changed = (
                    current_signatures.get(rank, ())
                    != previous_signatures.get(rank, ())
                )
                if not changed:
                    reused = _rank_files(spec, previous_root, rank)
                    if reused:
                        files.update(reused)
                        continue
                rank_root = temp_root / f"rank-{rank}"
                apply(
                    plan_payload,
                    spec,
                    target_rank=rank,
                    target_root=rank_root,
                    token=self.source_token,
                )
                files.update(_rank_files(spec, rank_root, rank))
            if self.adaptive is None:
                manifest = self._build_manifest(
                    version_id=descriptor.version_id,
                    parent_id=descriptor.marker.parent_id,
                    metadata_hash=descriptor.marker.metadata_hash,
                    created_at=descriptor.marker.created_at,
                    files=files,
                    chunk_store=chunk_store,
                    chunking=manifest_chunking,
                )
            else:
                previous_manifest = self.target.manifests.read(previous_id)
                previous_by_file: dict[str, list[ChunkRef]] = {}
                for chunk in previous_manifest.chunks:
                    previous_by_file.setdefault(chunk.file, []).append(chunk)
                changed_files = {
                    target_file_path(
                        spec,
                        tensor["name"],
                        rank,
                        root=Path("/"),
                    ).relative_to("/").as_posix()
                    for rank in changed_rank_set
                    for tensor in spec["checkpoint"]["tensors"]
                }
                reuse = {
                    file_name: tuple(previous_by_file[file_name])
                    for file_name in files
                    if file_name not in changed_files
                    and file_name in previous_by_file
                }
                manifest = build_manifest_from_paths(
                    version_id=descriptor.version_id,
                    parent_id=descriptor.marker.parent_id,
                    metadata_hash=descriptor.marker.metadata_hash,
                    created_at=descriptor.marker.created_at,
                    files=files,
                    chunk_store=chunk_store,
                    chunking=manifest_chunking,
                    hardlink=True,
                    reuse_chunks=reuse,
                    chunking_for_file=lambda _name, _path: manifest_chunking,
                )
        self._plans[descriptor.version_id] = plan_payload
        return manifest

    def _build_manifest(self, **kwargs) -> VersionManifest:
        """Ingest the target layout through the native chunker.

        Rust (aevrel-fast) is the single chunking implementation. There is no
        Python fallback: an unavailable binary or a failed ingest is a hard
        error so a failure can never masquerade as a successful manifest.
        """
        return build_manifest_native(
            version_id=kwargs["version_id"],
            parent_id=kwargs["parent_id"],
            metadata_hash=kwargs["metadata_hash"],
            created_at=kwargs["created_at"],
            files=kwargs["files"],
            chunk_store=kwargs["chunk_store"],
            chunking=kwargs["chunking"],
            hardlink=True,
        )


def _files_from_root(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".aevrel") for part in relative.parts):
            continue
        files[relative.as_posix()] = path
    return files


def _rank_files(spec: dict, root: Path, rank: int) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for tensor in spec["checkpoint"]["tensors"]:
        path = target_file_path(spec, tensor["name"], rank, root=root)
        if not path.is_file():
            return {}
        files[path.relative_to(root).as_posix()] = path
    return files


def _target_signatures(plan_payload: dict) -> dict[int, tuple]:
    signatures: dict[int, list] = {}
    for node in plan_payload.get("nodes", []):
        inputs = tuple(
            sorted(
                (
                    int(item["source_rank"]),
                    tuple(int(value) for value in item["source_offset"]),
                    str(item["checksum"]),
                )
                for item in node.get("inputs", [])
            )
        )
        signature = (
            str(node["tensor"]),
            tuple(int(value) for value in node["target_offset"]),
            tuple(int(value) for value in node["size"]),
            str(node["op"]),
            inputs,
        )
        signatures.setdefault(int(node["target_rank"]), []).append(signature)
    return {
        rank: tuple(sorted(values))
        for rank, values in signatures.items()
    }


def _reuse_rank(
    spec: dict,
    previous_root: Path,
    version_root: Path,
    rank: int,
) -> bool:
    pairs: list[tuple[Path, Path]] = []
    for tensor in spec["checkpoint"]["tensors"]:
        source = target_file_path(spec, tensor["name"], rank, root=previous_root)
        destination = target_file_path(spec, tensor["name"], rank, root=version_root)
        if not source.is_file():
            return False
        pairs.append((source, destination))
    for source, destination in pairs:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy(source, destination)
    return True


def _link_or_copy(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> None:
    if destination.exists():
        if not overwrite:
            return
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)
