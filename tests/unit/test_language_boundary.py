"""Machine guard for the language boundary documented in DEVELOPMENT-MAP.md.

The boundary is normative: C/C++ is FFI-only glue for external libraries and
must live under native/ffi/. These checks keep the rule enforceable in CI
instead of relying on reviewers remembering it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

C_SOURCE_SUFFIXES = {".c", ".cc", ".cxx", ".cpp", ".h", ".hpp"}
FFI_HOME = Path("native") / "ffi"
RUST_CRATE = ROOT / "native" / "aevrel-fast"


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_no_c_or_cpp_sources_outside_ffi_home():
    offenders = [
        path
        for path in _tracked_files()
        if Path(path).suffix in C_SOURCE_SUFFIXES
        and not Path(path).is_relative_to(FFI_HOME)
    ]
    assert offenders == [], (
        "C/C++ is FFI-only and must live under native/ffi/: "
        + ", ".join(sorted(offenders))
    )


def test_rust_hot_path_crate_exists():
    assert (RUST_CRATE / "Cargo.toml").is_file(), (
        "the native hot path crate native/aevrel-fast must stay in the tree"
    )


def test_development_map_documents_the_boundary():
    text = (ROOT / "docs" / "DEVELOPMENT-MAP.md").read_text(encoding="utf-8")
    assert "Language boundaries" in text
    assert "native/ffi/" in text
