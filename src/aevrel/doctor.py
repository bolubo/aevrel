from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import SCHEMA_DIR
from .dcp import DcpError, read_dcp_metadata
from .defaults import DEFAULT_MIGRATED_SUFFIX, DEFAULT_WORK_SUFFIX
from .planning.layout import DTYPE_BYTES
from .manifest import ManifestError, load_spec
from .storage.router import StorageRouter
from .targets import target_root_path


def environment_doctor() -> dict[str, Any]:
    return _result([
        _check_python(),
        _check_torch(),
    ])


def checkpoint_doctor(
    checkpoint: str | Path,
    *,
    output: str | Path | None = None,
    work_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    checkpoint_path = Path(checkpoint).resolve()
    checkpoint_check = _check_checkpoint(checkpoint_path)
    dcp_check = _check_torch()
    checks = [
        _check_python(),
        dcp_check,
        checkpoint_check,
    ]

    if checkpoint_check["ok"] and dcp_check["ok"]:
        try:
            metadata = read_dcp_metadata(checkpoint_path)
        except DcpError as exc:
            checks.append(_check("dcp-metadata", False, str(exc)))
        else:
            tensors = [
                value
                for value in metadata.state_dict_metadata.values()
                if hasattr(value, "chunks")
            ]
            chunks = sum(len(value.chunks) for value in tensors)
            checks.append(
                _check(
                    "dcp-metadata",
                    bool(tensors and chunks),
                    f"{len(tensors)} tensor(s), {chunks} chunk(s)",
                )
            )
            checks.append(_check_dcp_storage(checkpoint_path, metadata))
            checks.append(
                _check_disk_estimate(
                    checkpoint_path,
                    metadata,
                    output=(
                        Path(output).resolve()
                        if output is not None
                        else checkpoint_path.with_name(
                            checkpoint_path.name + DEFAULT_MIGRATED_SUFFIX
                        )
                    ),
                    work_dir=(
                        Path(work_dir).resolve()
                        if work_dir is not None
                        else checkpoint_path.with_name(
                            checkpoint_path.name + DEFAULT_WORK_SUFFIX
                        )
                    ),
                )
            )

    if not checkpoint_check["ok"]:
        return _result(checks)

    output_path = (
        Path(output).resolve()
        if output is not None
        else checkpoint_path.with_name(
            checkpoint_path.name + DEFAULT_MIGRATED_SUFFIX
        )
    )
    work_path = (
        Path(work_dir).resolve()
        if work_dir is not None
        else checkpoint_path.with_name(
            checkpoint_path.name + DEFAULT_WORK_SUFFIX
        )
    )
    checks.append(
        _check_output_path(
            checkpoint_path,
            output_path,
            work_path,
            force=force,
        )
    )
    checks.append(_check_writable_location("work-dir", work_path))
    return _result(checks)


def doctor(spec_path: str | Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        spec = load_spec(spec_path)
    except (ManifestError, OSError, ValueError) as exc:
        checks.append(_check("spec", False, str(exc)))
        return _result(checks)
    checks.append(_check("spec", True, "migration spec is valid"))

    checks.append(_check_contracts())
    checks.append(_check_source_shards(spec))
    checks.append(_check_target_root(spec))
    return _result(checks)


def _check_contracts() -> dict[str, Any]:
    try:
        for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
    except Exception as exc:
        return _check("contracts", False, str(exc))
    return _check("contracts", True, "all V1 contract schemas are valid")


def _check_source_shards(spec: dict[str, Any]) -> dict[str, Any]:
    storage = StorageRouter()
    missing: list[str] = []
    errors: list[str] = []
    for shard in spec["source"]["shards"]:
        uri = shard["uri"]
        try:
            if not storage.exists(uri):
                missing.append(uri)
        except Exception as exc:
            errors.append(f"{uri}: {exc}")
    if missing or errors:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing[:3]))
        if errors:
            details.append("errors: " + "; ".join(errors[:3]))
        return _check("source-shards", False, "; ".join(details))
    return _check(
        "source-shards",
        True,
        f"{len(spec['source']['shards'])} source shard URI(s) reachable",
    )


