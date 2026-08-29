#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import time
from pathlib import Path
from typing import Any

SOURCE_SCHEMA = "polymarket_v6_market_proxy_cache_v1"
ARCHIVE_SCHEMA = "polymarket_v7_point_in_time_universe_v1"


def finite_number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def validate_cache(payload: Any) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(payload, dict) or payload.get("schema") != SOURCE_SCHEMA:
        raise ValueError("market cache schema is invalid")
    ts = finite_number(payload.get("timestamp"))
    if ts is None or ts <= 0:
        raise ValueError("market cache timestamp is invalid")
    rows = payload.get("markets")
    if not isinstance(rows, list) or not rows:
        raise ValueError("market cache contains no markets")
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("market cache contains a non-object market")
        market_id = str(row.get("id") or "")
        condition_id = str(row.get("conditionId") or "")
        if not market_id or not condition_id:
            raise ValueError("market cache row is missing stable identifiers")
        clean.append(row)
    return int(ts), clean


def archive_payload(source: dict[str, Any], cadence_seconds: int) -> tuple[int, dict[str, Any]]:
    source_ts, rows = validate_cache(source)
    cadence = max(60, int(cadence_seconds))
    bucket_ts = (source_ts // cadence) * cadence
    payload = {
        "schema": ARCHIVE_SCHEMA,
        "bucket_timestamp": bucket_ts,
        "snapshot_timestamp": source_ts,
        "cadence_seconds": cadence,
        "source_schema": SOURCE_SCHEMA,
        "source": source.get("source"),
        "market_count": len(rows),
        "markets": rows,
    }
    return bucket_ts, payload


def encoded_snapshot(payload: dict[str, Any]) -> bytes:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def snapshot_filename(bucket_ts: int) -> str:
    return f"universe-{int(bucket_ts)}.json.gz"


def _snapshot_timestamp(path: Path) -> int | None:
    name = path.name
    if not name.startswith("universe-") or not name.endswith(".json.gz"):
        return None
    try:
        return int(name[len("universe-") : -len(".json.gz")])
    except ValueError:
        return None


def archive_once(
    cache_path: Path,
    archive_dir: Path,
    *,
    cadence_seconds: int = 1800,
    retention_days: float = 45.0,
    now_ts: int | None = None,
) -> dict[str, Any]:
    source = json.loads(cache_path.read_text(encoding="utf-8"))
    bucket_ts, payload = archive_payload(source, cadence_seconds)
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / snapshot_filename(bucket_ts)
    created = False

    # First snapshot observed in a bucket is immutable. The relay may run every five
    # minutes, but the point-in-time research universe is sampled once per 30-minute
    # bucket and is never overwritten by later survivor information from that bucket.
    if not target.exists():
        tmp = archive_dir / f".{target.name}.tmp.{os.getpid()}"
        tmp.write_bytes(encoded_snapshot(payload))
        try:
            # Atomic no-clobber publication on a normal local/server filesystem.
            os.link(tmp, target)
            created = True
        except FileExistsError:
            created = False
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    reference_now = int(now_ts if now_ts is not None else time.time())
    retention_seconds = max(86400, int(float(retention_days) * 86400))
    cutoff = reference_now - retention_seconds
    removed = 0
    for candidate in archive_dir.glob("universe-*.json.gz"):
        ts = _snapshot_timestamp(candidate)
        if ts is not None and ts < cutoff:
            candidate.unlink(missing_ok=True)
            removed += 1

    return {
        "schema": "polymarket_v7_point_in_time_universe_archive_status_v1",
        "source_cache": str(cache_path),
        "archive_dir": str(archive_dir),
        "bucket_timestamp": bucket_ts,
        "snapshot_timestamp": int(payload["snapshot_timestamp"]),
        "market_count": int(payload["market_count"]),
        "cadence_seconds": int(payload["cadence_seconds"]),
        "created": created,
        "target": str(target),
        "retention_days": float(retention_days),
        "removed_expired_snapshots": removed,
        "paper_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive immutable point-in-time Polymarket universe snapshots")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--cadence-seconds", type=int, default=1800)
    parser.add_argument("--retention-days", type=float, default=45.0)
    args = parser.parse_args()
    summary = archive_once(
        args.cache,
        args.archive_dir,
        cadence_seconds=args.cadence_seconds,
        retention_days=args.retention_days,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
