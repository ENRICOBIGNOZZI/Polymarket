#!/usr/bin/env python3
"""Runtime-safe lossless retention for producer-sealed PAPER tape segments.

This module deliberately contains no research, training, aggregation or durable
store dependency. It is safe to include in the minimal London runtime bundle.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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


def _open_regular_file_identities() -> set[tuple[int, int]] | None:
    """Take one fail-closed snapshot instead of spawning lsof per segment."""
    import stat
    import subprocess

    proc = Path("/proc")
    if proc.is_dir():
        identities: set[tuple[int, int]] = set()
        scanned = False
        for process in proc.iterdir():
            if not process.name.isdigit():
                continue
            descriptors = process / "fd"
            try:
                entries = list(descriptors.iterdir())
                scanned = True
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            for descriptor in entries:
                try:
                    info = descriptor.stat()
                except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
                    continue
                if stat.S_ISREG(info.st_mode):
                    identities.add((info.st_dev, info.st_ino))
        if scanned:
            return identities

    binary = shutil.which("lsof")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "-nP", "-a", "-u", str(os.getuid()), "-Fn"],
            text=True, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1):
        return None
    identities: set[tuple[int, int]] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n/"):
            continue
        try:
            info = Path(line[1:]).stat()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if stat.S_ISREG(info.st_mode):
            identities.add((info.st_dev, info.st_ino))
    return identities


def _tape_file_closed(path: Path, open_identities: set[tuple[int, int]] | None = None) -> bool:
    snapshot = _open_regular_file_identities() if open_identities is None else open_identities
    if snapshot is None:
        return False
    try:
        info = path.stat()
    except OSError:
        return False
    return (info.st_dev, info.st_ino) not in snapshot


def _tape_identity(path: Path):
    import stat
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("tape must be a single-link regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _shared_pack_aliases(store_root: Path | None, relevant_paths: set[str] | None = None) -> dict:
    """Recognize verified aliases without statting every historical pack."""
    import stat
    aliases = {}
    if store_root is None or store_root.is_symlink(): return aliases
    relevant = None if relevant_paths is None else {str(path) for path in relevant_paths}
    if relevant == set(): return aliases
    for manifest in (store_root / 'pack_manifests').glob('*.json'):
        try:
            if manifest.is_symlink(): continue
            raw = manifest.read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest.stem: continue
            value = json.loads(raw); sha = value.get('pack_sha256', '')
            if (value.get('schema') != 'polymarket_v7_lossless_shared_pack_v1'
                    or not re.fullmatch('[a-f0-9]{64}', sha)
                    or value.get('source_bytes_sha256_verified') is not True): continue
            declared = [str(Path(path).resolve()) for path in value.get('source_aliases', []) if isinstance(path,str)]
            matched = declared if relevant is None else [path for path in declared if path in relevant]
            if not matched: continue
            pack = store_root / 'packs' / sha[:2] / (sha + '.pack')
            info = pack.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222: continue
            if info.st_size != value.get('pack_bytes'): continue
            for path in matched:
                aliases[str(Path(path).resolve())] = (info.st_dev, info.st_ino, info.st_size)
        except (OSError, ValueError, TypeError): continue
    return aliases


def _closed_tape_locations(scope: Path) -> list[tuple[Path, str]]:
    """Return every producer-sealed tape directory, including per-asset feeds."""
    rows = [
        (scope / "external_fair/raw", "bin"),
        (scope / "external_fair/normalized_events", "bin"),
        (scope / "micro_maker/book_observations", "jsonl"),
        (scope / "research/repricing_book/book_observations", "jsonl"),
    ]
    assets = scope / "external_fair/assets"
    if assets.is_dir() and not assets.is_symlink():
        for asset in sorted(assets.iterdir()):
            if not asset.is_dir() or asset.is_symlink():
                continue
            rows.extend([
                (asset / "raw", "bin"),
                (asset / "normalized_events", "bin"),
            ])
    return rows


def compress_closed_cutover_tapes(archive_root: Path, *, now: int, dry_run: bool,
                                  minimum_age_seconds: int = 3600,
                                  active_minimum_age_seconds: int = 60,
                                  active_run_root: Path | None = None,
                                  permanent_store_root: Path | None = None) -> dict[str, Any]:
    # Inactive cutover tapes and producer-sealed segments in the active run.
    # Current files and ledgers are excluded. Byte preservation does not attest
    # economic validity; book JSONL follows the same verified-gzip contract.
    import contextlib
    import fcntl
    import subprocess
    result = {"archived": [], "skipped": [], "failures": [], "reclaimed_bytes": 0,
              "dry_run": dry_run, "active_tapes_rotated": False}
    if archive_root.is_symlink(): return result
    if not archive_root.is_dir():
        if active_run_root is None:
            return result
        if not dry_run:
            archive_root.mkdir(parents=True, exist_ok=True)
    root = archive_root.resolve(); lock_path = root / ".closed-tape-retention.lock"
    if lock_path.is_symlink(): raise ValueError("unsafe tape-retention lock")
    context = contextlib.nullcontext(None) if dry_run else lock_path.open("a")
    with context as lock:
        if lock is not None:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result["skipped"].append({"reason": "retention_already_running"}); return result
        scopes = [(archive, False) for archive in sorted(root.glob("cutover-*"))
                  if not archive.is_symlink() and _safe_cutover_archive(archive)]
        if active_run_root is not None:
            active = Path(active_run_root)
            runtime = _json(active / "control/runtime_status.json")
            if (active.is_symlink() or not active.is_dir() or runtime.get("paper_only") is not True
                    or runtime.get("authenticated_execution") is not False
                    or runtime.get("real_order_submission") is not False
                    or SHA40.fullmatch(str(runtime.get("model_sha") or "")) is None):
                raise ValueError("unsafe active segment scope")
            scopes.append((active.resolve(), True))
        relevant_aliases: set[str] = set()
        for archive, active_scope in scopes:
            for folder, suffix in _closed_tape_locations(archive):
                if folder.is_symlink() or folder.parent.is_symlink():
                    continue
                pattern = f"*.segment-*.{suffix}" if active_scope else f"*.{suffix}"
                for source in folder.glob(pattern):
                    if active_scope and not re.fullmatch(r".+\.segment-[0-9]{6,}\." + suffix, source.name):
                        continue
                    relevant_aliases.add(str(source.resolve()))
        shared_aliases = _shared_pack_aliases(permanent_store_root, relevant_aliases)
        result["shared_alias_candidates"] = len(relevant_aliases)
        result["verified_shared_aliases"] = len(shared_aliases)
        open_identities = _open_regular_file_identities()
        result["open_file_snapshot_verified"] = open_identities is not None
        result["open_regular_file_count"] = len(open_identities or ())
        if open_identities is None:
            result["failures"].append({"source": "*", "reason": "open_file_snapshot_unavailable"})
            return result
        for archive, active_scope in scopes:
            relative_root = archive if active_scope else root
            for folder, suffix in _closed_tape_locations(archive):
                if folder.is_symlink() or folder.parent.is_symlink():
                    continue
                pattern = f"*.segment-*.{suffix}" if active_scope else f"*.{suffix}"
                for source in sorted(folder.glob(pattern)):
                    if active_scope and not re.fullmatch(r".+\.segment-[0-9]{6,}\." + suffix, source.name):
                        continue
                    temporary = None
                    try:
                        info = source.lstat()
                        if shared_aliases.get(str(source.resolve())) == (info.st_dev, info.st_ino, info.st_size):
                            result['skipped'].append({'path': str(source), 'reason': 'VERIFIED_SHARED_IMMUTABLE_PACK'})
                            continue
                        before = _tape_identity(source)
                        minimum_age = active_minimum_age_seconds if active_scope else minimum_age_seconds
                        if now - source.stat().st_mtime < minimum_age:
                            result["skipped"].append({"path": str(source), "reason": "recent"}); continue
                        if not _tape_file_closed(source, open_identities):
                            result["skipped"].append({"path": str(source), "reason": "open_or_unverifiable"}); continue
                        target = source.with_name(source.name + ".gz")
                        if target.is_symlink(): raise ValueError("archive target is symlink")
                        if dry_run:
                            result["archived"].append({"source": str(source), "source_bytes": before[2]}); continue
                        if shutil.disk_usage(root).free < before[2] + 1024**3:
                            result["skipped"].append({"path": str(source), "reason": "compression_workspace_insufficient"}); continue
                        digest = hashlib.sha256(); size = 0
                        if not target.exists():
                            candidate = target.with_name(target.name + f".tmp.{os.getpid()}")
                            with candidate.open("xb") as raw_output:
                                temporary = candidate
                                with source.open("rb") as input_file, gzip.GzipFile(fileobj=raw_output, mode="wb", filename="", mtime=0, compresslevel=1) as compressed:
                                    while block := input_file.read(1024*1024):
                                        digest.update(block); size += len(block); compressed.write(block)
                                raw_output.flush(); os.fsync(raw_output.fileno())
                            if _gzip_digest(temporary) != (digest.hexdigest(), size): raise ValueError("tape gzip verification failed")
                            if _tape_identity(source) != before: raise ValueError("tape changed during compression")
                            os.link(temporary, target); temporary.unlink(); temporary = None
                            _sync_directory(target.parent)
                        else:
                            with source.open("rb") as handle:
                                while block := handle.read(1024*1024): digest.update(block); size += len(block)
                        if _gzip_digest(target) != (digest.hexdigest(), size) or size != before[2]: raise ValueError("existing tape archive mismatch")
                        if _tape_identity(source) != before or not _tape_file_closed(source, open_identities): raise ValueError("tape changed or opened before retirement")
                        manifest = archive / "lossless_compression_manifest.jsonl"
                        if manifest.is_symlink(): raise ValueError("unsafe tape manifest")
                        row = {"source": str(source.relative_to(relative_root)), "archive": str(target.relative_to(relative_root)),
                               "scope": "ACTIVE_RUN_CLOSED_SEGMENT" if active_scope else "INACTIVE_CUTOVER",
                               "source_bytes": size, "gzip_bytes": target.stat().st_size,
                               "source_sha256": digest.hexdigest(), "decompressed_sha256_verified": True,
                               "economic_evidence_validity_assessed": False, "timestamp": now}
                        with manifest.open("a") as handle:
                            handle.write(json.dumps(row, sort_keys=True)+"\n"); handle.flush(); os.fsync(handle.fileno())
                        _sync_directory(archive)
                        if _tape_identity(source) != before: raise ValueError("tape changed before unlink")
                        source.unlink(); _sync_directory(source.parent)
                        row["source_removed_after_verification"] = True
                        result["archived"].append(row); result["reclaimed_bytes"] += size - row["gzip_bytes"]
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        result["failures"].append({"source": str(source), "reason": str(exc)})
                    finally:
                        if temporary is not None: temporary.unlink(missing_ok=True)
    return result

