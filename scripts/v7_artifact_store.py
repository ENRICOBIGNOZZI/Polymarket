#!/usr/bin/env python3
"""Store V7 report bytes immutably under artifacts/by_sha/<sha>/<run_id>."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
RUN = re.compile(r"^[A-Za-z0-9._-]+$")
NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class ArtifactStoreError(ValueError):
    pass


def _safe_component(value: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.fullmatch(value)) and value not in {".", ".."}


def _mkdir_without_symlinks(root: Path, relative: Path) -> Path:
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ArtifactStoreError("artifact_path_symlink")
        current.mkdir(exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise ArtifactStoreError("artifact_path_not_directory")
    return current


def store(root: Path, source: Path, *, exact_code_sha: str, run_id: str, name: str) -> dict:
    if not SHA.fullmatch(exact_code_sha) or not _safe_component(run_id, RUN) or not _safe_component(name, NAME):
        raise ArtifactStoreError("invalid_identity")
    root = Path(root).resolve()
    source = Path(source)
    if not root.is_dir() or not source.is_file() or source.is_symlink():
        raise ArtifactStoreError("source_or_root_invalid")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    parent = _mkdir_without_symlinks(root, Path("artifacts") / "by_sha" / exact_code_sha / run_id)
    target = parent / name
    if target.is_symlink():
        raise ArtifactStoreError("artifact_path_symlink")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o444)
    except FileExistsError:
        if not target.is_file() or target.is_symlink() or target.read_bytes() != data:
            raise ArtifactStoreError("immutable_path_collision")
    except OSError as exc:
        if target.is_symlink():
            raise ArtifactStoreError("artifact_path_symlink") from exc
        raise ArtifactStoreError("artifact_write_failed") from exc
    else:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                target.unlink(missing_ok=True)
            finally:
                raise
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
