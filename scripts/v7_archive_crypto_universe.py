#!/usr/bin/env python3
"""Archive the exact live crypto universe without widening discovery."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

SOURCE_SCHEMA = "polymarket_v7_crypto_universe_snapshot_v1"
ARCHIVE_SCHEMA = "polymarket_v7_point_in_time_crypto_universe_v1"
ALLOWED_ASSETS = {"BTC", "ETH", "SOL", "XRP"}


def load_crypto_universe(path: Path, *, model_sha: str, maximum_age_seconds: int) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    now_ms = time.time_ns() // 1_000_000
    timestamp_ms = int(value.get("timestamp_ms") or 0)
    age_ms = now_ms - timestamp_ms
    if value.get("schema") != SOURCE_SCHEMA:
        raise ValueError("crypto_universe_schema")
    if value.get("model_sha") != model_sha:
        raise ValueError("crypto_universe_sha")
    if (
        value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("execution_authority") is not False
    ):
        raise ValueError("crypto_universe_authority")
    if timestamp_ms <= 0 or age_ms < -5_000 or age_ms > maximum_age_seconds * 1000:
        raise ValueError("crypto_universe_stale")
    rows = value.get("markets")
    if not isinstance(rows, list) or not rows:
        raise ValueError("crypto_universe_empty")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("crypto_universe_row")
        market_id = str(row.get("market_id") or "")
        condition_id = str(row.get("condition_id") or "")
        asset = str(row.get("asset") or "")
        horizon = str(row.get("horizon") or "")
        semantic_hash = str(row.get("settlement_semantic_hash") or "")
        if (
            not market_id or not condition_id or market_id in seen
            or asset not in ALLOWED_ASSETS or not horizon
            or len(semantic_hash) != 64
        ):
            raise ValueError("crypto_universe_identity")
        seen.add(market_id)
    return value


def archive_value(source: dict[str, Any], *, model_sha: str, captured_ts_ms: int) -> dict[str, Any]:
    rows = source["markets"]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    contexts = sorted({(str(row["asset"]), str(row["horizon"])) for row in rows})
    return {
        "schema": ARCHIVE_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "model_sha": model_sha,
        "captured_ts_ms": int(captured_ts_ms),
        "source_timestamp_ms": int(source["timestamp_ms"]),
        "source_schema": SOURCE_SCHEMA,
        "source": "canonical_live_crypto_universe",
        "market_count": len(rows),
        "assets": sorted({asset for asset, _ in contexts}),
        "contexts": [{"asset": asset, "horizon": horizon} for asset, horizon in contexts],
        "membership_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "markets": rows,
    }


def write_archive(archive_dir: Path, value: dict[str, Any]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    name = f"crypto-universe-{int(value['captured_ts_ms'])}-{str(value['model_sha'])[:8]}.json.gz"
    path = archive_dir / name
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(payload, mtime=0)
    if path.exists():
        if path.read_bytes() != compressed:
            raise ValueError("immutable_crypto_archive_collision")
        return path
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_bytes(compressed)
    os.replace(tmp, path)
    latest = archive_dir / "latest.json.gz"
    tmp_latest = latest.with_name(latest.name + f".tmp.{os.getpid()}")
    tmp_latest.write_bytes(compressed)
    os.replace(tmp_latest, latest)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--maximum-age-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise ValueError("model_sha must be a 40-character lowercase hexadecimal SHA")
    source = load_crypto_universe(
        args.universe, model_sha=args.model_sha,
        maximum_age_seconds=max(1, int(args.maximum_age_seconds)),
    )
    value = archive_value(source, model_sha=args.model_sha, captured_ts_ms=time.time_ns() // 1_000_000)
    path = write_archive(args.archive_dir, value)
    print(json.dumps({
        "archive": str(path), "market_count": value["market_count"],
        "assets": value["assets"], "membership_sha256": value["membership_sha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
