#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

if __package__:
    from .v6_market_proxy import FALLBACK_MARKETS, Proxy, gamma_req, valid_cache_market
else:
    from v6_market_proxy import FALLBACK_MARKETS, Proxy, gamma_req, valid_cache_market

PAGE_SIZE = 100
PAGE_ATTEMPTS = 3
PAGE_TIMEOUT_SECONDS = 12.0
RETRY_BACKOFF_SECONDS = (0.5, 1.5)


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_key(row: dict[str, Any]) -> str:
    return str(row.get("conditionId") or row.get("id") or "")


def _valid_liquid_row(row: Any, min_liquidity: float) -> bool:
    return (
        valid_cache_market(row)
        and number(row.get("liquidityNum"), -1.0) >= max(0.0, min_liquidity)
    )


def _gamma_page(
    gamma: str,
    offset: int,
    limit: int,
    min_liquidity: float,
) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Fetch one independent Gamma offset page with bounded retry/backoff.

    Relay cache validity is defined by the explicit ``min_markets`` floor, not by
    every requested discovery page succeeding in the same attempt. Returning page
    failures separately lets the caller keep valid independent pages while still
    recording upstream degradation in the relay status.
    """
    errors: list[str] = []
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(max(1, min(PAGE_SIZE, limit))),
        "offset": str(max(0, offset)),
        "order": "liquidityNum",
        "ascending": "false",
        "liquidity_num_min": str(max(0.0, min_liquidity)),
    }
    url = gamma.rstrip("/") + "/markets?" + urllib.parse.urlencode(params)
    for attempt in range(PAGE_ATTEMPTS):
        try:
            value = gamma_req(url, timeout=PAGE_TIMEOUT_SECONDS)
            if not isinstance(value, list):
                raise RuntimeError("Gamma legacy page is not a list")
            rows = [
                dict(row)
                for row in value
                if isinstance(row, dict) and _valid_liquid_row(row, min_liquidity)
            ]
            if rows:
                return offset, rows, errors
            errors.append(f"offset={offset}:attempt={attempt + 1}:empty")
        except Exception as exc:
            errors.append(f"offset={offset}:attempt={attempt + 1}:{type(exc).__name__}:{exc}")
        if attempt < PAGE_ATTEMPTS - 1:
            time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
    return offset, [], errors


def _merge_rows(
    target: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
    min_liquidity: float,
) -> None:
    for row in rows:
        if not _valid_liquid_row(row, min_liquidity):
            continue
        key = _row_key(row)
        if key:
            target.setdefault(key, dict(row))


def build_fresh_rows(
    proxy: Proxy,
    requested: int,
    min_markets: int,
    min_liquidity: float,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """Build the freshest valid catalogue without all-or-nothing page coupling.

    Independent legacy pages are fetched concurrently and retried. If fewer than
    ``requested`` rows survive, keyset Gamma and then the existing CLOB discovery
    path may supplement them. The snapshot is accepted only when the explicit
    ``min_markets`` contract is satisfied; no empty/stale cache is synthesized.
    """
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    offsets = list(range(0, requested, PAGE_SIZE))
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(offsets)))) as pool:
        futures = [
            pool.submit(
                _gamma_page,
                proxy.gamma,
                offset,
                min(PAGE_SIZE, requested - offset),
                min_liquidity,
            )
            for offset in offsets
        ]
        for future in as_completed(futures):
            offset, rows, page_errors = future.result()
            errors.extend(page_errors)
            _merge_rows(merged, rows, min_liquidity)
            if not rows:
                errors.append(f"offset={offset}:unavailable")

    source = "gamma_legacy_retried"
    if len(merged) < requested:
        query = {
            "active": ["true"],
            "closed": ["false"],
            "order": ["liquidityNum"],
            "ascending": ["false"],
            "liquidity_num_min": [str(max(0.0, min_liquidity))],
        }
        try:
            _merge_rows(merged, proxy.gamma_rows(requested, query), min_liquidity)
            source = "gamma_legacy_plus_keyset"
        except Exception as exc:
            errors.append(f"gamma_keyset:{type(exc).__name__}:{exc}")

    if len(merged) < min_markets:
        try:
            _merge_rows(merged, proxy.clob_rows(min_liquidity), min_liquidity)
            source = "gamma_plus_clob" if merged else "clob"
        except Exception as exc:
            errors.append(f"clob:{type(exc).__name__}:{exc}")

    rows = sorted(
        merged.values(),
        key=lambda row: number(row.get("liquidityNum"), 0.0),
        reverse=True,
    )[:requested]
    if len(rows) < min_markets:
        detail = "; ".join(errors[-12:])
        raise RuntimeError(
            f"fresh relay cache has only {len(rows)} markets; need {min_markets}; {detail}"
        )
    if len(rows) < requested:
        source += "_partial"
    return rows, source, errors


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
    rows, source, errors = build_fresh_rows(
        proxy,
        requested,
        min_markets,
        max(0.0, args.min_liquidity),
    )
    proxy.error = "; ".join(errors[-12:]) if errors else ""
    proxy.failures = len(errors)
    proxy.save(rows)
    proxy.stat(source, len(rows), source.startswith("gamma"), 0.0)

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
        "schema": "polymarket_v6_market_cache_relay_v2",
        "timestamp": int(time.time()),
        "cache_age_seconds": cache_age,
        "markets": len(cache_rows),
        "requested_markets": requested,
        "minimum_markets": min_markets,
        "partial_snapshot": len(cache_rows) < requested,
        "upstream_failures": len(errors),
        "source": source,
        "paper_only": True,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
