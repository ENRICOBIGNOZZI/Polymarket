#!/usr/bin/env python3
"""Bound the London PAPER evidence buffer without deleting unverified bytes.

Closed high-volume segments are first compressed losslessly and verified by
round-trip SHA-256. Compressed detail older than the explicitly authorized raw
window may be retired only after an immutable receipt records both compressed
and decompressed identities. Exact-hash research-plane receipts remain a second,
stronger deletion path. Canonical ledgers and active files are never candidates.
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v7_closed_tape_retention import compress_closed_cutover_tapes, _safe_cutover_archive
from v7_hft_windows import Windows
from v7_hft_data_health import snapshot, storage_projection


SHA40 = re.compile(r"^[0-9a-f]{40}$")
ROLLING_AUTHORIZATION = "USER_AUTHORIZED_HFT_ROLLING_RAW_WINDOW_20260916"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _canonical(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _immutable_receipt(directory: Path, value: dict[str, Any]) -> tuple[Path, str]:
    payload = _canonical(value)
    digest = hashlib.sha256(payload).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    if target.exists():
        if target.is_symlink() or target.read_bytes() != payload:
            raise ValueError(f"immutable receipt collision:{target}")
        return target, digest
    temporary = directory / f".{digest}.tmp.{os.getpid()}"
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, target)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != payload:
            raise ValueError(f"immutable receipt collision:{target}")
    finally:
        temporary.unlink(missing_ok=True)
    _fsync_dir(directory)
    return target, digest


def _safe_relative(root: Path, raw: str) -> Path:
    if not raw:
        raise ValueError("empty relative path")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path:{raw}")
    target = root / relative
    target.resolve(strict=False).relative_to(root.resolve())
    return target


def _matches(relative: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"managed evidence must be a single-link regular file:{path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(path: Path) -> str:
    output = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            output.update(block)
    return output.hexdigest()


def _gzip_identity(path: Path) -> tuple[str, int, str]:
    compressed_sha = _digest(path)
    raw_sha = hashlib.sha256()
    raw_bytes = 0
    with gzip.open(path, "rb") as handle:
        while True:
            block = handle.read(4 * 1024 * 1024)
            if not block:
                break
            raw_sha.update(block)
            raw_bytes += len(block)
    return raw_sha.hexdigest(), raw_bytes, compressed_sha


def _managed(root: Path, config: dict[str, Any]) -> list[tuple[Path, str, os.stat_result]]:
    patterns = [str(value) for value in (config.get("managed_patterns") or config.get("closed_segment_patterns") or [])]
    never = {str(value) for value in config.get("never_delete") or []}
    rows: list[tuple[Path, str, os.stat_result]] = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            relative = str(path.relative_to(root))
            if relative in never or not _matches(relative, patterns):
                continue
            rows.append((path, relative, path.stat()))
        except FileNotFoundError:
            continue
    return rows


def _total(rows: Iterable[tuple[Path, str, os.stat_result]]) -> int:
    return sum(int(info.st_size) for _, _, info in rows)


def _runtime_contract(root: Path) -> dict[str, Any]:
    value = _load(root / "control/runtime_status.json")
    if not value:
        return {}
    if (
        value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or SHA40.fullmatch(str(value.get("model_sha") or "")) is None
    ):
        raise ValueError("unsafe London runtime contract")
    return value


def _rolling_retire(
    root: Path,
    config: dict[str, Any],
    runtime: dict[str, Any],
    *,
    now_ns: int,
    dry_run: bool,
    window_root: Path | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "policy": config.get("rolling_retention_authorization"),
        "raw_detail_seconds": int(config.get("rolling_raw_detail_seconds") or 0),
        "eligible": 0,
        "retired": [],
        "pinned": [],
        "failures": [],
        "reclaimed_bytes": 0,
        "dry_run": dry_run,
    }
    if not runtime:
        result["state"] = "NO_VERIFIED_RUNTIME_CONTRACT"
        return result
    if config.get("rolling_retention_authorization") != ROLLING_AUTHORIZATION:
        result["state"] = "NOT_AUTHORIZED"
        return result
    window = int(config.get("rolling_raw_detail_seconds") or 0)
    if window < 7200 or window > 7 * 86400:
        raise ValueError("unsafe rolling raw-detail window")
    receipt_directory = _safe_relative(root, str(config.get("rolling_receipt_directory") or ""))
    cutoff = now_ns - window * 1_000_000_000
    windows = Windows(window_root or root, config.get('research_epoch_start_wall_ns',0)) if config.get('require_hft_window_preservation') and not dry_run else None
    for path, relative, info in sorted(_managed(root, config), key=lambda row: (row[2].st_mtime_ns, row[1])):
        if relative.startswith('research/hft_permanent/'):
            continue
        if not relative.endswith(".gz") or ".open" in path.suffixes or info.st_mtime_ns >= cutoff:
            continue
        result["eligible"] += 1
        try:
            before = _identity(path)
            raw_sha, raw_bytes, compressed_sha = _gzip_identity(path)
            if _identity(path) != before:
                raise ValueError("compressed evidence changed while hashing")
            hft_source = any(part in relative for part in ('/raw/','/normalized_events/','/book_observations/','native_observations/'))
            if hft_source and dry_run and config.get('require_hft_window_preservation'):
                raise ValueError('HFT_PRESERVATION_NOT_VERIFIED_IN_DRY_RUN')
            preservation = windows.preserve(path) if windows and hft_source else None
            receipt = {
                "schema": "polymarket_v7_london_windowed_retirement_receipt_v1",
                "paper_only": True,
                "authenticated_execution": False,
                "real_order_submission": False,
                "execution_authority": False,
                "policy": ROLLING_AUTHORIZATION,
                "model_sha": runtime["model_sha"],
                "source": relative,
                "compressed_sha256": compressed_sha,
                "compressed_bytes": before[2],
                "decompressed_sha256": raw_sha,
                "decompressed_bytes": raw_bytes,
                "source_mtime_ns": before[3],
                "retired_at_ns": now_ns,
                "raw_detail_available": False,
                "hft_window_preservation": preservation,
                "limitations": [
                    "Replayable raw detail was retired after the authorized rolling window.",
                    "The receipt preserves exact compressed and decompressed identities, not observations.",
                ],
            }
            if dry_run:
                result["retired"].append({**receipt, "receipt": None})
                continue
            receipt_path, receipt_sha = _immutable_receipt(receipt_directory, receipt)
            if _identity(path) != before:
                raise ValueError("compressed evidence changed after receipt persistence")
            path.unlink()
            _fsync_dir(path.parent)
            result["reclaimed_bytes"] += before[2]
            result["retired"].append({
                **receipt,
                "receipt": str(receipt_path.relative_to(root)),
                "receipt_sha256": receipt_sha,
            })
        except (OSError, EOFError, gzip.BadGzipFile, ValueError) as exc:
            reason = str(exc)
            row = {"source": relative, "reason": reason}
            # A closed capture explicitly marked unhealthy is intentionally
            # non-retirable. Keeping it pinned is the safety action, not a
            # retention malfunction. Storage-limit enforcement below remains
            # fail-closed if pinned evidence grows too large.
            if reason == "UNHEALTHY_NATIVE_CAPTURE_PINNED":
                result["pinned"].append(row)
            else:
                result["failures"].append(row)
    if windows: windows.close()
    result["state"] = "WINDOW_ENFORCED" if not result["failures"] else "WINDOW_PARTIAL_FAILURE"
    return result


def _offload_prune(
    root: Path,
    config: dict[str, Any],
    rows: list[tuple[Path, str, os.stat_result]],
    *,
    now_ns: int,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], bool]:
    receipt_path = _safe_relative(root, str(config["offload_receipt"]))
    receipt = _load(receipt_path)
    if receipt.get("schema") != "polymarket_v7_research_offload_receipt_v1":
        return [], False
    lag = int(config["offload_safety_lag_seconds"])
    minimum_age = int(config["minimum_age_seconds"])
    through = int(receipt.get("synced_through_ns") or 0) - lag * 1_000_000_000
    indexed = {
        str(row.get("path")): row
        for row in receipt.get("files") or []
        if isinstance(row, dict) and row.get("path")
    }
    candidates: list[tuple[int, Path, str, os.stat_result]] = []
    for path, relative, info in rows:
        if relative.startswith('research/hft_permanent/'):
            continue
        if ".open" in path.suffixes or (relative.endswith(".bin") and ".segment-" not in path.name):
            continue
        metadata = indexed.get(relative)
        if (
            metadata is None
            or info.st_mtime_ns > through
            or now_ns - info.st_mtime_ns < minimum_age * 1_000_000_000
            or int(metadata.get("size") or -1) != info.st_size
        ):
            continue
        if str(metadata.get("sha256") or "") != _digest(path):
            continue
        candidates.append((info.st_mtime_ns, path, relative, info))
    deleted: list[dict[str, Any]] = []
    total = _total(rows)
    target = int(config["target_managed_bytes"])
    for _, path, relative, info in sorted(candidates):
        if total <= target:
            break
        before = _identity(path)
        if dry_run:
            deleted.append({"path": relative, "bytes": info.st_size, "dry_run": True})
        else:
            if _identity(path) != before:
                continue
            path.unlink()
            _fsync_dir(path.parent)
            deleted.append({"path": relative, "bytes": info.st_size})
        total -= info.st_size
    return deleted, True


def run(root: Path, config: dict[str, Any], dry_run: bool = False, *, now: float | None = None) -> dict[str, Any]:
    root = root.resolve()
    now = time.time() if now is None else float(now)
    now_ns = int(now * 1_000_000_000)
    if config.get("schema") != "polymarket_v7_london_buffer_retention_v1" or config.get("paper_only") is not True:
        raise ValueError("invalid London retention policy")
    target = int(config["target_managed_bytes"])
    maximum = int(config["maximum_managed_bytes"])
    minimum_age = int(config["minimum_age_seconds"])
    lag = int(config["offload_safety_lag_seconds"])
    compression_age = int(config.get("lossless_compress_minimum_age_seconds") or 60)
    if not 0 < target < maximum or minimum_age < 300 or lag < 60 or not 30 <= compression_age <= 3600:
        raise ValueError("unsafe London retention bounds")
    runtime = _runtime_contract(root)
    archive_root=root.parent/'paper_v7_london_archives'
    archives=sorted(p for p in archive_root.glob('cutover-*') if _safe_cutover_archive(p)) if config.get('account_sibling_run_archives') else []
    storage_root=root.parent if config.get('account_sibling_run_archives') else root
    storage = None
    if config.get('account_all_run_files'):
        measured=snapshot(storage_root)
        previous=_load(root/'control/hft_storage_sample.json')
        if previous and measured['at_ns'] > previous.get('at_ns',0):
            storage=storage_projection(root,previous,measured,target=int(config['target_managed_bytes']))
            hours=storage.get('estimated_compressed_raw_hours')
            if hours:
                config=dict(config)
                config['rolling_raw_detail_seconds']=max(7200,min(7*86400,int(hours*3600*.8)))
            storage['raw_retention_hours']=config['rolling_raw_detail_seconds']/3600
        if not dry_run: _atomic_json(root/'control/hft_storage_sample.json',measured)
    preservation = None
    if runtime and config.get('require_hft_window_preservation') and not dry_run:
        windows = Windows(root, config.get('research_epoch_start_wall_ns',0))
        try:
            preservation = windows.ingest(
                max_rows=int(config.get('hft_active_ingest_max_rows',10_000)),
                archive_max_rows=int(config.get('hft_archive_ingest_max_rows',50_000)),
                budget_seconds=int(config.get('hft_ingest_budget_seconds',45)),
            )
        finally: windows.close()
    initial_rows = _managed(root, config)
    before = measured['total_bytes'] if config.get('account_all_run_files') else _total(initial_rows)
    compression: dict[str, Any] = {
        "state": "NO_VERIFIED_RUNTIME_CONTRACT",
        "archived": [],
        "failures": [],
        "reclaimed_bytes": 0,
    }
    if runtime:
        compression = compress_closed_cutover_tapes(
            archive_root if config.get('account_sibling_run_archives') else root / "control/retention_archive_scope",
            now=int(now),
            dry_run=dry_run,
            minimum_age_seconds=minimum_age,
            active_minimum_age_seconds=compression_age,
            active_run_root=root,
        )
        compression["state"] = "OK" if not compression.get("failures") else "PARTIAL_FAILURE"
    after_compression_rows = _managed(root, config)
    after_compression = _total(after_compression_rows)
    rolling = _rolling_retire(root, config, runtime, now_ns=now_ns, dry_run=dry_run)
    for archive in archives:
        retired=_rolling_retire(archive,config,_runtime_contract(archive),now_ns=now_ns,
                                dry_run=dry_run,window_root=root)
        for key in ('eligible','reclaimed_bytes'): rolling[key]=rolling.get(key,0)+retired.get(key,0)
        for key in ('retired','failures'): rolling[key].extend(retired.get(key,[]))
    after_rolling_rows = _managed(root, config)
    deleted, receipt_valid = _offload_prune(
        root, config, after_rolling_rows, now_ns=now_ns, dry_run=dry_run,
    )
    final_rows = _managed(root, config)
    after = snapshot(storage_root)['total_bytes'] if config.get('account_all_run_files') else _total(final_rows)
    if dry_run:
        after = max(0, after - sum(int(row.get("bytes") or row.get("compressed_bytes") or 0)
                                   for row in deleted + list(rolling.get("retired") or [])))
    if after > maximum:
        state = "BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED"
    elif compression.get("failures") or rolling.get("failures"):
        state = "RETENTION_PARTIAL_FAILURE"
    elif receipt_valid:
        state = "OK"
    elif rolling.get("retired"):
        state = "ROLLING_WINDOW_ENFORCED"
    else:
        state = "NO_VERIFIED_OFFLOAD"
    return {
        "schema": "polymarket_v7_london_buffer_retention_status_v1",
        "version": 2,
        "timestamp": int(now),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "state": state,
        "before_bytes": before,
        "after_compression_bytes": after_compression,
        "after_bytes": after,
        "target_bytes": target,
        "maximum_bytes": maximum,
        "managed_files": len(final_rows),
        "lossless_compression": compression,
        "hft_opportunity_preservation": preservation,
        "hft_storage": storage,
        "rolling_retirement": rolling,
        "verified_offload_receipt": receipt_valid,
        "deleted": deleted,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    value = run(args.run_root, _load(args.config), args.dry_run)
    _atomic_json(args.run_root / "control/london_buffer_retention_status.json", value)
    print(json.dumps(value, sort_keys=True))
    return 2 if value["state"] in {
        "BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED",
        "RETENTION_PARTIAL_FAILURE",
    } else 0


if __name__ == "__main__":
    raise SystemExit(main())
