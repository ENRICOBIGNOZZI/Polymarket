#!/usr/bin/env python3
"""Archive the native V7 point-in-time investable universe.

The archive is fetched directly from Gamma at archive time.  It does not consume
or translate any V3-V6 market proxy/cache.  Each gzip snapshot is immutable,
bound to the exact paper-validated model SHA, and records the market membership
that was observable at the capture timestamp.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_point_in_time_universe_v2"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def fetch_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-universe/2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalized_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    market_id = str(raw.get("id") or "").strip()
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    tokens = [str(item).strip() for item in array(raw.get("clobTokenIds")) if str(item).strip()]
    outcomes = [str(item) for item in array(raw.get("outcomes"))]
    if not market_id or not condition_id or len(tokens) < 2:
        return None
    event_ids: list[str] = []
    for event in raw.get("events") if isinstance(raw.get("events"), list) else []:
        if isinstance(event, dict) and str(event.get("id") or "").strip():
            event_ids.append(str(event.get("id")).strip())
    return {
        "market_id": market_id,
        "condition_id": condition_id,
        "event_ids": sorted(set(event_ids)),
        "slug": str(raw.get("slug") or ""),
        "question": str(raw.get("question") or ""),
        "group_item_title": str(raw.get("groupItemTitle") or ""),
        "clob_token_ids": tokens,
        "outcomes": outcomes,
        "liquidity": max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0))),
        "volume24h": max(0.0, finite(raw.get("volume24hr"), finite(raw.get("volume24h"), 0.0))),
        "end_date": str(raw.get("endDate") or raw.get("end_date_iso") or ""),
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "accepting_orders": bool(raw.get("acceptingOrders", True)),
        "neg_risk": bool(raw.get("negRisk", False)),
    }


def discover(
    gamma_url: str,
    *,
    market_limit: int,
    min_liquidity: float,
    page_size: int = 100,
    fetcher=fetch_json,
) -> list[dict[str, Any]]:
    limit = max(1, min(5000, int(market_limit)))
    page = max(1, min(500, int(page_size)))
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < limit:
        query = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": min(page, limit - len(out)),
            "offset": offset,
            "order": "liquidityNum",
            "ascending": "false",
        })
        value = fetcher(gamma_url.rstrip("/") + "/markets?" + query)
        rows = value if isinstance(value, list) else value.get("markets", []) if isinstance(value, dict) else []
        if not rows:
            break
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            market = normalized_market(raw)
            if market is None or market["liquidity"] + 1e-12 < float(min_liquidity):
                continue
            out.append(market)
            if len(out) >= limit:
                break
        if len(rows) < min(page, limit - min(len(out), limit)) or len(rows) < page:
            break
        offset += len(rows)
    unique = {row["market_id"]: row for row in out}
    return sorted(unique.values(), key=lambda row: (-float(row["liquidity"]), str(row["market_id"])))[:limit]


def snapshot(
    markets: list[dict[str, Any]],
    *,
    model_sha: str,
    captured_ts_ms: int,
    gamma_url: str,
    market_limit: int,
    min_liquidity: float,
    cadence_seconds: int,
) -> dict[str, Any]:
    if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha.lower()):
        raise ValueError("model_sha must be a 40-character hex SHA")
    if not markets:
        raise ValueError("universe snapshot cannot be empty")
    ids = [str(row["market_id"]) for row in markets]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate market IDs in snapshot")
    canonical_rows = json.dumps(markets, sort_keys=True, separators=(",", ":"))
    return {
        "schema": SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "model_sha": model_sha,
        "captured_ts_ms": int(captured_ts_ms),
        "captured_ts": int(captured_ts_ms) // 1000,
        "cadence_seconds": int(cadence_seconds),
        "source": "gamma_active_closed_false_direct",
        "gamma_url": gamma_url.rstrip("/"),
        "market_limit": int(market_limit),
        "minimum_liquidity_usd": float(min_liquidity),
        "market_count": len(markets),
        "membership_sha256": hashlib.sha256(canonical_rows.encode("utf-8")).hexdigest(),
        "markets": markets,
    }


def write_snapshot(archive_dir: Path, value: dict[str, Any], *, retention_days: int) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts_ms = int(value["captured_ts_ms"])
    sha8 = str(value["model_sha"])[:8]
    path = archive_dir / f"universe-{ts_ms}-{sha8}.json.gz"
    if path.exists():
        existing = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        if existing != value:
            raise ValueError("immutable universe snapshot path collision")
        return path
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_bytes(gzip.compress(payload, mtime=0))
    os.replace(temporary, path)
    latest = archive_dir / "latest.json.gz"
    tmp_latest = latest.with_name(latest.name + f".tmp.{os.getpid()}")
    tmp_latest.write_bytes(path.read_bytes())
    os.replace(tmp_latest, latest)
    cutoff_ms = ts_ms - max(1, int(retention_days)) * 86_400_000
    for old in archive_dir.glob("universe-*.json.gz"):
        try:
            old_ts = int(old.name.split("-", 2)[1])
        except (IndexError, ValueError):
            continue
        if old_ts < cutoff_ms:
            old.unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--market-limit", type=int, default=1000)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--cadence-seconds", type=int, default=1800)
    parser.add_argument("--retention-days", type=int, default=45)
    args = parser.parse_args(argv)
    captured = time.time_ns() // 1_000_000
    markets = discover(
        args.gamma_url,
        market_limit=args.market_limit,
        min_liquidity=args.min_liquidity,
        page_size=args.page_size,
    )
    value = snapshot(
        markets,
        model_sha=args.model_sha,
        captured_ts_ms=captured,
        gamma_url=args.gamma_url,
        market_limit=args.market_limit,
        min_liquidity=args.min_liquidity,
        cadence_seconds=args.cadence_seconds,
    )
    path = write_snapshot(args.archive_dir, value, retention_days=args.retention_days)
    print(json.dumps({
        "schema": SCHEMA,
        "snapshot": str(path),
        "market_count": value["market_count"],
        "model_sha": value["model_sha"],
        "captured_ts_ms": value["captured_ts_ms"],
        "paper_only": True,
        "authenticated_execution": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
