"""Test-only target command that writes the real target-ready contract."""

from __future__ import annotations

import os

from aevrel.state import build_manifest
from aevrel.verification.target_report import (
    ResumeStateEvidence,
    write_target_ready_report,
)


# This is a test double: it claims the full required component set so the
# drill layer can exercise the L4 path end to end. It restores nothing.
_DIGEST = "sha256:" + "0" * 64
_MANIFEST = build_manifest(
    checkpoint_uid=os.environ["AEVREL_MIGRATION_UID"],
    version=os.environ["AEVREL_VERSION"],
    metadata_digest=_DIGEST,
    world_size=1,
    present={
        "model": "lrs.training",
        "optimizer": "lrs.training",
        "global_step": "lrs.metadata",
        "dataloader": "lrs.training",
        "rng": "lrs.rng",
    },
    digests={
        "model": _DIGEST,
        "optimizer": _DIGEST,
        "global_step": _DIGEST,
        "dataloader": _DIGEST,
        "rng": _DIGEST,
    },
)


write_target_ready_report(
    model_loaded=True,
    optimizer_loaded=True,
    steps_ran=int(os.environ["AEVREL_VALIDATION_STEPS"]),
    migration_uid=os.environ["AEVREL_MIGRATION_UID"],
    version=os.environ["AEVREL_VERSION"],
    rank=int(os.environ["AEVREL_RANK"]),
    resume_state=ResumeStateEvidence.from_manifest(_MANIFEST),
    path=os.environ["AEVREL_REPORT_PATH"],
)
