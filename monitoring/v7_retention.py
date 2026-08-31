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
import re
import shutil
import time
from pathlib import Path
from typing import Any


SHA40 = re.compile(r"^[0-9a-f]{40}$")
DERIVED_CUTOVER_FILES = (
    "universe/current.json",
    "graph_rv/relation_registry.json",
    "reports/v7_arb_coverage_report.json",
    "control/fee_reward_registry.json",
    "micro_taker/state.json",
    "micro_taker/state_dataset_v1_degenerate_repeated_snapshot.json",
)


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


def rotate_append_reopen_streams(
    run_root: Path, active_policy: dict[str, Any], *, now: int, dry_run: bool
) -> list[str]:
    """Rotate only explicitly declared streams whose writers reopen on each append."""
    root = run_root.resolve()
    maximum = max(1, int(active_policy.get("append_reopen_max_bytes") or 1))
    protected = {str(value) for value in active_policy.get("never_copytruncate", [])}
    rotated: list[str] = []
    for raw in active_policy.get("append_reopen_streams", []):
        relative = Path(str(raw))
        if relative.is_absolute() or ".." in relative.parts or str(relative) in protected:
            raise ValueError(f"unsafe append-reopen rotation target:{raw}")
        active = (root / relative).resolve()
        try:
            active.relative_to(root)
            size = active.stat().st_size
        except (OSError, ValueError):
            continue
        if not active.is_file() or size < maximum:
            continue
        target = active.with_name(f"{active.name}.{now}")
        if target.exists():
            raise ValueError(f"rotation target already exists:{target}")
        rotated.append(str(target.relative_to(root)))
        if not dry_run:
            os.replace(active, target)
    return rotated


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


def _safe_cutover_archive(path: Path) -> bool:
    runtime = _json(path / "control" / "runtime_status.json")
    return (
        path.is_dir()
        and path.name.startswith("cutover-")
        and runtime.get("paper_only") is True
        and runtime.get("authenticated_execution") is False
        and runtime.get("real_order_submission") is False
        and SHA40.fullmatch(str(runtime.get("model_sha") or "")) is not None
    )


def _gzip_digest(path: Path) -> tuple[str, int]:
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


def _compact_cutover_ledger(archive: Path, *, dry_run: bool) -> dict[str, Any]:
    source = archive / "ledger" / "execution.jsonl"
    if not source.is_file():
        return {"created": False, "reason": "source_absent"}
    digest = hashlib.sha256()
    rows = 0
    source_bytes = 0
    with source.open("rb") as handle:
        for raw in handle:
            source_bytes += len(raw)
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise ValueError(f"cutover ledger has incomplete tail:{archive.name}")
            if not raw.strip():
                continue
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or value.get("paper_only") is not True
                or value.get("authenticated_execution") is not False
                or SHA40.fullmatch(str(value.get("model_sha") or "")) is None
            ):
                raise ValueError(f"unsafe cutover ledger record:{archive.name}:{rows + 1}")
            rows += 1
    if source_bytes <= 0:
        return {"created": False, "reason": "empty_source", "rows": rows}
    source_digest = digest.hexdigest()
    relative = Path("archive") / "canonical-ledger" / (
        f"execution-{source_digest[:20]}-{source_bytes}.jsonl.gz"
    )
    target = archive / relative
    if dry_run:
        return {
            "created": False,
            "reason": "dry_run",
            "source": str(source.relative_to(archive)),
            "target": str(relative),
            "source_bytes": source_bytes,
            "rows": rows,
            "sha256": source_digest,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        with source.open("rb") as input_handle, temporary.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="execution.jsonl", mode="wb",
                fileobj=raw_output, mtime=0,
            ) as output:
                shutil.copyfileobj(input_handle, output, length=1024 * 1024)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        compressed_digest, decompressed_bytes = _gzip_digest(temporary)
        if compressed_digest != source_digest or decompressed_bytes != source_bytes:
            temporary.unlink(missing_ok=True)
            raise ValueError(f"cutover ledger gzip verification failed:{archive.name}")
        os.replace(temporary, target)
    compressed_digest, decompressed_bytes = _gzip_digest(target)
    if compressed_digest != source_digest or decompressed_bytes != source_bytes:
        raise ValueError(f"existing cutover ledger gzip mismatch:{archive.name}")
    compressed_bytes = target.stat().st_size
    source.unlink()
    return {
        "created": True,
        "source": str(source.relative_to(archive)),
        "target": str(relative),
        "source_bytes": source_bytes,
        "compressed_bytes": compressed_bytes,
        "reclaimed_bytes": max(0, source_bytes - compressed_bytes),
        "rows": rows,
        "sha256": source_digest,
    }


