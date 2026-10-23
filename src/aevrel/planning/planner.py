from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from math import prod
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..identity import resolve_source, source_id_for_shard, source_maps
from .layout import DTYPE_BYTES, Region, contains, local_region, rank_coord
from ..manifest import validate_spec
from .mesh import Mesh, parse_mesh
from ..resources import resource_path
from .tensor_io import byte_runs, coalesce_runs


PLAN_SCHEMA_PATH = resource_path("plan.schema.json")
PLAN_POLICY = "aevrel-plan-v2"


class PlannerError(ValueError):
    """Raised when a transfer plan cannot be built."""


@dataclass(frozen=True)
class SourceShard:
    index: int
    id: str
    tensor: str
    rank: int
    region: Region
    checksum: str
    coord: tuple[int, ...]


class _SourceIndex:
    """Exact axis-filtered index for rectangular source regions.

    For each axis, sources are sorted by their start coordinate. A target
    query intersects the prefix that starts before the target end, then filters
    by the source end. Intersecting axis candidates avoids the O(sources)
    scan for every target shard while retaining exact final overlap checks.
    """

    def __init__(self, sources: list[SourceShard]) -> None:
        self.sources = sources
        self.start_values: list[list[int]] = []
        self.start_entries: list[list[tuple[int, int]]] = []
        self.max_end_trees: list[list[int]] = []
        self.tree_sizes: list[int] = []
        if not sources:
            return
        dimensions = len(sources[0].region.offset)
        for axis in range(dimensions):
            entries = sorted(
                (source.region.offset[axis], index)
                for index, source in enumerate(sources)
            )
            self.start_entries.append(entries)
            self.start_values.append([entry[0] for entry in entries])
            ends = [
                sources[index].region.offset[axis]
                + sources[index].region.size[axis]
                for _, index in entries
            ]
            size = 1
            while size < len(ends):
                size *= 2
            tree = [-1] * (size * 2)
            tree[size : size + len(ends)] = ends
            for index in range(size - 1, 0, -1):
                tree[index] = max(tree[index * 2], tree[index * 2 + 1])
            self.max_end_trees.append(tree)
            self.tree_sizes.append(size)

    def query(self, target: Region) -> list[SourceShard]:
        if not self.sources:
            return []
        prefixes = [
            bisect_left(
                self.start_values[axis],
                target.offset[axis] + target.size[axis],
            )
            for axis in range(len(target.offset))
        ]
        base_axis = min(range(len(prefixes)), key=prefixes.__getitem__)
        if prefixes[base_axis] == 0:
            return []
        candidates: set[int] = set()
        self._collect_candidates(
            base_axis,
            prefixes[base_axis],
            target.offset[base_axis],
            1,
            0,
            self.tree_sizes[base_axis],
            candidates,
        )
        return [
            self.sources[index]
            for index in sorted(candidates)
            if all(
                source_start < target_start + target_length
                and target_start < source_start + source_length
                for source_start, source_length, target_start, target_length in zip(
                    self.sources[index].region.offset,
                    self.sources[index].region.size,
                    target.offset,
                    target.size,
                )
            )
        ]

    def _collect_candidates(
        self,
        axis: int,
        limit: int,
        threshold: int,
        node: int,
        left: int,
        right: int,
        result: set[int],
    ) -> None:
        tree = self.max_end_trees[axis]
        if left >= limit or tree[node] <= threshold:
            return
        if right - left == 1:
            if left < len(self.start_entries[axis]):
                result.add(self.start_entries[axis][left][1])
            return
        middle = (left + right) // 2
        self._collect_candidates(
            axis,
            limit,
            threshold,
            node * 2,
            left,
            middle,
            result,
        )
        self._collect_candidates(
            axis,
            limit,
            threshold,
            node * 2 + 1,
            middle,
            right,
            result,
        )


