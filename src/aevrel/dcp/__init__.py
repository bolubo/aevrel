"""DCP checkpoint support.

The package keeps the historical ``aevrel.dcp`` import surface while the
implementation is split by responsibility inside this directory.

Heavy checkpoint/export imports are lazy so importing the package from the
storage layer does not create an executor -> dcp -> executor cycle.
"""

from __future__ import annotations

import importlib

from .range import DcpRangeError, ZipTensorRange, inspect_zip_tensor

__all__ = [
    "DcpChunkRef",
    "DcpError",
    "DcpRangeError",
    "DcpRangeSpecBuilder",
    "DcpRangeSpecError",
    "ZipTensorRange",
    "build_local_range_spec",
    "export_dcp",
    "export_dcp_local",
    "inspect_dcp",
    "inspect_zip_tensor",
    "inventory_dcp",
    "inventory_dcp_descriptors",
    "local_dcp_ranks",
    "migrate_dcp",
    "read_dcp_chunk",
    "read_dcp_chunk_range",
    "read_dcp_metadata",
    "read_dcp_tensor_chunk",
]

_LAZY = {
    "DcpChunkRef": (".checkpoint", "DcpChunkRef"),
    "DcpError": (".checkpoint", "DcpError"),
    "export_dcp": (".checkpoint", "export_dcp"),
    "export_dcp_local": (".checkpoint", "export_dcp_local"),
    "inspect_dcp": (".checkpoint", "inspect_dcp"),
    "inventory_dcp": (".checkpoint", "inventory_dcp"),
    "inventory_dcp_descriptors": (".checkpoint", "inventory_dcp_descriptors"),
    "local_dcp_ranks": (".checkpoint", "local_dcp_ranks"),
    "migrate_dcp": (".checkpoint", "migrate_dcp"),
    "read_dcp_chunk": (".checkpoint", "read_dcp_chunk"),
    "read_dcp_chunk_range": (".checkpoint", "read_dcp_chunk_range"),
    "read_dcp_metadata": (".checkpoint", "read_dcp_metadata"),
    "read_dcp_tensor_chunk": (".checkpoint", "read_dcp_tensor_chunk"),
    "build_local_range_spec": (".local", "build_local_range_spec"),
    "DcpRangeSpecBuilder": (".provider", "DcpRangeSpecBuilder"),
    "DcpRangeSpecError": (".provider", "DcpRangeSpecError"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
