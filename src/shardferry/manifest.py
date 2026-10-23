from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .planning.mesh import parse_mesh, validate_placement_dims
from .resources import resource_path

SCHEMA_PATH = resource_path("migration-spec.schema.json")


class ManifestError(ValueError):
    """Raised when a MigrationSpec is invalid."""


def load_spec(path: str | Path) -> dict[str, Any]:
    spec = load_payload(path)
    validate_spec(spec)
    return spec


def load_payload(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise ManifestError(f"file not found: {source}")
    try:
        text = source.read_text(encoding="utf-8")
        if source.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(text)
        else:
            payload = json.loads(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ManifestError(f"could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest must be an object")
    return payload


def validate_spec(spec: dict[str, Any]) -> None:
    if not isinstance(spec, dict):
        raise ManifestError("manifest must be an object")
    if not SCHEMA_PATH.is_file():
        raise ManifestError(f"schema not found: {SCHEMA_PATH}")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(spec), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ManifestError(f"schema validation failed: {details}")
    _validate_semantics(spec)


def _validate_semantics(spec: dict[str, Any]) -> None:
    tensor_list = spec["checkpoint"]["tensors"]
    if not tensor_list:
        raise ManifestError("checkpoint must contain at least one tensor")
    tensors: dict[str, dict[str, Any]] = {}
    for tensor in tensor_list:
        name = tensor["name"]
        if name in tensors:
            raise ManifestError(f"duplicate tensor name: {name}")
        tensors[name] = tensor

    meshes = {}
    for role in ("source", "target"):
        endpoint = spec[role]
        try:
            mesh = parse_mesh(endpoint["mesh"])
        except ValueError as exc:
            raise ManifestError(f"{role}.mesh: {exc}") from exc
        meshes[role] = mesh
        _validate_rank_map(role, mesh, endpoint["mesh"])
        for name, tensor in tensors.items():
            try:
                validate_placement_dims(mesh, len(tensor["shape"]))
            except ValueError as exc:
                raise ManifestError(f"{role}.mesh for tensor {name}: {exc}") from exc

    source_shards = spec["source"].get("shards") or []
    if not source_shards:
        raise ManifestError("source.shards must not be empty")
    _validate_shards("source", source_shards, tensors, meshes["source"])

    target_shards = spec["target"].get("shards") or []
    if target_shards:
        _validate_shards("target", target_shards, tensors, meshes["target"])


def _validate_rank_map(
    role: str,
    mesh: Any,
    mesh_payload: dict[str, Any],
) -> None:
    rank_map = mesh_payload.get("rank_map") or []
    seen_ranks: set[int] = set()
    for entry in rank_map:
        rank = entry["rank"]
        if rank >= mesh.world_size:
            raise ManifestError(f"{role}.mesh.rank_map rank {rank} is out of range")
        if rank in seen_ranks:
            raise ManifestError(f"{role}.mesh.rank_map contains duplicate rank {rank}")
        seen_ranks.add(rank)


def _validate_shards(
    role: str,
    shards: list[dict[str, Any]],
    tensors: dict[str, dict[str, Any]],
    mesh: Any,
) -> None:
    for shard in shards:
        tensor = tensors.get(shard["tensor"])
        if tensor is None:
            raise ManifestError(
                f"{role} shard references unknown tensor: {shard['tensor']}"
            )
        offset = tuple(shard["offset"])
        size = tuple(shard["size"])
        shape = tuple(tensor["shape"])
        if shard["rank"] >= mesh.world_size:
            raise ManifestError(f"{role} rank {shard['rank']} is out of range")
        if len(offset) != len(shape) or len(size) != len(shape):
            raise ManifestError(
                f"{role} shard for {shard['tensor']} has rank mismatch"
            )
        for dim, (start, length, limit) in enumerate(zip(offset, size, shape)):
            if start + length > limit:
                raise ManifestError(
                    f"{role} shard for {shard['tensor']} exceeds shape on dim {dim}"
                )