def plan(spec: dict[str, Any]) -> dict[str, Any]:
    validate_spec(spec)
    source_mesh = parse_mesh(spec["source"]["mesh"])
    target_mesh = parse_mesh(spec["target"]["mesh"])
    target_partial_axes, target_reduce_op = _partial_layout(target_mesh)
    if target_partial_axes and target_reduce_op not in {"sum", "avg"}:
        raise PlannerError(
            f"target Partial({target_reduce_op}) is not supported"
        )

    source_shards = _source_catalog(spec, source_mesh)
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    source_indexes = {
        tensor_name: _SourceIndex(
            [shard for shard in source_shards if shard.tensor == tensor_name]
        )
        for tensor_name in tensors
    }
    partial_axes, reduce_op = _partial_layout(source_mesh)
    nodes: list[dict[str, Any]] = []
    total_bytes = 0

    for tensor_name, tensor in tensors.items():
        shape = tuple(tensor["shape"])
        item_size = DTYPE_BYTES[tensor["dtype"]]
        tensor_index = source_indexes[tensor_name]
        for target_rank in range(target_mesh.world_size):
            target_region = local_region(
                target_mesh,
                target_mesh.placements,
                shape,
                target_rank,
                allow_partial=True,
            )
            if target_region.element_count == 0:
                continue
            if (
                target_partial_axes
                and target_reduce_op == "sum"
                and not _is_partial_owner(
                    target_rank, target_mesh, target_partial_axes
                )
            ):
                nodes.append(
                    {
                        "id": f"n{len(nodes):06d}",
                        "tensor": tensor_name,
                        "target_rank": target_rank,
                        "op": "zeros",
                        "target_offset": list(target_region.offset),
                        "size": list(target_region.size),
                        "inputs": [],
                    }
                )
                continue
            for op, source_inputs, target_cell in _plan_region(
                tensor_name,
                target_rank,
                target_region,
                tensor_index,
                source_mesh,
                partial_axes,
                reduce_op,
                item_size,
            ):
                inputs = [
                    {
                        "source_rank": source.rank,
                        "source_id": source.id,
                        "source_offset": list(target_cell.offset),
                        "checksum": source.checksum,
                    }
                    for source in source_inputs
                ]
                nodes.append(
                    {
                        "id": f"n{len(nodes):06d}",
                        "tensor": tensor_name,
                        "target_rank": target_rank,
                        "op": op,
                        "target_offset": list(target_cell.offset),
                        "size": list(target_cell.size),
                        "inputs": inputs,
                    }
                )
                total_bytes += (
                    target_cell.element_count * item_size * len(source_inputs)
                )

    nodes = _coalesce_nodes(nodes)
    theoretical_min_bytes = _theoretical_min_bytes(spec)
    result = {
        "version": "1",
        "migration_id": _migration_id(spec),
        "strategy": "direct",
        "full_gather": False,
        "total_bytes": total_bytes,
        "theoretical_min_bytes": theoretical_min_bytes,
        "cost": _plan_cost(spec, nodes, total_bytes),
        "nodes": nodes,
    }
    validate_plan(
        result,
        spec,
        _spec_validated=True,
        _schema_validated=True,
    )
    return result


