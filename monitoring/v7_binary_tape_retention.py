#!/usr/bin/env python3
"""Verified retention for closed V7 binary market-data tape segments.

The recorder writes active segments with a ``.open`` suffix and atomically
publishes a complete ``.bin`` only after the writer has flushed and closed it.
This module never touches ``.open`` files. It validates the binary format and
exact model SHA, creates a deterministic gzip archive, verifies the decompressed
SHA-256 and byte count, commits an atomic manifest, and only then removes the
uncompressed segment.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable


TAPE_HEADER_BYTES = 232
RAW_RECORD_HEADER = struct.Struct("<QQqqB3xI")
NORMALIZED_MAGIC = b"PMV7TAPE"
RAW_MAGIC = b"PMV7RAW!"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
LEGACY_PID = re.compile(r"\.(?P<pid>[1-9][0-9]*)\.bin$")
SEGMENT_NAME = re.compile(
    r"\.(?P<pid>[1-9][0-9]*)\.segment-(?P<segment>[0-9]{6})\.bin$"
)


@dataclass(frozen=True)
class TapeValidation:
    magic: str
    schema_version: int
    model_sha: str
    run_id: str
    session_id: str
    source: str
    creation_wall_ns: int
    source_bytes: int
    records: int
    first_sequence: int
    last_sequence: int


def _cstring(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="strict")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> tuple[str, int]:
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


def _read_exact(handle: BinaryIO, size: int) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise ValueError(
            f"incomplete binary tape record: expected={size} actual={len(value)}"
        )
    return value


def validate_closed_tape(
    path: Path,
    expected_sha: str,
    *,
    allowed_schema_versions: Iterable[int] = (1, 2, 3),
    maximum_raw_payload_bytes: int = 2 * 1024 * 1024,
) -> TapeValidation:
    """Validate one immutable closed segment without trusting its file name."""
    if not SHA40.fullmatch(expected_sha):
        raise ValueError("expected SHA must be exact lowercase hexadecimal")
    if path.name.endswith(".open") or path.suffix != ".bin":
        raise ValueError("binary tape is not a closed .bin segment")
    if path.is_symlink() or not path.is_file():
        raise ValueError("binary tape must be a regular file")
    source_bytes = path.stat().st_size
    if source_bytes < TAPE_HEADER_BYTES:
        raise ValueError("binary tape is shorter than its session header")

    with path.open("rb") as handle:
        header = _read_exact(handle, TAPE_HEADER_BYTES)
        magic = header[0:8]
        schema_version = int.from_bytes(header[8:12], "little", signed=False)
        record_bytes = int.from_bytes(header[12:16], "little", signed=False)
        creation_wall_ns = int.from_bytes(header[16:24], "little", signed=True)
        model_sha = _cstring(header[24:65])
        run_id = _cstring(header[65:130])
        session_id = _cstring(header[130:195])
        source = _cstring(header[195:228])

        if magic not in {NORMALIZED_MAGIC, RAW_MAGIC}:
            raise ValueError("binary tape magic is not recognized")
        allowed = {int(value) for value in allowed_schema_versions}
        if schema_version not in allowed:
            raise ValueError(f"binary tape schema is not allowed:{schema_version}")
        if model_sha != expected_sha:
            raise ValueError("binary tape model SHA drift")
        if creation_wall_ns <= 0 or not run_id or not session_id or not source:
            raise ValueError("binary tape session identity is incomplete")

        records = 0
        first_sequence = 0
        last_sequence = 0
        if magic == NORMALIZED_MAGIC:
            if record_bytes <= 0:
                raise ValueError("normalized tape record size is invalid")
            payload_bytes = source_bytes - TAPE_HEADER_BYTES
            if payload_bytes % record_bytes != 0:
                raise ValueError("normalized tape has an incomplete final record")
            records = payload_bytes // record_bytes
            for _ in range(records):
                record = _read_exact(handle, record_bytes)
                sequence = int.from_bytes(record[0:8], "little", signed=False)
                receive_ns = int.from_bytes(record[8:16], "little", signed=True)
                payload_size = int.from_bytes(record[28:32], "little", signed=False)
                if sequence <= 0 or receive_ns <= 0:
                    raise ValueError("normalized tape record identity is invalid")
                if payload_size <= 0 or payload_size > record_bytes - 32:
                    raise ValueError("normalized tape payload size is invalid")
                if last_sequence and sequence <= last_sequence:
                    raise ValueError(
                        "normalized tape sequence is not strictly increasing"
                    )
                if not first_sequence:
                    first_sequence = sequence
                last_sequence = sequence
        else:
            if record_bytes != 0:
                raise ValueError("raw tape must declare variable-length records")
            while handle.tell() < source_bytes:
                disk_header = _read_exact(handle, RAW_RECORD_HEADER.size)
                sequence, epoch, receive_ns, wall_ns, venue, payload_size = (
                    RAW_RECORD_HEADER.unpack(disk_header)
                )
                if sequence <= 0 or epoch <= 0 or receive_ns <= 0 or wall_ns <= 0:
                    raise ValueError("raw tape record identity is invalid")
                if venue < 1 or venue > 6:
                    raise ValueError("raw tape venue is invalid")
                if payload_size <= 0 or payload_size > maximum_raw_payload_bytes:
                    raise ValueError("raw tape payload size is invalid")
                _read_exact(handle, payload_size)
                if last_sequence and sequence <= last_sequence:
                    raise ValueError("raw tape sequence is not strictly increasing")
                if not first_sequence:
                    first_sequence = sequence
                last_sequence = sequence
                records += 1
            if handle.tell() != source_bytes:
                raise ValueError("raw tape boundary mismatch")

    return TapeValidation(
        magic=magic.decode("ascii"),
        schema_version=schema_version,
        model_sha=model_sha,
        run_id=run_id,
        session_id=session_id,
        source=source,
        creation_wall_ns=creation_wall_ns,
        source_bytes=source_bytes,
        records=records,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _closed_candidate(
    path: Path, *, now: int, minimum_age_seconds: int
) -> tuple[bool, str]:
    if path.name.endswith(".open") or path.suffix != ".bin":
        return False, "not_closed_bin"
    try:
        stat = path.stat()
    except OSError:
        return False, "stat_failed"
    if now - int(stat.st_mtime) < minimum_age_seconds:
        return False, "within_close_grace"
    if SEGMENT_NAME.search(path.name):
        return True, "segmented_closed_file"
    legacy = LEGACY_PID.search(path.name)
    if legacy and _pid_alive(int(legacy.group("pid"))):
        return False, "legacy_writer_pid_alive"
    return True, "legacy_writer_absent"


def _safe_relative(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("candidate is not a regular file")
    return resolved.relative_to(resolved_root)


def _archive_target(archive_root: Path, relative: Path, digest: str) -> Path:
    directory = archive_root / relative.parent
    return directory / f"{relative.name}.{digest[:20]}.gz"


def archive_closed_binary_tapes(
    run_root: Path,
    policy: dict[str, Any],
    expected_sha: str,
    *,
    now: int | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    """Archive all eligible closed segments and return an auditable report."""
    current_time = int(time.time()) if now is None else int(now)
    root = run_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    minimum_age = max(1, int(policy.get("minimum_closed_age_seconds") or 30))
    allowed_schema_versions = policy.get("allowed_schema_versions")
    if not isinstance(allowed_schema_versions, list) or not allowed_schema_versions:
        allowed_schema_versions = [1, 2, 3]
    maximum_raw_payload = max(
        1, int(policy.get("maximum_raw_payload_bytes") or 2 * 1024 * 1024)
    )
    archive_relative = Path(
        str(policy.get("archive_directory") or "archive/binary_tapes")
    )
    if archive_relative.is_absolute() or ".." in archive_relative.parts:
        raise ValueError("unsafe binary tape archive directory")
    archive_root = (root / archive_relative).resolve()
    archive_root.relative_to(root)

    patterns = policy.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("binary tape retention requires explicit patterns")
    candidates: dict[Path, str] = {}
    skipped: list[dict[str, str]] = []
    for pattern in patterns:
        for path in root.glob(str(pattern)):
            try:
                relative = _safe_relative(root, path)
            except (OSError, ValueError) as exc:
                skipped.append({"path": str(path), "reason": str(exc)})
                continue
            if archive_relative in relative.parents or relative == archive_relative:
                continue
            eligible, reason = _closed_candidate(
                path, now=current_time, minimum_age_seconds=minimum_age
            )
            if eligible:
                candidates[path.resolve()] = reason
            else:
                skipped.append({"path": str(relative), "reason": reason})

    manifest_path = archive_root / "manifest.json"
    manifest = _json(manifest_path)
    rows = (
        manifest.get("segments")
        if isinstance(manifest.get("segments"), list)
        else []
    )
    by_source = {
        str(row.get("source_relative")): row
        for row in rows
        if isinstance(row, dict) and row.get("source_relative")
    }
    archived: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    reclaimed_bytes = 0

    for source_path in sorted(candidates):
        try:
            relative = _safe_relative(root, source_path)
            validation = validate_closed_tape(
                source_path,
                expected_sha,
                allowed_schema_versions=allowed_schema_versions,
                maximum_raw_payload_bytes=maximum_raw_payload,
            )
            digest, source_bytes = _sha256(source_path)
            if source_bytes != validation.source_bytes:
                raise ValueError("binary tape changed while hashing")
            target = _archive_target(archive_root, relative, digest)
            target_relative = target.relative_to(root)
            compressed_bytes = 0
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    temporary = target.with_name(
                        f"{target.name}.tmp.{os.getpid()}"
                    )
                    with source_path.open("rb") as input_handle, temporary.open(
                        "wb"
                    ) as raw_output:
                        with gzip.GzipFile(
                            filename=relative.name,
                            mode="wb",
                            fileobj=raw_output,
                            mtime=0,
                        ) as output:
                            shutil.copyfileobj(
                                input_handle, output, length=1024 * 1024
                            )
                        raw_output.flush()
                        os.fsync(raw_output.fileno())
                    compressed_digest, decompressed_bytes = _gzip_digest(temporary)
                    if (
                        compressed_digest != digest
                        or decompressed_bytes != source_bytes
                    ):
                        temporary.unlink(missing_ok=True)
                        raise ValueError("binary tape gzip verification failed")
                    os.replace(temporary, target)
                    _fsync_directory(target.parent)
                compressed_digest, decompressed_bytes = _gzip_digest(target)
                if compressed_digest != digest or decompressed_bytes != source_bytes:
                    raise ValueError("existing binary tape archive mismatch")
                compressed_bytes = target.stat().st_size
            prior = by_source.get(str(relative), {})
            entry = {
                "source_relative": str(relative),
                "archive_relative": str(target_relative),
                "source_sha256": digest,
                "compressed_bytes": compressed_bytes,
                "source_mtime_ns": source_path.stat().st_mtime_ns,
                "archived_at": current_time,
                **asdict(validation),
            }
            if prior and prior.get("source_sha256") not in (None, digest):
                raise ValueError("manifest source identity conflict")
            by_source[str(relative)] = entry
            if not dry_run:
                _atomic_json(
                    manifest_path,
                    {
                        "schema": "polymarket_v7_binary_tape_archive_manifest_v1",
                        "paper_only": True,
                        "authenticated_execution": False,
                        "real_order_submission": False,
                        "model_sha": expected_sha,
                        "updated_at": current_time,
                        "segments": sorted(
                            by_source.values(),
                            key=lambda row: row["source_relative"],
                        ),
                    },
                )
                source_path.unlink()
                _fsync_directory(source_path.parent)
                reclaimed_bytes += max(0, source_bytes - compressed_bytes)
            archived.append(entry)
        except (OSError, UnicodeDecodeError, ValueError, struct.error) as exc:
            failures.append(
                {
                    "path": str(source_path.relative_to(root)),
                    "reason": str(exc),
                }
            )

    return {
        "schema": "polymarket_v7_binary_tape_retention_status_v1",
        "timestamp": current_time,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "expected_sha": expected_sha,
        "archive_directory": str(archive_relative),
        "candidate_count": len(candidates),
        "archived": archived,
        "skipped": sorted(
            skipped, key=lambda row: (row["path"], row["reason"])
        ),
        "failures": failures,
        "reclaimed_bytes": reclaimed_bytes,
        "complete": not failures,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-root", type=Path, default=Path("runs/paper_v7_live")
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/v7_data_retention.json")
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = _json(args.config)
    if config.get("schema") != "polymarket_v7_data_retention_v1":
        raise SystemExit("invalid V7 retention config")
    policy = config.get("binary_tapes")
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        raise SystemExit("binary tape retention is not enabled")
    result = archive_closed_binary_tapes(
        args.run_root,
        policy,
        args.expected_sha,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        _atomic_json(
            args.run_root / "control" / "binary_tape_retention_status.json",
            result,
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
