from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_live_acceptance_dry_run(tmp_path: Path) -> None:
    migration = tmp_path / "migration.yaml"
    migration.write_text(
        "apiVersion: shardferry.io/v1\n"
        "kind: CheckpointMigration\n"
        "metadata:\n"
        "  name: test\n"
        "spec:\n"
        "  source:\n"
        "    clusterRef: source\n"
        "  target:\n"
        "    clusterRef: target\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/live_acceptance.py"),
            "--management-kubeconfig",
            "/tmp/management",
            "--migration",
            str(migration),
            "--dry-run",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text())
    assert report["status"] == "dry-run"
    assert report["source_cluster"] == "source"
    assert report["target_cluster"] == "target"