def validate_plan(
    plan_payload: dict[str, Any],
    spec: dict[str, Any],
    *,
    _spec_validated: bool = False,
    _schema_validated: bool = False,
) -> None:
    if not _spec_validated:
        validate_spec(spec)
    if not _schema_validated:
        _validate_schema(plan_payload)
    if plan_payload["migration_id"] != _migration_id(spec):
        raise PlannerError("migration_id does not match the spec")

    source_mesh = parse_mesh(spec["source"]["mesh"])
    target_mesh = parse_mesh(spec["target"]["mesh"])
    target_partial_axes, target_reduce_op = _partial_layout(target_mesh)
    if target_partial_axes and target_reduce_op not in {"sum", "avg"}:
        raise PlannerError(
            f"target Partial({target_reduce_op}) is not supported"
        )

    source_shards = _source_catalog(spec, source_mesh)
    source_by_id = {source.id: source for source in source_shards}
    source_by_rank: dict[tuple[str, int], list[SourceShard]] = {}
    for source in source_shards:
        source_by_rank.setdefault(
            (source.tensor, source.rank),
            [],
        ).append(source)
    partial_axes, reduce_op = _partial_layout(source_mesh)
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    item_sizes = {
        name: DTYPE_BYTES[tensor["dtype"]] for name, tensor in tensors.items()
    }
    grouped: dict[tuple[str, int], list[Region]] = {}
    computed_bytes = 0

    for node in plan_payload["nodes"]:
        tensor_name = node["tensor"]
        if tensor_name not in tensors:
            raise PlannerError(f"plan references unknown tensor: {tensor_name}")
        tensor = tensors[tensor_name]
        shape = tuple(tensor["shape"])
        target_rank = node["target_rank"]
        if target_rank >= target_mesh.world_size:
            raise PlannerError(f"target rank {target_rank} is out of range")
        target_offset = tuple(node["target_offset"])
        size = tuple(node["size"])
        if len(target_offset) != len(shape) or len(size) != len(shape):
            raise PlannerError(f"{node['id']}: target rank mismatch")
        target_region = Region(target_offset, size)
        expected_target = local_region(
            target_mesh,
            target_mesh.placements,
            shape,
            target_rank,
            allow_partial=True,
        )
        if expected_target.element_count and not contains(
            expected_target, target_offset, size
        ):
            raise PlannerError(f"{node['id']}: target range is outside target shard")
        if target_partial_axes and target_reduce_op == "sum":
            owner = _is_partial_owner(
                target_rank, target_mesh, target_partial_axes
            )
            if owner == (node["op"] == "zeros"):
                raise PlannerError(
                    f"{node['id']}: target Partial ownership mismatch"
                )
        elif node["op"] == "zeros":
            raise PlannerError(f"{node['id']}: zeros requires target Partial(sum)")

        if node["op"] == "zeros":
            if node["inputs"]:
                raise PlannerError(f"{node['id']}: zeros node must have no inputs")
            if not target_partial_axes:
                raise PlannerError(f"{node['id']}: zeros requires target Partial")
        elif not partial_axes:
            if node["op"] != "copy" or len(node["inputs"]) != 1:
                raise PlannerError(
                    f"{node['id']}: non-partial source requires one copy input"
                )
        else:
            expected_inputs = prod(
                source_mesh.shape[axis] for axis in partial_axes
            )
            if node["op"] != reduce_op or len(node["inputs"]) != expected_inputs:
                raise PlannerError(
                    f"{node['id']}: Partial node has wrong op or input count"
                )
        input_ranks = [source_input["source_rank"] for source_input in node["inputs"]]
        if len(input_ranks) != len(set(input_ranks)):
            raise PlannerError(f"{node['id']}: duplicate source input")
        for source_input in node["inputs"]:
            source_rank = source_input["source_rank"]
            if source_rank >= source_mesh.world_size:
                raise PlannerError(f"{node['id']}: source rank is out of range")
            source_offset = tuple(source_input["source_offset"])
            if len(source_offset) != len(shape):
                raise PlannerError(f"{node['id']}: source rank mismatch")
            requested_id = source_input.get("source_id")
            if requested_id:
                source = source_by_id.get(requested_id)
                source_candidates = (
                    [source]
                    if source is not None
                    and source.tensor == tensor_name
                    and contains(source.region, source_offset, size)
                    else []
                )
            else:
                source_candidates = [
                    source
                    for source in source_by_rank.get(
                        (tensor_name, source_rank),
                        [],
                    )
                    if contains(source.region, source_offset, size)
                ]
            if not source_candidates:
                raise PlannerError(
                    f"{node['id']}: source range is not present in source shard"
                )
            if not requested_id and len(source_candidates) != 1:
                raise PlannerError(
                    f"{node['id']}: source_id is required when a rank has multiple chunks"
                )
            if source_input["checksum"] != source_candidates[0].checksum:
                raise PlannerError(f"{node['id']}: source checksum does not match")

        if partial_axes and node["op"] != "zeros":
            selected = []
            for source_input in node["inputs"]:
                identifier = source_input.get("source_id")
                if identifier:
                    selected.append(source_by_id[identifier])
                else:
                    candidates = source_by_rank.get(
                        (tensor_name, source_input["source_rank"]),
                        [],
                    )
                    if len(candidates) != 1:
                        raise PlannerError(
                            f"{node['id']}: source_id is required for a "
                            "multi-chunk Partial rank"
                        )
                    selected.append(candidates[0])
            non_partial_axes = tuple(
                axis
                for axis in range(len(source_mesh.shape))
                if axis not in partial_axes
            )
            group_keys = {
                tuple(source.coord[axis] for axis in non_partial_axes)
                for source in selected
            }
            if len(group_keys) != 1:
                raise PlannerError(f"{node['id']}: mixed Partial source groups")
            base_coord = list(selected[0].coord)
            expected_ranks = set()
            for partial_coord in product(
                *(range(source_mesh.shape[axis]) for axis in partial_axes)
            ):
                coord = list(base_coord)
                for axis, value in zip(partial_axes, partial_coord):
                    coord[axis] = value
                expected_ranks.add(mesh_coord_rank(tuple(coord), source_mesh.shape))
            if {source.rank for source in selected} != expected_ranks:
                raise PlannerError(f"{node['id']}: incomplete Partial source group")

        computed_bytes += (
            target_region.element_count
            * item_sizes[tensor_name]
            * len(node["inputs"])
        )
        grouped.setdefault((tensor_name, target_rank), []).append(target_region)

    if computed_bytes != plan_payload["total_bytes"]:
        raise PlannerError("total_bytes does not match node sizes")
    if plan_payload["theoretical_min_bytes"] > computed_bytes:
        raise PlannerError("theoretical_min_bytes exceeds total_bytes")
    _validate_cost(plan_payload, spec)

    for (tensor_name, target_rank), regions in grouped.items():
        shape = tuple(tensors[tensor_name]["shape"])
        expected = local_region(
            target_mesh,
            target_mesh.placements,
            shape,
            target_rank,
            allow_partial=True,
        )
        _check_exact_cover(expected, regions, f"{tensor_name}/rank{target_rank}")

    for tensor_name, tensor in tensors.items():
        shape = tuple(tensor["shape"])
        for target_rank in range(target_mesh.world_size):
            expected = local_region(
                target_mesh,
                target_mesh.placements,
                shape,
                target_rank,
                allow_partial=True,
            )
            if expected.element_count == 0:
                continue
            if (tensor_name, target_rank) not in grouped:
                raise PlannerError(
                    f"{tensor_name}/rank{target_rank}: target shard is not covered"
                )


