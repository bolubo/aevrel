from __future__ import annotations

from pathlib import Path

from .checkpoint import inventory_dcp_descriptors
from ..manifest import validate_spec
from ..planning import plan


class DcpRangeSpecError(RuntimeError):
    """Raised when a DCP range spec cannot be assembled."""


class DcpRangeSpecBuilder:
    """Build a source spec whose shards are source-agent HTTP range URLs.

    This intentionally uses ``inventory_dcp_descriptors`` instead of
    ``inspect_dcp``: the latter materializes raw shards into a work directory.
    """

    def __init__(
        self,
        *,
        source_world: int,
        source_mesh_shape: tuple[int, ...],
        source_placements: tuple[str, ...],
        target_mesh_shape: tuple[int, ...],
        target_placements: tuple[str, ...],
        source_public_url: str,
        target_storage_uri: str = "file:///target",
        metadata_dir: str | Path | None = None,
    ) -> None:
        if source_world < 1:
            raise DcpRangeSpecError("source_world must be positive")
        if len(source_mesh_shape) != len(source_placements):
            raise DcpRangeSpecError("source mesh shape/placements mismatch")
        if _product(source_mesh_shape) != source_world:
            raise DcpRangeSpecError(
                "source mesh product does not match source_world"
            )
        if len(target_mesh_shape) != len(target_placements):
            raise DcpRangeSpecError("target mesh shape/placements mismatch")
        if not source_public_url:
            raise DcpRangeSpecError("source_public_url must not be empty")
        self.source_world = source_world
        self.source_mesh_shape = source_mesh_shape
        self.source_placements = source_placements
        self.target_mesh_shape = target_mesh_shape
        self.target_placements = target_placements
        self.source_public_url = source_public_url.rstrip("/")
        self.target_storage_uri = target_storage_uri
        self.metadata_dir = metadata_dir

    def build(self, checkpoint: str | Path) -> dict:
        checkpoint_path = Path(checkpoint).resolve()
        if not checkpoint_path.is_dir():
            raise DcpRangeSpecError(
                f"DCP checkpoint not found: {checkpoint_path}"
            )
        tensors: dict[str, dict] = {}
        shards: list[dict] = []
        metadata_hash = None
        for rank in range(self.source_world):
            inventory, _refs = inventory_dcp_descriptors(
                checkpoint_path,
                rank,
                metadata_dir=self.metadata_dir,
                file_prefix=self.source_public_url + "/v1/shards",
            )
            if metadata_hash is None:
                metadata_hash = inventory["metadata_hash"]
            elif metadata_hash != inventory["metadata_hash"]:
                raise DcpRangeSpecError("DCP metadata hash differs across ranks")
            for tensor in inventory["tensors"]:
                tensors.setdefault(tensor["name"], tensor)
            for chunk in inventory["chunks"]:
                shards.append(
                    {
                        "tensor": chunk["tensor"],
                        "rank": chunk["rank"],
                        "uri": chunk["file"],
                        "offset": chunk["tensor_offset"],
                        "size": chunk["tensor_size"],
                        "checksum": chunk["checksum"],
                    }
                )
        if not shards:
            raise DcpRangeSpecError(
                f"DCP checkpoint has no source shards: {checkpoint_path}"
            )
        source_world = self.source_world
        target_world = _product(self.target_mesh_shape)
        spec = {
            "version": "1",
            "checkpoint": {
                "format": "dcp",
                "tensors": list(tensors.values()),
            },
            "source": {
                "mesh": {
                    "shape": list(self.source_mesh_shape),
                    "world_size": source_world,
                    "placements": list(self.source_placements),
                },
                "storage": {"uri": checkpoint_path.as_uri()},
                "shards": sorted(
                    shards,
                    key=lambda row: (
                        row["tensor"],
                        row["rank"],
                        tuple(row["offset"]),
                    ),
                ),
            },
            "target": {
                "mesh": {
                    "shape": list(self.target_mesh_shape),
                    "world_size": target_world,
                    "placements": list(self.target_placements),
                },
                "storage": {"uri": self.target_storage_uri},
            },
        }
        validate_spec(spec)
        return spec

    def build_and_plan(self, checkpoint: str | Path) -> tuple[dict, dict]:
        spec = self.build(checkpoint)
        return spec, plan(spec)


def _product(values: tuple[int, ...]) -> int:
    result = 1
    for value in values:
        result *= value
    return result
