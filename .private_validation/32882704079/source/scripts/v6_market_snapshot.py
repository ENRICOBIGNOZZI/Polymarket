#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

if __package__:
    from .v6_market_proxy import FALLBACK_MARKETS, Proxy, valid_cache_market
else:
    from v6_market_proxy import FALLBACK_MARKETS, Proxy, valid_cache_market


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a fresh, paper-only V6 public-market cache for relay delivery."
    )
    parser.add_argument("--gamma", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob", default="https://clob.polymarket.com")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--markets", type=int, default=FALLBACK_MARKETS)
    parser.add_argument("--min-liquidity", type=float, default=10.0)
    parser.add_argument("--min-markets", type=int, default=100)
    parser.add_argument("--max-cache-age-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)

    requested = max(1, min(FALLBACK_MARKETS, int(args.markets)))
    min_markets = max(1, min(requested, int(args.min_markets)))
    status = args.status or args.output.with_suffix(args.output.suffix + ".status.json")
    proxy = Proxy(args.gamma, args.clob, args.output, status)
    rows = proxy.markets(
        {
            "limit": [str(requested)],
            "offset": ["0"],
            "liquidity_num_min": [str(max(0.0, args.min_liquidity))],
        }
    )
    if len(rows) < min_markets:
        raise RuntimeError(f"fresh relay cache has only {len(rows)} markets; need {min_markets}")

    try:
        payload = json.loads(args.output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"relay cache was not written: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "polymarket_v6_market_proxy_cache_v1":
        raise RuntimeError("relay cache schema is invalid")
    cache_rows = payload.get("markets")
    if not isinstance(cache_rows, list) or len(cache_rows) < min_markets:
        raise RuntimeError("relay cache did not persist enough markets")
    cache_age = max(0.0, time.time() - number(payload.get("timestamp"), -1.0))
    if cache_age > max(0.0, args.max_cache_age_seconds):
        raise RuntimeError(f"relay cache is too old: {cache_age:.1f}s")
    if not all(
        valid_cache_market(row)
        and number(row.get("liquidityNum")) >= max(0.0, args.min_liquidity)
        for row in cache_rows
    ):
        raise RuntimeError("relay cache contains invalid market rows")

    summary = {
        "schema": "polymarket_v6_market_cache_relay_v1",
        "timestamp": int(time.time()),
        "cache_age_seconds": cache_age,
        "markets": len(cache_rows),
        "served_page_markets": len(rows),
        "source": proxy.source,
        "paper_only": True,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
