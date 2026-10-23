from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from .cdp import (
    ChunkStore,
    ChunkingPolicy,
    VersionDescriptor,
    VersionManifest,
    build_manifest_from_paths,
)
from .executor import _apply_node, _target_files
from .identity import source_maps
from .manifest import validate_spec
from .planning.mesh import parse_mesh
from .planning import validate_plan
from .storage.file import FileStorage
from .storage.router import StorageRouter
from .planning.tensor_io import verify_source_checksums


class CasExecutorError(RuntimeError):
    """Raised when a target rank cannot be written through the CAS path."""


def apply_rank_to_staging(
    spec: dict,
    plan: dict,
    target_rank: int,
    staging_root: str | Path,
    *,
    source_token: str | None = None,
) -> dict[str, Path]:
    """Write one target rank's plan directly into a staging root.

    This bypasses ``executor.apply``'s target tree and journal. The caller
    hardlinks the resulting files into CAS and removes the staging root.
    """
    validate_spec(spec)
    validate_plan(plan, spec)
    root = Path(staging_root).resolve()
    target_mesh = parse_mesh(spec["target"]["mesh"])
    if target_rank < 0 or target_rank >= target_mesh.world_size:
        raise CasExecutorError(f"target rank {target_rank} is out of range")

    source_storage = StorageRouter(token=source_token)
    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    target_storage = FileStorage()
    targets = _target_files(
        spec,
        plan["migration_id"],
        target_rank=target_rank,
        target_root=root,
    )
    target_map = {(target.tensor, target.rank): target for target in targets}
    for target in targets:
        target.final_path.parent.mkdir(parents=True, exist_ok=True)
        target_storage.ensure_size(target.final_path.as_uri(), target.size_bytes)

    nodes = [
        node for node in plan["nodes"]
        if int(node["target_rank"]) == target_rank
    ]
    verify_source_checksums(
        spec,
        plan,
        source_storage,
        target_rank=target_rank,
    )
    tensors = {
        tensor["name"]: tensor
        for tensor in spec["checkpoint"]["tensors"]
    }
    for node in nodes:
        target = target_map[(node["tensor"], target_rank)]
        _apply_node(
            source_storage,
            target_storage,
            target.final_path,
            source_by_id,
            source_by_rank,
            tensors[node["tensor"]],
            node,
            target,
        )
    for target in targets:
        target_storage.fsync(target.final_path.as_uri())
    return {
        target.final_path.relative_to(root).as_posix(): target.final_path
        for target in targets
    }


class CasStagingProvider:
    """Full-version CAS provider using the direct staging executor."""

    def __init__(
        self,
        factory: Callable[[VersionDescriptor], tuple[dict, dict]],
        *,
        target_root: str | Path,
        source_token: str | None = None,
    ) -> None:
        self.factory = factory
        self.target_root = Path(target_root).resolve()
        self.target_root.mkdir(parents=True, exist_ok=True)
        self.source_token = source_token

    def __call__(
        self,
        descriptor: VersionDescriptor,
        chunk_store: ChunkStore,
        chunking: ChunkingPolicy,
    ) -> VersionManifest:
        spec, plan = self.factory(descriptor)
        with TemporaryDirectory(prefix=".cas-full-", dir=self.target_root) as tmp:
            root = Path(tmp)
            files: dict[str, Path] = {}
            world = parse_mesh(spec["target"]["mesh"]).world_size
            for rank in range(world):
                files.update(
                    apply_rank_to_staging(
                        spec,
                        plan,
                        rank,
                        root,
                        source_token=self.source_token,
                    )
                )
            return build_manifest_from_paths(
                version_id=descriptor.version_id,
                parent_id=descriptor.marker.parent_id,
                metadata_hash=descriptor.marker.metadata_hash,
                created_at=descriptor.marker.created_at,
                files=files,
                chunk_store=chunk_store,
                chunking=chunking,
                hardlink=True,
            )