def _source_catalog(spec: dict[str, Any], mesh: Mesh) -> list[SourceShard]:
    catalog: list[SourceShard] = []
    seen_ids: set[str] = set()
    for index, shard in enumerate(spec["source"].get("shards") or []):
        identifier = source_id_for_shard(shard)
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        catalog.append(
            SourceShard(
                index=index,
                id=identifier,
                tensor=shard["tensor"],
                rank=shard["rank"],
                region=Region(tuple(shard["offset"]), tuple(shard["size"])),
                checksum=shard["checksum"],
                coord=rank_coord(shard["rank"], mesh.shape),
            )
        )
    return catalog


def _theoretical_min_bytes(spec: dict[str, Any]) -> int:
    source_mesh = parse_mesh(spec["source"]["mesh"])
    target_mesh = parse_mesh(spec["target"]["mesh"])
    target_partial_axes, target_reduce_op = _partial_layout(target_mesh)
    source_partial_axes, _ = _partial_layout(source_mesh)
    multiplier = (
        prod(source_mesh.shape[axis] for axis in source_partial_axes)
        if source_partial_axes
        else 1
    )
    total = 0
    for tensor in spec["checkpoint"]["tensors"]:
        shape = tuple(tensor["shape"])
        item_size = DTYPE_BYTES[tensor["dtype"]]
        for target_rank in range(target_mesh.world_size):
            region = local_region(
                target_mesh,
                target_mesh.placements,
                shape,
                target_rank,
                allow_partial=True,
            )
            if region.element_count == 0:
                continue
            if (
                target_partial_axes
                and target_reduce_op == "sum"
                and not _is_partial_owner(
                    target_rank, target_mesh, target_partial_axes
                )
            ):
                continue
            total += region.element_count * item_size * multiplier
    return total


