"""Compatibility shim for the old flat target-ready module."""

from .verification.target_ready import (
    CommandTargetReady,
    DcpTargetReady,
    HttpTargetReady,
    TargetReadyChain,
    TargetReadyError,
)

__all__ = [
    "CommandTargetReady",
    "DcpTargetReady",
    "HttpTargetReady",
    "TargetReadyChain",
    "TargetReadyError",
]
