"""Checkpoint-level resume-state closure contract.

This module implements the structural half of RFC #30: a resume-state
manifest declares *which* resume components exist for a checkpoint, who
provides them, their digests and their rank coverage, and a read-only
closure checker classifies a manifest as COMPLETE / PARTIAL / MISSING /
MALFORMED with specific reason codes.

Hard boundary: the manifest is **structural only**. It never asserts that
a checkpoint is loadable or that a resume will succeed — loadability is
proven by restore drills and readiness gates, not by this contract. A
COMPLETE classification means "every required component is declared,
present and covered"; it does not mean "safe to resume".

Required set (frozen, adapter-layer authoritative):
    model, optimizer, global_step, dataloader, rng
Optional-but-must-declare set:
    lr_scheduler, sampler, rank_local
The manifest-level ``topology`` block is mandatory by schema; provider-
defined component kinds are allowed to exist but can never be marked
required by the core closure (the core cannot verify them).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from ..local.errors import LocalError
from ..resources import resource_path
from .manifest import payload_digest

RESUME_STATE_SCHEMA_VERSION = "1"

SCHEMA_PATH = resource_path("v1", "resume-state.schema.json")

RESUME_REQUIRED_COMPONENTS = (
    "model",
    "optimizer",
    "global_step",
    "dataloader",
    "rng",
)
RESUME_OPTIONAL_COMPONENTS = (
    "lr_scheduler",
    "sampler",
    "rank_local",
)
RESUME_KNOWN_COMPONENTS = frozenset(
    RESUME_REQUIRED_COMPONENTS + RESUME_OPTIONAL_COMPONENTS + ("topology",)
)

RESUME_STATE_COMPLETE = "COMPLETE"
RESUME_STATE_PARTIAL = "PARTIAL"
RESUME_STATE_MISSING = "MISSING"
RESUME_STATE_MALFORMED = "MALFORMED"
RESUME_STATE_STATUSES = frozenset(
    {
        RESUME_STATE_COMPLETE,
        RESUME_STATE_PARTIAL,
        RESUME_STATE_MISSING,
        RESUME_STATE_MALFORMED,
    }
)

# RFC #30 failure reason codes, plus one manifest-level structural code.
RESUME_COMPONENT_MISSING = "RESUME_COMPONENT_MISSING"
RESUME_COMPONENT_INVALID = "RESUME_COMPONENT_INVALID"
RESUME_COMPONENT_DIGEST_MISMATCH = "RESUME_COMPONENT_DIGEST_MISMATCH"
RESUME_RANK_COVERAGE_INCOMPLETE = "RESUME_RANK_COVERAGE_INCOMPLETE"
RESUME_TOPOLOGY_INCOMPATIBLE = "RESUME_TOPOLOGY_INCOMPATIBLE"
RESUME_STATE_UNVERIFIED = "RESUME_STATE_UNVERIFIED"
RESUME_MANIFEST_INVALID = "RESUME_MANIFEST_INVALID"

# Declared as the provider of a component a producer knows it did not
# capture; a present component must always name its real provider.
RESUME_PROVIDER_NONE = "none"
RESUME_REASON_CODES = frozenset(
    {
        RESUME_COMPONENT_MISSING,
        RESUME_COMPONENT_INVALID,
        RESUME_COMPONENT_DIGEST_MISMATCH,
        RESUME_RANK_COVERAGE_INCOMPLETE,
        RESUME_TOPOLOGY_INCOMPATIBLE,
        RESUME_STATE_UNVERIFIED,
        RESUME_MANIFEST_INVALID,
    }
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

_VALIDATOR: Draft202012Validator | None = None


def _validator() -> Draft202012Validator:
    global _VALIDATOR
    if _VALIDATOR is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        _VALIDATOR = Draft202012Validator(schema)
    return _VALIDATOR


def _schema_errors(payload: Any) -> list[str]:
    errors = sorted(
        _validator().iter_errors(payload),
        key=lambda error: list(error.path),
    )
    return [error.message for error in errors]


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.match(value) is not None


@dataclass(frozen=True)
class ResumeComponentCoverage:
    """Rank coverage declared for one component."""

    world_size: int
    ranks: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.world_size < 1:
            raise ValueError("coverage world size must be positive")
        if any(rank < 0 for rank in self.ranks):
            raise ValueError("coverage ranks must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"worldSize": self.world_size, "ranks": list(self.ranks)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResumeComponentCoverage:
        return cls(
            world_size=int(payload["worldSize"]),
            ranks=tuple(int(rank) for rank in payload["ranks"]),
        )

    def is_full(self) -> bool:
        return set(self.ranks) == set(range(self.world_size))

    def missing_ranks(self) -> tuple[int, ...]:
        return tuple(
            rank for rank in range(self.world_size) if rank not in set(self.ranks)
        )


@dataclass(frozen=True)
class ResumeComponent:
    """One declared resume component (checkpoint-level, rank-aggregated)."""

    kind: str
    name: str
    provider: str
    digest: str | None
    coverage: ResumeComponentCoverage
    required: bool
    present: bool

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("component kind is required")
        if not self.name:
            raise ValueError("component name is required")
        if not self.provider:
            raise ValueError("component provider is required")
        if self.digest is not None and not _is_digest(self.digest):
            raise ValueError("component digest must be sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "provider": self.provider,
            "digest": self.digest,
            "coverage": self.coverage.to_dict(),
            "required": self.required,
            "present": self.present,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResumeComponent:
        return cls(
            kind=str(payload["kind"]),
            name=str(payload["name"]),
            provider=str(payload["provider"]),
            digest=None if payload["digest"] is None else str(payload["digest"]),
            coverage=ResumeComponentCoverage.from_dict(payload["coverage"]),
            required=bool(payload["required"]),
            present=bool(payload["present"]),
        )


@dataclass(frozen=True)
class ResumeTopology:
    """Topology the checkpoint was written under."""

    world_size: int
    mesh: str
    placements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.world_size < 1:
            raise ValueError("topology world size must be positive")
        if not self.mesh:
            raise ValueError("topology mesh is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "worldSize": self.world_size,
            "mesh": self.mesh,
            "placements": list(self.placements),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResumeTopology:
        return cls(
            world_size=int(payload["worldSize"]),
            mesh=str(payload["mesh"]),
            placements=tuple(str(item) for item in payload.get("placements", ())),
        )


@dataclass(frozen=True)
class ResumeCheckpoint:
    """Identity of the checkpoint this manifest describes."""

    uid: str
    version: str
    metadata_digest: str

    def __post_init__(self) -> None:
        if not self.uid:
            raise ValueError("checkpoint uid is required")
        if not self.version:
            raise ValueError("checkpoint version is required")
        if not _is_digest(self.metadata_digest):
            raise ValueError("checkpoint metadata digest must be sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "version": self.version,
            "metadataDigest": self.metadata_digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResumeCheckpoint:
        return cls(
            uid=str(payload["uid"]),
            version=str(payload["version"]),
            metadata_digest=str(payload["metadataDigest"]),
        )


@dataclass(frozen=True)
class ResumeStateManifest:
    """Checkpoint-level resume-state manifest (schema v1)."""

    schema_version: str
    checkpoint: ResumeCheckpoint
    topology: ResumeTopology
    components: tuple[ResumeComponent, ...]

    def __post_init__(self) -> None:
        if self.schema_version != RESUME_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported resume-state manifest schema {self.schema_version}"
            )
        # Canonical component order: declaration order carries no meaning and
        # a single order keeps round-trips lossless and digests stable.
        object.__setattr__(
            self,
            "components",
            tuple(
                sorted(self.components, key=lambda item: (item.kind, item.name))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "checkpoint": self.checkpoint.to_dict(),
            "topology": self.topology.to_dict(),
            "components": [
                component.to_dict() for component in self.components
            ],
        }

    def compute_digest(self) -> str:
        """Canonical digest of the manifest (order-independent)."""
        return payload_digest(self.to_dict())

    def component(self, kind: str) -> ResumeComponent | None:
        for component in self.components:
            if component.kind == kind:
                return component
        return None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ResumeStateManifest:
        return cls(
            schema_version=str(payload["schemaVersion"]),
            checkpoint=ResumeCheckpoint.from_dict(payload["checkpoint"]),
            topology=ResumeTopology.from_dict(payload["topology"]),
            components=tuple(
                ResumeComponent.from_dict(item) for item in payload["components"]
            ),
        )


@dataclass(frozen=True)
class ResumeReason:
    """One specific closure failure, with machine-readable details."""

    code: str
    detail: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ResumeStateReport:
    """Closure classification of a resume-state manifest.

    ``closed`` is the only positive signal and it is strictly structural:
    every required component declared, present and fully covered, with no
    contradicting declarations. It never implies loadability.
    """

    status: str
    reasons: tuple[ResumeReason, ...]
    manifest: ResumeStateManifest | None
    # Digest of the classified manifest, when one could be parsed. Carried
    # explicitly so a verdict that crosses a process boundary can still be
    # anchored to the manifest it describes.
    manifest_digest: str | None = None

    @property
    def closed(self) -> bool:
        return self.status == RESUME_STATE_COMPLETE and not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "closed": self.closed,
            "manifestDigest": self.manifest_digest,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }

    def to_error(self) -> LocalError | None:
        """Fail-closed error for non-closed manifests; None when closed."""
        if self.closed:
            return None
        code = self.reasons[0].code if self.reasons else RESUME_STATE_UNVERIFIED
        return LocalError(
            code,
            f"resume state is {self.status}",
            details={
                "status": self.status,
                "reasons": [reason.to_dict() for reason in self.reasons],
            },
        )


def build_manifest(
    *,
    checkpoint_uid: str,
    version: str,
    metadata_digest: str,
    world_size: int,
    present: Mapping[str, str],
    digests: Mapping[str, str] | None = None,
    covered_ranks: Mapping[str, Sequence[int]] | None = None,
    absent_provider: str = RESUME_PROVIDER_NONE,
    mesh: str = "1d",
    placements: Sequence[str] = (),
) -> ResumeStateManifest:
    """Declare exactly the resume components a producer verified.

    ``present`` maps component kind to the provider that produced it.
    Every core kind (required and optional) is always declared, present or
    not: declaring a component absent is itself information, and dropping it
    would make a partial checkpoint look structurally simpler than it is.
    Only kinds in ``present`` are marked present, so nothing is invented and
    the resulting closure stays truthful.
    """
    digests = digests or {}
    ranks_by_kind = covered_ranks or {}
    all_ranks = tuple(range(world_size))

    def coverage(kind: str) -> ResumeComponentCoverage:
        return ResumeComponentCoverage(
            world_size=world_size,
            ranks=tuple(ranks_by_kind.get(kind, all_ranks)),
        )

    components: list[ResumeComponent] = []
    for kind in RESUME_REQUIRED_COMPONENTS + RESUME_OPTIONAL_COMPONENTS:
        provider = present.get(kind)
        components.append(
            ResumeComponent(
                kind=kind,
                name=kind,
                provider=provider if provider is not None else absent_provider,
                digest=digests.get(kind) if provider is not None else None,
                coverage=coverage(kind),
                required=kind in RESUME_REQUIRED_COMPONENTS,
                present=provider is not None,
            )
        )
    for kind in present:
        if kind in RESUME_KNOWN_COMPONENTS:
            continue
        components.append(
            ResumeComponent(
                kind=kind,
                name=kind,
                provider=present[kind],
                digest=digests.get(kind),
                coverage=coverage(kind),
                required=False,
                present=True,
            )
        )
    return ResumeStateManifest(
        schema_version=RESUME_STATE_SCHEMA_VERSION,
        checkpoint=ResumeCheckpoint(
            uid=checkpoint_uid,
            version=version,
            metadata_digest=metadata_digest,
        ),
        topology=ResumeTopology(
            world_size=world_size,
            mesh=mesh,
            placements=tuple(placements),
        ),
        components=tuple(components),
    )


def parse_resume_state(payload: Any) -> ResumeStateManifest:
    """Strict path: schema-validate and return the typed manifest.

    Raises ValueError on schema violations or duplicate component kinds.
    """
    errors = _schema_errors(payload)
    if errors:
        raise ValueError(f"resume-state manifest validation failed: {errors[0]}")
    manifest = ResumeStateManifest.from_dict(payload)
    duplicates = _duplicate_kinds(manifest)
    if duplicates:
        raise ValueError(
            "resume-state manifest has duplicate component kinds: "
            + ", ".join(sorted(duplicates))
        )
    return manifest


def classify_resume_state(payload: Any) -> ResumeStateReport:
    """Read-only closure classification; never raises for bad input.

    Malformed payloads (schema violations) classify as MALFORMED.
    """
    errors = _schema_errors(payload)
    if errors:
        return ResumeStateReport(
            status=RESUME_STATE_MALFORMED,
            reasons=(
                ResumeReason(
                    code=RESUME_MANIFEST_INVALID,
                    detail=errors[0],
                    details={"schemaErrors": errors},
                ),
            ),
            manifest=None,
        )
    manifest = ResumeStateManifest.from_dict(payload)
    reasons = _semantic_reasons(manifest)
    required_present = sum(
        1
        for component in manifest.components
        if component.kind in RESUME_REQUIRED_COMPONENTS and component.present
    )
    if not reasons:
        status = RESUME_STATE_COMPLETE
    elif required_present == 0:
        status = RESUME_STATE_MISSING
    else:
        status = RESUME_STATE_PARTIAL
    return ResumeStateReport(
        status=status,
        reasons=tuple(reasons),
        manifest=manifest,
        manifest_digest=manifest.compute_digest(),
    )


def _duplicate_kinds(manifest: ResumeStateManifest) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for component in manifest.components:
        if component.kind in seen:
            duplicates.add(component.kind)
        seen.add(component.kind)
    return duplicates


def _semantic_reasons(manifest: ResumeStateManifest) -> list[ResumeReason]:
    reasons: list[ResumeReason] = []

    for kind in sorted(_duplicate_kinds(manifest)):
        reasons.append(
            ResumeReason(
                code=RESUME_COMPONENT_INVALID,
                detail=f"duplicate component kind: {kind}",
                details={"kind": kind},
            )
        )

    placements = manifest.topology.placements
    if placements and len(placements) != manifest.topology.world_size:
        reasons.append(
            ResumeReason(
                code=RESUME_TOPOLOGY_INCOMPATIBLE,
                detail=(
                    "topology placements do not cover world size "
                    f"{manifest.topology.world_size}"
                ),
                details={
                    "worldSize": manifest.topology.world_size,
                    "placementCount": len(placements),
                },
            )
        )

    by_kind = {component.kind: component for component in manifest.components}

    for component in manifest.components:
        if component.coverage.world_size != manifest.topology.world_size:
            reasons.append(
                ResumeReason(
                    code=RESUME_TOPOLOGY_INCOMPATIBLE,
                    detail=(
                        f"component {component.kind} expects world size "
                        f"{component.coverage.world_size}, manifest declares "
                        f"{manifest.topology.world_size}"
                    ),
                    details={
                        "kind": component.kind,
                        "componentWorldSize": component.coverage.world_size,
                        "manifestWorldSize": manifest.topology.world_size,
                    },
                )
            )
        if not component.coverage.is_full():
            reasons.append(
                ResumeReason(
                    code=RESUME_RANK_COVERAGE_INCOMPLETE,
                    detail=(
                        f"component {component.kind} covers ranks "
                        f"{list(component.coverage.ranks)} of "
                        f"{component.coverage.world_size}"
                    ),
                    details={
                        "kind": component.kind,
                        "missingRanks": list(component.coverage.missing_ranks()),
                    },
                )
            )
        if component.present and component.digest is None:
            reasons.append(
                ResumeReason(
                    code=RESUME_COMPONENT_INVALID,
                    detail=f"component {component.kind} is present without a digest",
                    details={"kind": component.kind},
                )
            )
        if not component.present and component.digest is not None:
            reasons.append(
                ResumeReason(
                    code=RESUME_COMPONENT_INVALID,
                    detail=f"component {component.kind} is absent but declares a digest",
                    details={"kind": component.kind},
                )
            )
        expected_required = component.kind in RESUME_REQUIRED_COMPONENTS
        if component.kind in RESUME_KNOWN_COMPONENTS:
            if component.required != expected_required:
                reasons.append(
                    ResumeReason(
                        code=RESUME_COMPONENT_INVALID,
                        detail=(
                            f"component {component.kind} declares required="
                            f"{component.required}, core closure expects "
                            f"{expected_required}"
                        ),
                        details={
                            "kind": component.kind,
                            "declared": component.required,
                            "expected": expected_required,
                        },
                    )
                )
        elif component.required:
            reasons.append(
                ResumeReason(
                    code=RESUME_COMPONENT_INVALID,
                    detail=(
                        f"provider-defined kind {component.kind} cannot be "
                        "required by the core closure"
                    ),
                    details={"kind": component.kind},
                )
            )

    for kind in RESUME_REQUIRED_COMPONENTS:
        component = by_kind.get(kind)
        if component is None:
            reasons.append(
                ResumeReason(
                    code=RESUME_COMPONENT_MISSING,
                    detail=f"required component not declared: {kind}",
                    details={"kind": kind},
                )
            )
        elif not component.present:
            reasons.append(
                ResumeReason(
                    code=RESUME_COMPONENT_MISSING,
                    detail=f"required component declared absent: {kind}",
                    details={"kind": kind},
                )
            )

    for kind in RESUME_OPTIONAL_COMPONENTS:
        if kind not in by_kind:
            reasons.append(
                ResumeReason(
                    code=RESUME_COMPONENT_MISSING,
                    detail=f"optional component not declared: {kind}",
                    details={"kind": kind},
                )
            )

    return reasons
