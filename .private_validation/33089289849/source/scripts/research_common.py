#!/usr/bin/env python3
"""Shared deterministic utilities for the Polymarket autonomous research plane."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def finite(value: Any, default: float = 0.0) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return output if math.isfinite(output) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def parse_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw // 1000 if raw > 10_000_000_000 else raw
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        raw = int(float(text))
        return raw // 1000 if raw > 10_000_000_000 else raw
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def connect_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def stable_file_fingerprint(paths: Iterable[Path], root: Path | None = None) -> tuple[str, list[dict[str, Any]]]:
    root = (root or Path(".")).resolve()
    records: list[dict[str, Any]] = []
    for raw in sorted({str(path) for path in paths}):
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            relative = str(candidate.resolve().relative_to(root))
        except ValueError:
            relative = str(candidate.resolve())
        if candidate.is_file():
            records.append(
                {
                    "path": relative,
                    "present": True,
                    "bytes": candidate.stat().st_size,
                    "sha256": file_sha256(candidate),
                }
            )
        else:
            records.append({"path": relative, "present": False, "bytes": 0, "sha256": None})
    return sha256_json(records), records


def safe_relative_script(path: str) -> bool:
    candidate = Path(path)
    return (
        not candidate.is_absolute()
        and ".." not in candidate.parts
        and len(candidate.parts) >= 2
        and candidate.parts[0] == "scripts"
        and candidate.suffix == ".py"
    )


def non_overlapping_window(current_start: int, current_end: int, prior_end: int) -> bool:
    return current_start > 0 and current_end >= current_start and current_start > prior_end
