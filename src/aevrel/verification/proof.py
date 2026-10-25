from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from ..identity import resolve_source, source_maps
from ..planning.layout import DTYPE_BYTES, local_region
from ..planning.mesh import parse_mesh
from ..planning import validate_plan
from ..resources import resource_path
from ..storage.file import FileStorage
from ..storage.router import StorageRouter
from ..targets import target_file_path
from ..planning.tensor_io import (
    byte_runs,
    reduce_bytes,
)
from ..fastkernels import merkle_root


PROOF_VERSION = "2"
DEFAULT_LEAF_BYTES = 4 * 1024 * 1024
PROOF_SCHEMA_PATH = resource_path("proof.schema.json")
_RESUME_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ProofError(ValueError):
    """Raised when a migration proof is incomplete or invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _digest_bytes(data: bytes) -> str:
    """``sha256:<hex>`` for one buffer, through OpenSSL.

    Deliberately *not* routed to the native kernel. Measured on the dev box,
    `hashlib` and the Rust `sha2` path are level for small payloads (1.03x
    native at 64 bytes) and the native binding is 4% slower on a 4 MiB leaf,
    which is the size this function actually sees. Raw SHA-256 is where OpenSSL's
    SHA-NI assembly already wins; the migration that pays is `_merkle_root`,
    where the win is deleting a Python loop rather than the hash primitive.
    `tests/unit/test_fastkernels.py` pins both bindings to the same result so
    the duplication cannot drift.
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _resume_digest(value: str | None) -> str | None:
    """Validate an optional resume-state manifest digest.

    ``None`` states that the proof makes no resume-closure claim. Any other
    value must be a canonical ``sha256:<64 hex>`` digest so a verifier can
    compare it byte for byte against the manifest it holds.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _RESUME_DIGEST_PATTERN.fullmatch(value):
        raise ProofError(f"invalid resume manifest digest: {value!r}")
    return value


def _merkle_root(digests: list[str]) -> str:
    """Fold `sha256:<hex>` digests into one root over the native kernel.

    Worth 3.7x-5.1x over the retired Python tree walk (64 to 65536 leaves),
    because the whole fold - sort, domain separation, odd-node duplication and
    every node hash - happens in one call instead of two Python iterations per
    node. The native implementation also rejects malformed digests instead of
    hashing whatever bytes happen to decode.
    """
    return merkle_root(digests)


def _proof_root(proof: dict[str, Any]) -> str:
    return _digest_bytes(
        _canonical(
            {
                "plan_id": proof["plan_id"],
                "plan_hash": proof["plan_hash"],
                "target_root": proof["target_root"],
                "target_files_root": proof["target_files_root"],
                "source_root": proof["source_root"],
                "coverage_root": proof["coverage_root"],
                "resume_manifest_digest": proof["resume_manifest_digest"],
            }
        )
    )


def _read_digest(
    reader: StorageRouter,
    uri: str,
    offset: int,
    length: int,
) -> str:
    return _digest_bytes(reader.read_range(uri, offset, length))


def build_proof(
    spec: dict[str, Any],
    plan: dict[str, Any],
    target_root: str | Path,
    *,
    source: str | None = None,
    token: str | None = None,
    target_rank: int | None = None,
    resume_manifest_digest: str | None,
) -> dict[str, Any]:
    """Build a compact Merkle/coverage proof for a completed migration.

    A rank-scoped proof is used by a target agent that only owns one rank's
    files. Several rank proofs can be combined with :func:`combine_proofs`.

    ``resume_manifest_digest`` binds the proof to one resume-state manifest.
    It is part of the proof root, so a proof cannot be replayed for a
    different closure. Pass ``None`` only to state that this proof makes no
    resume-closure claim.
    """
    validate_plan(plan, spec)
    resume_manifest_digest = _resume_digest(resume_manifest_digest)
    source_storage = StorageRouter(source_base=source, token=token)
    target_storage = FileStorage()
    target_root_path = Path(target_root).resolve()
    target_mesh = parse_mesh(spec["target"]["mesh"])
    if target_rank is not None and not 0 <= target_rank < target_mesh.world_size:
        raise ProofError(f"target rank {target_rank} is out of range")
    source_by_id, source_by_rank = source_maps(spec["source"]["shards"])
    tensors = {
        tensor["name"]: tensor for tensor in spec["checkpoint"]["tensors"]
    }
    target_files: list[dict[str, Any]] = []
    for tensor_name, tensor in tensors.items():
        for rank in range(target_mesh.world_size):
            if target_rank is not None and rank != target_rank:
                continue
            region = local_region(
                target_mesh,
                target_mesh.placements,
                tuple(tensor["shape"]),
                rank,
                allow_partial=True,
            )
            path = target_file_path(
                spec,
                tensor_name,
                rank,
                root=target_root_path,
            )
            target_files.append(
                {
                    "file": path.relative_to(target_root_path).as_posix(),
                    "size": region.element_count * DTYPE_BYTES[tensor["dtype"]],
                }
            )

    target_leaves: dict[tuple[str, int, int], dict[str, Any]] = {}
    source_leaves: dict[tuple[str, int, int], dict[str, Any]] = {}
    coverage: list[dict[str, Any]] = []

    for node in plan["nodes"]:
        tensor_name = node["tensor"]
        rank = int(node["target_rank"])
        if target_rank is not None and rank != target_rank:
            continue
        tensor = tensors[tensor_name]
        item_size = DTYPE_BYTES[tensor["dtype"]]
        target_region = local_region(
            target_mesh,
            target_mesh.placements,
            tuple(tensor["shape"]),
            rank,
            allow_partial=True,
        )
        target_path = target_file_path(
            spec,
            tensor_name,
            rank,
            root=target_root_path,
        )
        relative = target_path.relative_to(target_root_path).as_posix()
        target_local_offset = tuple(
            value - base
            for value, base in zip(node["target_offset"], target_region.offset)
        )
        target_runs = byte_runs(
            target_region.size,
            target_local_offset,
            tuple(node["size"]),
            item_size,
        )
        source_meta = []
        source_run_lists = []
        for source_input in node["inputs"]:
            shard = resolve_source(
                source_by_id,
                source_by_rank,
                tensor_name,
                source_input,
            )
            source_local_offset = tuple(
                value - base
                for value, base in zip(
                    source_input["source_offset"],
                    shard["offset"],
                )
            )
            source_runs = byte_runs(
                tuple(shard["size"]),
                source_local_offset,
                tuple(node["size"]),
                item_size,
            )
            if len(source_runs) != len(target_runs):
                raise ProofError(
                    f"source/target run count mismatch for {node['id']}"
                )
            source_id = source_input.get("source_id") or source_input["source_rank"]
            source_run_lists.append(source_runs)
            source_meta.append((str(source_id), shard))

        for run_index, (target_offset, target_length) in enumerate(target_runs):
            source_ranges = [
                runs[run_index] for runs in source_run_lists
            ]
            target_digest = _read_digest(
                target_storage,
                target_path.as_uri(),
                target_offset,
                target_length,
            )
            target_key = (relative, target_offset, target_length)
            target_leaves[target_key] = {
                "file": relative,
                "offset": target_offset,
                "length": target_length,
                "digest": target_digest,
            }
            source_refs = []
            for (source_id, shard), (offset, length) in zip(
                source_meta,
                source_ranges,
            ):
                digest = _read_digest(
                    source_storage,
                    shard["uri"],
                    offset,
                    length,
                )
                key = (str(source_id), offset, length)
                source_leaves[key] = {
                    "source_id": str(source_id),
                    "tensor": tensor_name,
                    "rank": int(shard["rank"]),
                    "uri": shard["uri"],
                    "offset": offset,
                    "length": length,
                    "digest": digest,
                }
                source_refs.append([str(source_id), offset, length])

            coverage.append(
                {
                    "id": f"{node['id']}:{run_index:06d}",
                    "tensor": tensor_name,
                    "target_rank": rank,
                    "op": node["op"],
                    "dtype": tensor["dtype"],
                    "target": [relative, target_offset, target_length],
                    "sources": source_refs,
                }
            )

    ordered_targets = [
        target_leaves[key] for key in sorted(target_leaves)
    ]
    ordered_sources = [
        source_leaves[key] for key in sorted(source_leaves)
    ]
    coverage_payload = sorted(coverage, key=lambda item: item["id"])
    target_root_hash = _merkle_root(
        [leaf["digest"] for leaf in ordered_targets]
    )
    target_files_root = _merkle_root(
        [_digest_bytes(_canonical(item)) for item in target_files]
    )
    source_root_hash = _merkle_root(
        [leaf["digest"] for leaf in ordered_sources]
    )
    coverage_root = _merkle_root(
        [_digest_bytes(_canonical(item)) for item in coverage_payload]
    )
    result = {
        "version": PROOF_VERSION,
        "plan_id": plan["migration_id"],
        "plan_hash": _digest_bytes(_canonical(plan)),
        "target_root": target_root_hash,
        "target_files_root": target_files_root,
        "target_files": target_files,
        "source_root": source_root_hash,
        "coverage_root": coverage_root,
        "resume_manifest_digest": resume_manifest_digest,
        "root": "",
        "target_leaves": ordered_targets,
        "source_leaves": ordered_sources,
        "coverage": coverage_payload,
    }
    result["root"] = _proof_root(result)
    validate_proof(result)
    return result


def combine_proofs(proofs: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine rank-scoped proofs into one migration proof.

    Rank proofs must come from the same plan. Source leaves are deduplicated
    by their content identity; target files and coverage must not overlap.
    """
    if not proofs:
        raise ProofError("at least one proof is required")

    plan_id: str | None = None
    plan_hash: str | None = None
    resume_manifest_digest: str | None = None
    resume_bound = False
    target_files: list[dict[str, Any]] = []
    target_leaves: dict[tuple[str, int, int], dict[str, Any]] = {}
    source_leaves: dict[tuple[str, int, int], dict[str, Any]] = {}
    coverage: dict[str, dict[str, Any]] = {}

    for proof in proofs:
        validate_proof(proof)
        if proof.get("version") != PROOF_VERSION:
            raise ProofError("unsupported proof version")
        if plan_id is None:
            plan_id = proof["plan_id"]
            plan_hash = proof["plan_hash"]
        elif (
            proof["plan_id"] != plan_id
            or proof["plan_hash"] != plan_hash
        ):
            raise ProofError("cannot combine proofs from different plans")

        if not resume_bound:
            resume_manifest_digest = proof["resume_manifest_digest"]
            resume_bound = True
        elif proof["resume_manifest_digest"] != resume_manifest_digest:
            raise ProofError(
                "cannot combine proofs with different resume manifest digests"
            )

        for entry in proof.get("target_files", []):
            name = entry["file"]
            if any(item["file"] == name for item in target_files):
                raise ProofError(f"duplicate target file in proof: {name}")
            target_files.append(dict(entry))

        for leaf in proof.get("target_leaves", []):
            key = (
                str(leaf["file"]),
                int(leaf["offset"]),
                int(leaf["length"]),
            )
            previous = target_leaves.get(key)
            if previous is not None and previous["digest"] != leaf["digest"]:
                raise ProofError(f"conflicting target leaf in proof: {key}")
            target_leaves[key] = dict(leaf)

        for leaf in proof.get("source_leaves", []):
            key = (
                str(leaf["source_id"]),
                int(leaf["offset"]),
                int(leaf["length"]),
            )
            previous = source_leaves.get(key)
            if previous is not None and previous["digest"] != leaf["digest"]:
                raise ProofError(f"conflicting source leaf in proof: {key}")
            source_leaves[key] = dict(leaf)

        for item in proof.get("coverage", []):
            identifier = str(item["id"])
            previous = coverage.get(identifier)
            if previous is not None and previous != item:
                raise ProofError(f"conflicting coverage entry in proof: {identifier}")
            coverage[identifier] = dict(item)

    target_files.sort(key=lambda item: item["file"])
    ordered_targets = [
        target_leaves[key] for key in sorted(target_leaves)
    ]
    ordered_sources = [
        source_leaves[key] for key in sorted(source_leaves)
    ]
    coverage_payload = sorted(coverage.values(), key=lambda item: item["id"])
    result = {
        "version": PROOF_VERSION,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "target_root": _merkle_root(
            [leaf["digest"] for leaf in ordered_targets]
        ),
        "target_files_root": _merkle_root(
            [_digest_bytes(_canonical(item)) for item in target_files]
        ),
        "target_files": target_files,
        "source_root": _merkle_root(
            [leaf["digest"] for leaf in ordered_sources]
        ),
        "coverage_root": _merkle_root(
            [_digest_bytes(_canonical(item)) for item in coverage_payload]
        ),
        "resume_manifest_digest": resume_manifest_digest,
        "root": "",
        "target_leaves": ordered_targets,
        "source_leaves": ordered_sources,
        "coverage": coverage_payload,
    }
    result["root"] = _proof_root(result)
    validate_proof(result)
    return result