def _check_target_root(spec: dict[str, Any]) -> dict[str, Any]:
    root = target_root_path(spec)
    parent = root.parent
    if not parent.exists():
        return _check("target-parent", False, f"missing: {parent}")
    if not parent.is_dir():
        return _check("target-parent", False, f"not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        return _check("target-parent", False, f"not writable: {parent}")
    if root.exists():
        if not root.is_dir():
            return _check("target-root", False, f"not a directory: {root}")
        try:
            if any(root.iterdir()):
                return _check("target-root", False, f"not empty: {root}")
        except OSError as exc:
            return _check("target-root", False, str(exc))
        if not os.access(root, os.W_OK):
            return _check("target-root", False, f"not writable: {root}")
    return _check("target-root", True, f"ready: {root}")


def _check_python() -> dict[str, Any]:
    current = sys.version_info
    version = f"{current.major}.{current.minor}.{current.micro}"
    return _check(
        "python",
        current >= (3, 11),
        f"{version} ({sys.executable})",
        hint="install Python 3.11 or newer",
    )


def _check_torch() -> dict[str, Any]:
    try:
        import torch
    except ImportError:
        return _check(
            "dcp-runtime",
            False,
            "PyTorch is not installed",
            hint='install with: pip install "aevrel[dcp]"',
        )
    return _check("dcp-runtime", True, f"torch {torch.__version__}")


def _check_checkpoint(checkpoint: Path) -> dict[str, Any]:
    if not checkpoint.exists():
        return _check(
            "checkpoint",
            False,
            f"not found: {checkpoint}",
            hint="pass the directory that contains the DCP .metadata file",
        )
    if not checkpoint.is_dir():
        return _check("checkpoint", False, f"not a directory: {checkpoint}")
    if not os.access(checkpoint, os.R_OK | os.X_OK):
        return _check("checkpoint", False, f"not readable: {checkpoint}")
    return _check("checkpoint", True, str(checkpoint))


def _check_dcp_storage(checkpoint: Path, metadata: Any) -> dict[str, Any]:
    storage_data = getattr(metadata, "storage_data", None) or {}
    if not storage_data:
        return _check("dcp-storage", False, "metadata has no storage entries")
    missing: list[str] = []
    truncated: list[str] = []
    unreadable: list[str] = []
    for info in storage_data.values():
        relative = getattr(info, "relative_path", "")
        if not relative:
            continue
        path = checkpoint / relative
        if not path.is_file():
            missing.append(relative)
            continue
        required = int(getattr(info, "offset", 0)) + int(
            getattr(info, "length", 0)
        )
        try:
            size = path.stat().st_size
        except OSError as exc:
            unreadable.append(f"{relative}: {exc}")
            continue
        if size < required:
            truncated.append(relative)
    if missing or truncated or unreadable:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(set(missing))[:3]))
        if truncated:
            details.append(
                "truncated: " + ", ".join(sorted(set(truncated))[:3])
            )
        if unreadable:
            details.append("unreadable: " + "; ".join(unreadable[:3]))
        return _check("dcp-storage", False, "; ".join(details))
    return _check(
        "dcp-storage",
        True,
        f"{len(storage_data)} storage reference(s) readable",
    )


def _check_disk_estimate(
    checkpoint: Path,
    metadata: Any,
    *,
    output: Path,
    work_dir: Path,
) -> dict[str, Any]:
    tensors = [
        value
        for value in getattr(metadata, "state_dict_metadata", {}).values()
        if hasattr(value, "size")
    ]
    tensor_bytes = 0
    for tensor in tensors:
        dtype = getattr(tensor.properties, "dtype", None)
        dtype_name = getattr(dtype, "name", "") or str(dtype).rsplit(".", 1)[-1]
        item_bytes = DTYPE_BYTES.get(dtype_name)
        if item_bytes is None:
            return _check(
                "disk-estimate",
                False,
                f"unsupported dtype for disk estimate: {dtype_name}",
            )
        tensor_bytes += int(tensor.size.numel()) * item_bytes
    storage_files: dict[Path, int] = {}
    for info in (getattr(metadata, "storage_data", None) or {}).values():
        relative = getattr(info, "relative_path", "")
        if not relative:
            continue
        path = checkpoint / relative
        try:
            storage_files[path] = path.stat().st_size
        except OSError:
            continue
    source_bytes = sum(storage_files.values())
    work_required = tensor_bytes * 2
    output_required = tensor_bytes
    work_free = _free_bytes(work_dir)
    output_free = _free_bytes(output)
    if work_free < work_required:
        return _check(
            "disk-estimate",
            False,
            f"work needs { _human(work_required) }, free { _human(work_free) }",
            hint="free local disk or choose another --work-dir",
        )
    if output_free < output_required:
        return _check(
            "disk-estimate",
            False,
            f"output needs { _human(output_required) }, free { _human(output_free) }",
            hint="free local disk or choose another --output",
        )
    return _check(
        "disk-estimate",
        True,
        (
            f"source {_human(source_bytes)}, target {_human(tensor_bytes)}, "
            f"work-needs {_human(work_required)}; "
            f"free work/output {_human(work_free)}/{_human(output_free)}"
        ),
    )


def _free_bytes(path: Path) -> int:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return 0


def _human(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{value}B"


def _check_output_path(
    checkpoint: Path,
    output: Path,
    work: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    for path, label in ((output, "output"), (work, "work directory")):
        if (
            path == checkpoint
            or checkpoint in path.parents
            or path in checkpoint.parents
        ):
            return _check(
                "layout",
                False,
                f"{label} must not overlap the source checkpoint: {path}",
            )
    if output == work or output in work.parents or work in output.parents:
        return _check(
            "layout",
            False,
            f"output and work directory must be separate: {output} vs {work}",
        )
    if output.exists() and not output.is_dir():
        return _check("output", False, f"not a directory: {output}")
    if output.exists():
        try:
            non_empty = any(output.iterdir())
        except OSError as exc:
            return _check("output", False, str(exc))
        if non_empty and not force:
            return _check(
                "output",
                False,
                f"directory is not empty: {output}",
                hint="choose another --output or pass --force",
            )
    location = _check_writable_location("output", output)
    if not location["ok"]:
        return location
    return _check("output", True, str(output))


def _check_writable_location(name: str, path: Path) -> dict[str, Any]:
    if path.exists() and not path.is_dir():
        return _check(name, False, f"not a directory: {path}")
    if path.exists() and not os.access(path, os.W_OK):
        return _check(name, False, f"not writable: {path}")
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir():
        return _check(name, False, f"not a directory: {parent}")
    if not os.access(parent, os.W_OK):
        return _check(name, False, f"not writable: {parent}")
    return _check(name, True, str(path))


def _check(
    name: str,
    ok: bool,
    detail: str,
    *,
    hint: str | None = None,
) -> dict[str, Any]:
    result = {"name": name, "ok": ok, "detail": detail}
    if hint and not ok:
        result["hint"] = hint
    return result


def _result(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok" if all(check["ok"] for check in checks) else "failed",
        "checks": checks,
    }
