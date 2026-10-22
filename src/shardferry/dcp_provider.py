"""Compatibility shim for the old flat DCP provider module."""

from .dcp.provider import DcpRangeSpecBuilder, DcpRangeSpecError

__all__ = ["DcpRangeSpecBuilder", "DcpRangeSpecError"]
