#!/usr/bin/env python3
"""Store V7 report bytes immutably under artifacts/by_sha/<sha>/<run_id>."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
RUN = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactStoreError(ValueError):
    pass


def store(root: Path, source: Path, *, exact_code_sha: str, run_id: str, name: str) -> dict:
    if not SHA.fullmatch(exact_code_sha) or not RUN.fullmatch(run_id) or not name or "/" in name or "\\" in name:
        raise ArtifactStoreError("invalid_identity")
    data = Path(source).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    target = Path(root) / "artifacts" / "by_sha" / exact_code_sha / run_id / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != data:
        raise ArtifactStoreError("immutable_path_collision")
    if not target.exists(): target.write_bytes(data)
    return {"schema_version": 1, "exact_code_sha": exact_code_sha, "run_id": run_id,
            "name": name, "location": str(target.relative_to(root)), "sha256": digest,
            "historical_non_authoritative": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".")); parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--exact-code-sha", required=True); parser.add_argument("--run-id", required=True); parser.add_argument("--name", required=True)
    args = parser.parse_args(); print(json.dumps(store(args.root.resolve(), args.source, exact_code_sha=args.exact_code_sha, run_id=args.run_id, name=args.name), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
