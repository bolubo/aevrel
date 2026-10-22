"""Compatibility shim for the old flat planner module."""

from .planning.planner import (
    PlannerError,
    _legacy_migration_id,
    plan,
    validate_plan,
)

__all__ = [
    "PlannerError",
    "_legacy_migration_id",
    "plan",
    "validate_plan",
]
