"""Compatibility shim for the old flat DCP range module."""

from .dcp.range import DcpRangeError, ZipTensorRange, inspect_zip_tensor

__all__ = ["DcpRangeError", "ZipTensorRange", "inspect_zip_tensor"]
