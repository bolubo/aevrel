"""Test-only target command that writes the real target-ready contract."""

from __future__ import annotations

import os

from shardferry.verification.target_report import write_target_ready_report


write_target_ready_report(
    model_loaded=True,
    optimizer_loaded=True,
    steps_ran=int(os.environ["SHARDFERRY_VALIDATION_STEPS"]),
    migration_uid=os.environ["SHARDFERRY_MIGRATION_UID"],
    version=os.environ["SHARDFERRY_VERSION"],
    rank=int(os.environ["SHARDFERRY_RANK"]),
    state_complete=True,
    path=os.environ["SHARDFERRY_REPORT_PATH"],
)