def _plan_cost(
    spec: dict[str, Any],
    nodes: list[dict[str, Any]],
    total_bytes: int,
) -> dict[str, int]:
    fan_ins = [len(node.get("inputs", [])) for node in nodes]
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    range_reads = 0
    local_reads = 0
    remote_reads = 0
    for node in nodes:
        if node["op"] == "zeros":
            continue
        tensor = tensors[node["tensor"]]
        item_size = DTYPE_BYTES[tensor["dtype"]]
        for source_input in node["inputs"]:
            shard = resolve_source(
                source_by_id,
                source_by_rank,
                node["tensor"],
                source_input,
            )
            local_offset = tuple(
                value - base
                for value, base in zip(
                    source_input["source_offset"],
                    shard["offset"],
                )
            )
            runs = byte_runs(
                tuple(shard["size"]),
                local_offset,
                tuple(node["size"]),
                item_size,
            )
            coalesced = coalesce_runs(runs)
            range_reads += len(coalesced)
            if int(shard["rank"]) == int(node["target_rank"]):
                local_reads += len(coalesced)
            else:
                remote_reads += len(coalesced)
    return {
        "bytes": total_bytes,
        "range_reads": range_reads,
        "local_reads": local_reads,
        "remote_reads": remote_reads,
        "max_fan_in": max(fan_ins, default=0),
        "nodes": len(nodes),
    }


def _coalesce_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_key: dict[tuple[Any, ...], list[int]] = {}
    index_keys: dict[int, list[tuple[Any, ...]]] = {}

    def add_index(node: dict[str, Any], index: int) -> None:
        keys = _merge_keys(node)
        index_keys[index] = keys
        for key in keys:
            by_key.setdefault(key, []).append(index)

    def remove_index(index: int) -> None:
        for key in index_keys.pop(index, []):
            bucket = by_key.get(key)
            if bucket is None:
                continue
            try:
                bucket.remove(index)
            except ValueError:
                continue
            if not bucket:
                by_key.pop(key, None)

    for node in nodes:
        candidates: set[int] = set()
        for key in _merge_keys(node):
            candidates.update(by_key.get(key, ()))
        selected: tuple[int, int] | None = None
        for index in candidates:
            axis = _merge_axis(result[index], node)
            if axis is None:
                continue
            if selected is None or index > selected[0]:
                selected = (index, axis)
        if selected is None:
            index = len(result)
            result.append(deepcopy(node))
            add_index(result[index], index)
            continue
        index, axis = selected
        remove_index(index)
        result[index] = _merge_nodes(result[index], node, axis)
        add_index(result[index], index)
    for index, node in enumerate(result):
        node["id"] = f"n{index:06d}"
    return result


def _merge_keys(node: dict[str, Any]) -> list[tuple[Any, ...]]:
    dimensions = len(node["size"])
    source_signature = tuple(
        (
            source_input.get("source_id"),
            source_input.get("source_rank"),
            source_input.get("checksum"),
            tuple(
                source_input["source_offset"][dim]
                for dim in range(dimensions)
            ),
        )
        for source_input in node.get("inputs", [])
    )
    keys = []
    for axis in range(dimensions):
        target_signature = tuple(
            (node["target_offset"][dim], node["size"][dim])
            for dim in range(dimensions)
            if dim != axis
        )
        source_signature_without_axis = tuple(
            (
                source_id,
                source_rank,
                checksum,
                tuple(
                    offset
                    for dim, offset in enumerate(offsets)
                    if dim != axis
                ),
            )
            for source_id, source_rank, checksum, offsets in source_signature
        )
        keys.append(
            (
                node["tensor"],
                node["target_rank"],
                node["op"],
                dimensions,
                axis,
                target_signature,
                source_signature_without_axis,
            )
        )
    return keys


