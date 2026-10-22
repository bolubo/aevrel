"""Compatibility shim for the old flat DCP local-spec module."""

from .dcp.local import build_local_range_spec

__all__ = ["build_local_range_spec"]
