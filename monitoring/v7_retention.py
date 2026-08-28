#!/usr/bin/env python3
"""Safe retention for V7 PAPER telemetry with immutable ledger checkpoints.

The active canonical ledger and causal CSV streams are never truncated.  The
tool checkpoints complete ledger bytes, verifies their PAPER/SHA contract,
compresses inactive diagnostic logs, and expires only already-rotated stream
segments.  Canonical checkpoints are pruned only after an operator supplies an
explicit durable-archive acknowledgement.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def disk_state(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    free_ratio = usage.free / usage.total if usage.total else 0.0
    minimum = int(policy.get("minimum_free_bytes") or 0)
    critical_ratio = float(policy.get("critical_free_ratio") or 0.10)
    warning_ratio = float(policy.get("warning_free_ratio") or 0.20)
    if usage.free < minimum or free_ratio <= critical_ratio:
        state = "critical"
    elif free_ratio <= warning_ratio:
        state = "warning"
    else:
        state = "ok"
    return {"state": state, "total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free, "free_ratio": free_ratio}


def _complete_ledger_bytes(path: Path, expected_sha: str) -> tuple[bytes, int]:
    if not path.is_file():
        return b"", 0
    with path.open("rb") as handle:
        size = path.stat().st_size
        raw = handle.read(size)
    boundary = raw.rfind(b"\n")
    raw = raw[: boundary + 1] if boundary >= 0 else b""
    rows = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("canonical ledger contains a non-object record")
        if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
            raise ValueError("canonical ledger contains a non-PAPER record")
        if value.get("model_sha") != expected_sha:
            raise ValueError("canonical ledger contains SHA drift")
        rows += 1
    return raw, rows


def checkpoint_ledger(run_root: Path, policy: dict[str, Any], expected_sha: str) -> dict[str, Any]:
    ledger = run_root / str(policy["path"])
    payload, rows = _complete_ledger_bytes(ledger, expected_sha)
    if not payload:
        return {"created": False, "rows": rows, "bytes": 0, "reason": "no_complete_records"}
    digest = hashlib.sha256(payload).hexdigest()
    archive = run_root / str(policy["archive_directory"])
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / f"execution-{digest[:20]}-{len(payload)}.jsonl.gz"
    if not target.exists():
        temporary = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        with temporary.open("wb") as raw_handle:
            with gzip.GzipFile(filename="execution.jsonl", mode="wb", fileobj=raw_handle, mtime=0) as compressed:
                compressed.write(payload)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        os.replace(temporary, target)
    manifest = archive / "manifest.json"
    current = _json(manifest)
    checkpoints = current.get("checkpoints") if isinstance(current.get("checkpoints"), list) else []
    entry = {"sha256": digest, "source_bytes": len(payload), "rows": rows, "model_sha": expected_sha, "file": target.name}
    checkpoints = [row for row in checkpoints if isinstance(row, dict) and row.get("sha256") != digest]
    checkpoints.append(entry)
    _atomic_json(
        manifest,
        {
            "schema": "polymarket_v7_ledger_archive_manifest_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "updated_at": int(time.time()),
            "checkpoints": checkpoints,
        },
    )
    return {"created": True, "rows": rows, "bytes": len(payload), "sha256": digest, "path": str(target)}


def expire_rotated_streams(run_root: Path, streams: list[Any], *, now: int, dry_run: bool) -> list[str]:
    removed: list[str] = []
    root = run_root.resolve()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        cutoff = now - int(stream.get("retention_days") or 0) * 86400
        for pattern in stream.get("patterns") if isinstance(stream.get("patterns"), list) else []:
            for candidate in root.glob(str(pattern)):
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(root)
                    modified = resolved.stat().st_mtime
                except (OSError, ValueError):
                    continue
                if not resolved.is_file() or modified >= cutoff:
                    continue
                removed.append(str(resolved.relative_to(root)))
                if not dry_run:
                    resolved.unlink()
    return sorted(set(removed))


def prune_checkpoints(run_root: Path, policy: dict[str, Any], *, durable_archive_confirmed: bool, dry_run: bool) -> list[str]:
    if not durable_archive_confirmed:
        return []
    archive = run_root / str(policy["archive_directory"])
    retain = max(1, int(policy.get("retain_local_checkpoints") or 1))
    candidates = sorted(archive.glob("execution-*.jsonl.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for path in candidates[retain:]:
        removed.append(str(path.relative_to(run_root)))
        if not dry_run:
            path.unlink()
    return removed


def run_retention(
    run_root: Path,
    config: dict[str, Any],
    expected_sha: str,
    *,
    dry_run: bool,
    durable_archive_confirmed: bool,
    now: int | None = None,
) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    run_root.mkdir(parents=True, exist_ok=True)
    if config.get("schema") != "polymarket_v7_data_retention_v1" or config.get("paper_only") is not True:
        raise ValueError("invalid V7 PAPER retention policy")
    disk = disk_state(run_root, config["disk"])
    checkpoint = checkpoint_ledger(run_root, config["canonical_ledger"], expected_sha) if not dry_run else {"created": False, "reason": "dry_run"}
    expired = expire_rotated_streams(run_root, config.get("streams", []), now=now, dry_run=dry_run)
    pruned = prune_checkpoints(
        run_root,
        config["canonical_ledger"],
        durable_archive_confirmed=durable_archive_confirmed,
        dry_run=dry_run,
    )
    result = {
        "schema": "polymarket_v7_retention_status_v1",
        "timestamp": now,
        "paper_only": True,
        "authenticated_execution": False,
        "expected_sha": expected_sha,
        "disk": disk,
        "ledger_checkpoint": checkpoint,
        "expired_rotated_segments": expired,
        "pruned_ledger_checkpoints": pruned,
        "durable_archive_confirmed": durable_archive_confirmed,
        "dry_run": dry_run,
    }
    if not dry_run:
        _atomic_json(run_root / "control" / "retention_status.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path("runs/paper_v7_live"))
    parser.add_argument("--config", type=Path, default=Path("config/v7_data_retention.json"))
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--durable-archive-confirmed", action="store_true")
    args = parser.parse_args()
    result = run_retention(
        args.run_root.resolve(),
        _json(args.config),
        args.expected_sha,
        dry_run=args.dry_run,
        durable_archive_confirmed=args.durable_archive_confirmed,
    )
    print(json.dumps(result, sort_keys=True))
    return 2 if result["disk"]["state"] == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(main())