def _merge_axis(left: dict[str, Any], right: dict[str, Any]) -> int | None:
    if (
        left["tensor"] != right["tensor"]
        or left["target_rank"] != right["target_rank"]
        or left["op"] != right["op"]
        or len(left["size"]) != len(right["size"])
        or len(left["inputs"]) != len(right["inputs"])
    ):
        return None
    dimensions = len(left["size"])
    if dimensions == 0:
        return None
    for axis in range(dimensions):
        if all(
            left["target_offset"][dim] == right["target_offset"][dim]
            and left["size"][dim] == right["size"][dim]
            for dim in range(dimensions)
            if dim != axis
        ) and (
            left["target_offset"][axis] + left["size"][axis]
            == right["target_offset"][axis]
        ):
            source_ok = True
            for left_input, right_input in zip(left["inputs"], right["inputs"]):
                if (
                    left_input.get("source_id") != right_input.get("source_id")
                    or left_input.get("source_rank") != right_input.get("source_rank")
                    or left_input.get("checksum") != right_input.get("checksum")
                    or any(
                        left_input["source_offset"][dim]
                        != right_input["source_offset"][dim]
                        for dim in range(dimensions)
                        if dim != axis
                    )
                    or (
                        left_input["source_offset"][axis] + left["size"][axis]
                        != right_input["source_offset"][axis]
                    )
                ):
                    source_ok = False
                    break
            if source_ok:
                return axis
    return None


def _merge_nodes(
    left: dict[str, Any],
    right: dict[str, Any],
    axis: int,
) -> dict[str, Any]:
    merged = deepcopy(left)
    merged["size"][axis] += right["size"][axis]
    return merged


def _validate_cost(plan_payload: dict[str, Any], spec: dict[str, Any]) -> None:
    cost = plan_payload.get("cost")
    if cost is None:
        return
    nodes = plan_payload["nodes"]
    fan_ins = [len(node.get("inputs", [])) for node in nodes]
    computed = _plan_cost(spec, nodes, plan_payload["total_bytes"])
    expected = {
        "bytes": plan_payload["total_bytes"],
        "range_reads": computed["range_reads"],
        "local_reads": computed["local_reads"],
        "remote_reads": computed["remote_reads"],
        "max_fan_in": max(fan_ins, default=0),
        "nodes": len(nodes),
    }
    if cost != expected:
        raise PlannerError(
            f"plan cost does not match nodes: {cost} != {expected}"
        )


def _partial_layout(mesh: Mesh) -> tuple[tuple[int, ...], str]:
    axes = tuple(
        axis
        for axis, placement in enumerate(mesh.placements)
        if placement.kind == "partial"
    )
    ops = {
        mesh.placements[axis].reduce_op
        for axis in axes
        if mesh.placements[axis].reduce_op is not None
    }
    if len(ops) > 1:
        raise PlannerError("multiple Partial reduce operations are not supported")
    return axes, next(iter(ops), "sum") if axes else "copy"


def _is_partial_owner(
    rank: int,
    mesh: Mesh,
    partial_axes: tuple[int, ...],
) -> bool:
    coord = rank_coord(rank, mesh.shape)
    return all(coord[axis] == 0 for axis in partial_axes)


