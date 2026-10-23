from __future__ import annotations

import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from shardferry.commands import support_bundle, target_load  # noqa: E402


def test_redact_text_removes_common_credentials() -> None:
    sample = (
        "token: abc123\n"
        "password = hunter2\n"
        "Authorization: Bearer secret\n"
        '{"api_key":"json-secret"}\n'
        "safe=ok\n"
    )
    redacted = support_bundle.redact_text(sample)
    assert "abc123" not in redacted
    assert "hunter2" not in redacted
    assert "json-secret" not in redacted
    assert "secret\n" not in redacted
    assert "safe=ok" in redacted


def test_support_bundle_creates_redacted_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"get pods -o json"*) echo \'{"items":[]}\' ;;\n'
        '  *"get secrets -o name"*) echo "secret/example" ;;\n'
        '  *"config current-context"*) echo "management" ;;\n'
        '  *) echo "token: super-secret";;\n'
        "esac\n",
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    helm = bin_dir / "helm"
    helm.write_text("#!/bin/sh\necho 'NAME\tNAMESPACE'\n", encoding="utf-8")
    helm.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    output = tmp_path / "support.tar.gz"
    assert (
        support_bundle.main(
            [
                "--output",
                str(output),
                "--no-logs",
            ]
        )
        == 0
    )
    assert output.is_file()
    with tarfile.open(output) as archive:
        contents = "\n".join(
            archive.extractfile(member).read().decode("utf-8", "replace")
            for member in archive.getmembers()
            if member.isfile() and member.name.endswith(".txt")
        )
    assert "super-secret" not in contents
    assert "<redacted>" in contents


def test_target_load_smoke_rejects_missing_metadata(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        target_load.main(
            [
                "--checkpoint",
                str(empty),
                "--json",
            ]
        )
        == 2
    )


def test_target_load_smoke_loads_generated_dcp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("torch")
    from shardferry.dcp import export_dcp
    from shardferry.executor import apply
    from shardferry.planning import plan
    from tests.fixtures.generated import (
        generate_sharded_spec,
        materialize_source_files,
    )

    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (2, 2),
        ("Shard(0)", "Shard(1)"),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path / "work")
    apply(plan(spec), spec)
    checkpoint = tmp_path / "target.dcp"
    export_dcp(spec, checkpoint)

    assert (
        target_load.main(
            [
                "--checkpoint",
                str(checkpoint),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["tensors"] == 1
    assert payload["mode"] == "full"


def test_target_load_smoke_loads_rank_local_dcp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("torch")
    from shardferry.dcp import export_dcp_local
    from shardferry.executor import apply
    from shardferry.planning import plan
    from tests.fixtures.generated import (
        generate_sharded_spec,
        materialize_source_files,
    )

    spec, arrays, _ = generate_sharded_spec(
        (8, 4),
        (4,),
        ("Shard(0)",),
        (2,),
        ("Shard(0)",),
    )
    materialize_source_files(spec, arrays, tmp_path)
    apply(plan(spec), spec)
    local = tmp_path / "rank0.dcp"
    export_dcp_local(
        spec,
        local,
        raw_root=tmp_path / "target",
        ranks=(0,),
    )

    assert (
        target_load.main(
            [
                "--checkpoint",
                str(local),
                "--metadata-dir",
                str(local),
                "--rank",
                "0",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["mode"] == "rank-local"
