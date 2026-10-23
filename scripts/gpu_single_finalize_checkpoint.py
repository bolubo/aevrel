from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

from aevrel.cdp import VersionMarker, write_marker


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--parent", default="")
    parser.add_argument("--from-dcp", default="")
    parser.add_argument("--version-id", required=True)
    parser.add_argument("--job-uid", default="gpu-single")
    args = parser.parse_args()

    root = Path(args.source_root)
    target = root / args.version_id
    if bool(args.parent) == bool(args.from_dcp):
        raise SystemExit("set exactly one of --parent or --from-dcp")
    if args.parent:
        parent = root / args.parent
        if not (parent / ".metadata").is_file():
            raise SystemExit(f"missing parent DCP metadata: {parent}")
        source = parent
        parent_id = args.parent
    else:
        source = Path(args.from_dcp)
        if not (source / ".metadata").is_file():
            raise SystemExit(f"missing DCP metadata: {source}")
        parent_id = None
    if target.exists():
        raise SystemExit(f"version already exists: {target}")
    shutil.copytree(source, target)
    marker = VersionMarker(
        version="1",
        version_id=args.version_id,
        job_uid=args.job_uid,
        step=int(args.version_id.removeprefix("step-")),
        parent_id=parent_id,
        metadata_hash=sha256_file(target / ".metadata"),
        created_at="2026-10-22T00:00:00Z",
        complete=True,
    )
    path = write_marker(target, marker)
    print(path)


if __name__ == "__main__":
    main()