def _plan_region(
    tensor_name: str,
    target_rank: int,
    target_region: Region,
    source_index: _SourceIndex,
    source_mesh: Mesh,
    partial_axes: tuple[int, ...],
    reduce_op: str,
    item_size: int,
) -> list[tuple[str, list[SourceShard], Region]]:
    if not source_index.sources:
        raise PlannerError(f"no source shards for tensor {tensor_name}")

    relevant = source_index.query(target_region)
    if not relevant:
        raise PlannerError(
            f"{tensor_name}/rank{target_rank}: no source overlaps target region"
        )
    if partial_axes:
        ranks = [source.rank for source in relevant]
        if len(ranks) != len(set(ranks)):
            raise PlannerError(
                f"{tensor_name}/rank{target_rank}: multiple source chunks "
                "per Partial rank are not supported"
            )
    full_cover = [
        source
        for source in relevant
        if contains(
            source.region,
            target_region.offset,
            target_region.size,
        )
    ]
    if not partial_axes and full_cover:
        selected = min(
            full_cover,
            key=lambda source: _source_read_cost(
                source,
                target_region,
                target_rank,
                item_size,
            ),
        )
        return [("copy", [selected], target_region)]
    if partial_axes:
        full_group = _select_partial_group(
            full_cover,
            source_mesh,
            partial_axes,
            target_rank,
        )
        if full_group is not None:
            return [(reduce_op, full_group, target_region)]

    result: list[tuple[str, list[SourceShard], Region]] = []
    for target_cell in _iter_cells(target_region, relevant):
        covering = [
            source
            for source in relevant
            if contains(source.region, target_cell.offset, target_cell.size)
        ]
        if not covering:
            raise PlannerError(
                f"{tensor_name}/rank{target_rank}: source coverage gap at "
                f"{target_cell.offset}"
            )
        if not partial_axes:
            selected = min(
                covering,
                key=lambda source: _source_read_cost(
                    source,
                    target_cell,
                    target_rank,
                    item_size,
                ),
            )
            result.append(("copy", [selected], target_cell))
            continue
        selected_group = _select_partial_group(
            covering,
            source_mesh,
            partial_axes,
            target_rank,
        )
        if selected_group is None:
            raise PlannerError(
                f"{tensor_name}/rank{target_rank}: incomplete Partial source "
                f"group at {target_cell.offset}"
            )
        result.append((reduce_op, selected_group, target_cell))
    return result


def _source_read_cost(
    source: SourceShard,
    target: Region,
    target_rank: int,
    item_size: int,
) -> tuple[int, int, int]:
    local_offset = tuple(
        value - base
        for value, base in zip(target.offset, source.region.offset)
    )
    runs = byte_runs(
        source.region.size,
        local_offset,
        target.size,
        item_size,
    )
    return (
        source.rank != target_rank,
        len(coalesce_runs(runs)),
        source.index,
    )


def _overlaps(region: Region, target: Region) -> bool:
    return all(
        left_start < right_start + right_length
        and right_start < left_start + left_length
        for left_start, left_length, right_start, right_length in zip(
            region.offset,
            region.size,
            target.offset,
            target.size,
        )
    )


def _select_partial_group(
    covering: list[SourceShard],
    mesh: Mesh,
    partial_axes: tuple[int, ...],
    target_rank: int,
) -> list[SourceShard] | None:
    by_rank: dict[int, SourceShard] = {}
    for source in covering:
        current = by_rank.get(source.rank)
        if current is None or source.index < current.index:
            by_rank[source.rank] = source
    groups: dict[tuple[int, ...], list[SourceShard]] = {}
    non_partial_axes = tuple(
        axis for axis in range(len(mesh.shape)) if axis not in partial_axes
    )
    for source in by_rank.values():
        key = tuple(source.coord[axis] for axis in non_partial_axes)
        groups.setdefault(key, []).append(source)

    complete: list[list[SourceShard]] = []
    for members in groups.values():
        base_coord = list(members[0].coord)
        expected = set()
        for partial_coord in product(
            *(range(mesh.shape[axis]) for axis in partial_axes)
        ):
            coord = list(base_coord)
            for axis, value in zip(partial_axes, partial_coord):
                coord[axis] = value
            expected.add(mesh_coord_rank(tuple(coord), mesh.shape))
        if expected <= {member.rank for member in members}:
            complete.append(members)
    if not complete:
        return None
    selected = min(
        complete,
        key=lambda members: (
            target_rank not in {member.rank for member in members},
            tuple(sorted(member.rank for member in members)),
        ),
    )
    return sorted(selected, key=lambda member: member.rank)