def verify_proof(
    proof: dict[str, Any],
    target_root: str | Path,
    *,
    plan: dict[str, Any] | None = None,
    source: str | None = None,
    token: str | None = None,
    resume_manifest_digest: str | None = None,
) -> dict[str, Any]:
    """Verify target bytes from a proof; source verification is optional.

    When ``resume_manifest_digest`` is given the proof must be bound to that
    exact manifest digest, so a proof built for one closure cannot verify a
    different one.
    """
    validate_proof(proof)
    if proof.get("version") != PROOF_VERSION:
        raise ProofError("unsupported proof version")
    if plan is not None and _digest_bytes(_canonical(plan)) != proof.get("plan_hash"):
        raise ProofError("plan hash mismatch")
    expected_resume = _resume_digest(resume_manifest_digest)
    if (
        expected_resume is not None
        and proof.get("resume_manifest_digest") != expected_resume
    ):
        raise ProofError("resume manifest digest mismatch")
    target_root_path = Path(target_root).resolve()
    target_storage = FileStorage()
    target_leaves = proof.get("target_leaves")
    source_leaves = proof.get("source_leaves")
    coverage = proof.get("coverage")
    target_files = proof.get("target_files")
    if not isinstance(target_leaves, list) or not isinstance(source_leaves, list):
        raise ProofError("proof leaves are missing")
    if not isinstance(coverage, list):
        raise ProofError("proof coverage is missing")
    if not isinstance(target_files, list):
        raise ProofError("proof target file manifest is missing")

    target_file_names = set()
    for entry in target_files:
        path = target_root_path / entry["file"]
        if not path.is_file():
            raise ProofError(f"target file not found: {entry['file']}")
        actual_size = path.stat().st_size
        if actual_size != int(entry["size"]):
            raise ProofError(
                f"target file size mismatch: {entry['file']} "
                f"{actual_size} != {entry['size']}"
            )
        target_file_names.add(entry["file"])

    target_digests = []
    sizes_by_file: dict[str, int] = {}
    for leaf in target_leaves:
        path = target_root_path / leaf["file"]
        digest = _read_digest(
            target_storage,
            path.as_uri(),
            int(leaf["offset"]),
            int(leaf["length"]),
        )
        if digest != leaf["digest"]:
            raise ProofError(
                f"target leaf mismatch: {leaf['file']}@{leaf['offset']}"
            )
        target_digests.append(digest)
        sizes_by_file[leaf["file"]] = (
            sizes_by_file.get(leaf["file"], 0) + int(leaf["length"])
        )
    if _merkle_root(target_digests) != proof["target_root"]:
        raise ProofError("target Merkle root mismatch")
    if _merkle_root(
        [_digest_bytes(_canonical(item)) for item in coverage]
    ) != proof["coverage_root"]:
        raise ProofError("coverage root mismatch")
    if _merkle_root(
        [_digest_bytes(_canonical(item)) for item in target_files]
    ) != proof["target_files_root"]:
        raise ProofError("target files root mismatch")
    target_keys = {
        (leaf["file"], int(leaf["offset"]), int(leaf["length"]))
        for leaf in target_leaves
    }
    referenced_targets = Counter()
    for item in coverage:
        entry = item.get("target", [])
        if len(entry) != 3:
            raise ProofError("coverage target must have three fields")
        referenced_targets[(entry[0], int(entry[1]), int(entry[2]))] += 1
    if set(referenced_targets) != target_keys:
        raise ProofError("coverage does not reference every target leaf")
    if {key[0] for key in target_keys} != target_file_names:
        raise ProofError("target leaf files do not match target file manifest")
    if any(count != 1 for count in referenced_targets.values()):
        raise ProofError("a target leaf is covered more than once")
    source_keys = {
        (leaf["source_id"], int(leaf["offset"]), int(leaf["length"]))
        for leaf in source_leaves
    }
    referenced_sources = {
        (entry[0], int(entry[1]), int(entry[2]))
        for item in coverage
        for entry in item.get("sources", [])
    }
    if not referenced_sources <= source_keys:
        raise ProofError("coverage references an unknown source leaf")

    source_verified = False
    if source is not None:
        source_storage = StorageRouter(source_base=source, token=token)
        source_digests = []
        for leaf in source_leaves:
            data = source_storage.read_range(
                leaf["uri"],
                int(leaf["offset"]),
                int(leaf["length"]),
            )
            digest = _digest_bytes(data)
            if digest != leaf["digest"]:
                raise ProofError(
                    f"source leaf mismatch: {leaf['source_id']}@{leaf['offset']}"
                )
            source_digests.append(digest)
        if _merkle_root(source_digests) != proof["source_root"]:
            raise ProofError("source Merkle root mismatch")
        source_uri_by_key = {
            (
                leaf["source_id"],
                int(leaf["offset"]),
                int(leaf["length"]),
            ): leaf["uri"]
            for leaf in source_leaves
        }
        target_storage = FileStorage()
        for item in coverage:
            target_file, target_offset, target_length = item["target"]
            target_path = target_root_path / target_file
            if item["op"] == "zeros":
                expected = b"\0" * int(target_length)
            else:
                source_data_list = []
                for ref in item.get("sources", []):
                    key = (ref[0], int(ref[1]), int(ref[2]))
                    try:
                        source_uri = source_uri_by_key[key]
                    except KeyError as exc:
                        raise ProofError(
                            f"coverage references missing source leaf: {key}"
                        ) from exc
                    source_data_list.append(
                        source_storage.read_range(
                            source_uri,
                            int(ref[1]),
                            int(ref[2]),
                        )
                    )
                expected = reduce_bytes(
                    source_data_list,
                    item["op"],
                    item["dtype"],
                )
            actual = target_storage.read_range(
                target_path.as_uri(),
                int(target_offset),
                int(target_length),
            )
            if not hmac.compare_digest(actual, expected):
                raise ProofError(
                    f"reduction mismatch: {target_file}@{target_offset}"
                )
        source_verified = True

    if proof.get("root") != _proof_root(proof):
        raise ProofError("proof root mismatch")
    return {
        "passed": True,
        "method": "merkle",
        "target_bytes": sum(sizes_by_file.values()),
        "target_files": len(sizes_by_file),
        "source_verified": source_verified,
        "reduction_verified": source_verified,
        "resume_bound": proof.get("resume_manifest_digest") is not None,
    }


def validate_proof(proof: dict[str, Any]) -> None:
    try:
        schema = json.loads(PROOF_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProofError(f"could not read proof schema: {exc}") from exc
    errors = list(Draft202012Validator(schema).iter_errors(proof))
    if errors:
        details = "; ".join(error.message for error in errors)
        raise ProofError(f"proof schema validation failed: {details}")