def compact_cutover_archives(
    archive_root: Path, policy: dict[str, Any], *, now: int, dry_run: bool,
) -> dict[str, Any]:
    """Compact only inactive, verified PAPER cutovers.

    Canonical ledgers remain byte-verifiable gzip evidence. Only derived
    snapshots, cumulative superseded Micro datasets and diagnostic logs are
    removed; the newest generations remain fully expanded for incident review.
    """
    keep_full = max(1, int(policy.get("keep_full_generations") or 3))
    if not archive_root.exists():
        return {"archive_root": str(archive_root), "compacted": [], "skipped": []}
    candidates = sorted(
        (path for path in archive_root.glob("cutover-*") if path.is_dir()),
        key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True,
    )
    protected = {path.resolve() for path in candidates[:keep_full]}
    compacted: list[dict[str, Any]] = []
    skipped: list[str] = []
    for archive in candidates[keep_full:]:
        if archive.resolve() in protected or not _safe_cutover_archive(archive):
            skipped.append(archive.name)
            continue
        archive_resolved = archive.resolve()
        ledger = _compact_cutover_ledger(archive, dry_run=dry_run)
        removed: list[dict[str, Any]] = []
        removal_candidates = [archive / relative for relative in DERIVED_CUTOVER_FILES]
        removal_candidates.extend(archive.glob("**/*.log"))
        removal_candidates.extend(archive.glob("**/*.log.*"))
        seen: set[Path] = set()
        for candidate in removal_candidates:
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(archive_resolved)
            except (OSError, ValueError):
                continue
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            size = resolved.stat().st_size
            removed.append({
                "path": str(resolved.relative_to(archive_resolved)), "bytes": size,
            })
            if not dry_run:
                resolved.unlink()
        reclaimed = int(ledger.get("reclaimed_bytes") or 0) + sum(
            int(row["bytes"]) for row in removed
        )
        entry = {
            "archive": archive.name,
            "ledger": ledger,
            "removed_derived_files": removed,
            "reclaimed_bytes": reclaimed,
        }
        if not dry_run:
            _atomic_json(archive / "archive" / "compaction_manifest.json", {
                "schema": "polymarket_v7_cutover_compaction_v1",
                "timestamp": now,
                "paper_only": True,
                "authenticated_execution": False,
                **entry,
            })
        compacted.append(entry)
    return {
        "archive_root": str(archive_root),
        "keep_full_generations": keep_full,
        "protected_full_archives": [path.name for path in candidates[:keep_full]],
        "compacted": compacted,
        "skipped": skipped,
        "reclaimed_bytes": sum(int(row["reclaimed_bytes"]) for row in compacted),
        "dry_run": dry_run,
    }


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
    checkpoint = checkpoint_ledger(run_root, config["canonical_ledger"], expected_sha) if not dry_run else {"created": False, "reason": "dry_run"}
    rotated = rotate_append_reopen_streams(
        run_root, config.get("active_files", {}), now=now, dry_run=dry_run
    )
    expired = expire_rotated_streams(run_root, config.get("streams", []), now=now, dry_run=dry_run)
    pruned = prune_checkpoints(
        run_root,
        config["canonical_ledger"],
        durable_archive_confirmed=durable_archive_confirmed,
        dry_run=dry_run,
    )
    cutover_policy = config.get("cutover_archives", {})
    archive_name = str(cutover_policy.get("directory_name") or "paper_v7_archives")
    if Path(archive_name).name != archive_name:
        raise ValueError("unsafe cutover archive directory name")
    cutover_compaction = compact_cutover_archives(
        run_root.parent / archive_name, cutover_policy, now=now, dry_run=dry_run,
    )
    disk = disk_state(run_root, config["disk"])
    result = {
        "schema": "polymarket_v7_retention_status_v1",
        "timestamp": now,
        "paper_only": True,
        "authenticated_execution": False,
        "expected_sha": expected_sha,
        "disk": disk,
        "ledger_checkpoint": checkpoint,
        "rotated_append_reopen_streams": rotated,
        "expired_rotated_segments": expired,
        "pruned_ledger_checkpoints": pruned,
        "cutover_archive_compaction": cutover_compaction,
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
