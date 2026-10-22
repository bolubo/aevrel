"""Compatibility shim for the old flat target-ready module."""

from .verification.target_ready import (
    CommandTargetReady,
    DcpTargetReady,
    HttpTargetReady,
    TargetReadyChain,
    TargetReadyError,
)
from .verification.target_report import (
    TargetReadyReport,
    TargetReportError,
    load_target_ready_report,
    write_target_ready_report,
)

__all__ = [
    "CommandTargetReady",
    "DcpTargetReady",
    "HttpTargetReady",
    "TargetReadyChain",
    "TargetReadyError",
    "TargetReadyReport",
    "TargetReportError",
    "load_target_ready_report",
    "write_target_ready_report",
]