def mesh_coord_rank(coord: tuple[int, ...], shape: tuple[int, ...]) -> int:
    rank = 0
    for value, dimension in zip(coord, shape):
        if value < 0 or value >= dimension:
            raise ValueError(f"coordinate {coord} is out of range for {shape}")
        rank = rank * dimension + value
    return rank


def _iter_cells(target: Region, sources: list[SourceShard]) -> list[Region]:
    intervals: list[list[tuple[int, int]]] = []
    for dim, (start, length) in enumerate(zip(target.offset, target.size)):
        end = start + length
        boundaries = {start, end}
        for source in sources:
            source_start = source.region.offset[dim]
            source_end = source_start + source.region.size[dim]
            overlap_start = max(start, source_start)
            overlap_end = min(end, source_end)
            if overlap_start < overlap_end:
                boundaries.add(overlap_start)
                boundaries.add(overlap_end)
        ordered = sorted(boundaries)
        intervals.append(
            [
                (left, right - left)
                for left, right in zip(ordered, ordered[1:])
                if right > left
            ]
        )

    cells: list[Region] = []
    for selected in product(*intervals):
        cells.append(
            Region(
                offset=tuple(item[0] for item in selected),
                size=tuple(item[1] for item in selected),
            )
        )
    return cells


def _check_exact_cover(expected: Region, regions: list[Region], label: str) -> None:
    if expected.element_count == 0:
        if regions:
            raise PlannerError(f"{label}: empty target has plan nodes")
        return
    intervals: list[list[tuple[int, int]]] = []
    for dim, (start, length) in enumerate(zip(expected.offset, expected.size)):
        end = start + length
        boundaries = {start, end}
        for region in regions:
            region_start = region.offset[dim]
            region_end = region_start + region.size[dim]
            if region_start < end and region_end > start:
                boundaries.add(max(start, region_start))
                boundaries.add(min(end, region_end))
        ordered = sorted(boundaries)
        intervals.append(
            [
                (left, right - left)
                for left, right in zip(ordered, ordered[1:])
                if right > left
            ]
        )
    for selected in product(*intervals):
        offset = tuple(item[0] for item in selected)
        size = tuple(item[1] for item in selected)
        count = sum(contains(region, offset, size) for region in regions)
        if count != 1:
            raise PlannerError(f"{label}: target coverage count is {count}")


def _validate_schema(payload: dict[str, Any]) -> None:
    validator = _plan_validator()
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        details = "; ".join(error.message for error in errors)
        raise PlannerError(f"plan schema validation failed: {details}")


@lru_cache(maxsize=1)
def _plan_validator() -> Draft202012Validator:
    if not PLAN_SCHEMA_PATH.is_file():
        raise PlannerError(f"plan schema not found: {PLAN_SCHEMA_PATH}")
    schema = json.loads(PLAN_SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _migration_id(spec: dict[str, Any]) -> str:
    identity = deepcopy(spec)
    identity["source"]["storage"]["uri"] = ""
    identity["target"]["storage"]["uri"] = ""
    identity["source"]["mesh"].pop("rank_map", None)
    identity["checkpoint"]["tensors"].sort(key=lambda tensor: tensor["name"])
    for shard in identity["source"]["shards"]:
        shard["uri"] = ""
        shard.pop("id", None)
        shard.pop("id", None)
    identity["source"]["shards"].sort(
        key=lambda shard: (
            shard["tensor"],
            shard["rank"],
            tuple(shard["offset"]),
            tuple(shard["size"]),
            shard["checksum"],
        )
    )
    payload = PLAN_POLICY + "\0" + json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
