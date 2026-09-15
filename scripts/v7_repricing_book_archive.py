#!/usr/bin/env python3
"""Non-destructive archive for dedicated repricing causal-book evidence.

This helper is intentionally weaker than the general retention worker: it may
create verified gzip copies of producer-sealed repricing JSONL segments, but it
never unlinks, truncates, renames, or modifies the source files. It has zero
trading authority and is safe to run before a later storage-compaction review.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

SEGMENT = re.compile(r"^.+\.segment-[0-9]{6,}\.jsonl$")


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def sha256_gzip_payload(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with gzip.open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def atomic_gzip_copy(source: Path, target: Path) -> dict[str, Any]:
    before = source.stat()
    source_sha, source_bytes = sha256_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    if target.exists():
        archived_sha, archived_bytes = sha256_gzip_payload(target)
        if (archived_sha, archived_bytes) != (source_sha, source_bytes):
            raise ValueError(f"existing archive mismatch:{target}")
    else:
        with source.open("rb") as input_handle, temporary.open("xb") as raw_output:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_output, mtime=0, compresslevel=1
            ) as output:
                shutil.copyfileobj(input_handle, output, length=1024 * 1024)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        if sha256_gzip_payload(temporary) != (source_sha, source_bytes):
            temporary.unlink(missing_ok=True)
            raise ValueError(f"gzip verification failed:{source}")
        os.replace(temporary, target)
    after = source.stat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or sha256_file(source) != (source_sha, source_bytes)
    ):
        raise ValueError(f"source changed during archive:{source}")
    return {
        "source": str(source),
        "archive": str(target),
        "source_sha256": source_sha,
        "source_bytes": source_bytes,
        "gzip_bytes": target.stat().st_size,
        "decompressed_sha256_verified": True,
        "source_preserved": source.exists(),
    }


def archive_segments(run_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    run_root = run_root.resolve()
    source_root = run_root / "research/repricing_book/book_observations"
    archive_root = run_root / "archive/repricing-book"
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if source_root.is_symlink() or archive_root.is_symlink():
        raise ValueError("unsafe repricing archive path")
    candidates = [
        path for path in sorted(source_root.glob("*.jsonl"))
        if path.is_file() and not path.is_symlink() and SEGMENT.fullmatch(path.name)
    ]
    for source in candidates:
        try:
            digest, size = sha256_file(source)
            target = archive_root / f"{digest[:20]}-{size}-{source.name}.gz"
            if dry_run:
                rows.append({
                    "source": str(source), "archive": str(target),
                    "source_sha256": digest, "source_bytes": size,
                    "source_preserved": True, "dry_run": True,
                })
            else:
                rows.append(atomic_gzip_copy(source, target))
        except (OSError, ValueError) as error:
            failures.append({"source": str(source), "reason": str(error)})
    usage = shutil.disk_usage(run_root)
    result = {
        "schema": "polymarket_v7_repricing_book_archive_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "source_root": str(source_root),
        "archive_root": str(archive_root),
        "segments_seen": len(candidates),
        "segments_verified": len(rows),
        "failures": failures,
        "archives": rows,
        "free_bytes": usage.free,
        "source_deletion_permitted": False,
        "dry_run": dry_run,
    }
    if not dry_run:
        manifest = archive_root / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest.with_name(f"{manifest.name}.tmp.{os.getpid()}")
        temporary.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, manifest)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = archive_segments(args.run_root, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
