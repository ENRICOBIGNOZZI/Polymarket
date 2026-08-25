#!/usr/bin/env python3
from __future__ import annotations

import math
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

try:
    import v6_hard_arb_paper_v2 as base
except ModuleNotFoundError:
    from scripts import v6_hard_arb_paper_v2 as base

BASE_MAX_EXECUTABLE = base.max_executable_shares


def _one_book(clob: str, token: str):
    start = time.monotonic_ns() // 1_000_000
    try:
        raw = base.request_json(clob.rstrip("/") + "/book?" + urllib.parse.urlencode({"token_id": token}))
    except Exception:
        return None
    received = time.monotonic_ns() // 1_000_000
    if not isinstance(raw, dict):
        return None
    asks = []
    for row in raw.get("asks", []):
        if not isinstance(row, dict):
            continue
        p, q = base.finite(row.get("price"), math.nan), base.finite(row.get("size"), 0.0)
        if math.isfinite(p) and 0 < p < 1 and q > 0:
            asks.append((p, q))
    asks.sort()
    if not asks:
        return None
    book = base.Book(str(raw.get("asset_id") or token), asks, max(1.0, base.finite(raw.get("min_order_size"), 1.0)))
    book.received_ms = received
    book.request_ms = start
    book.tick = max(1e-6, base.finite(raw.get("tick_size"), 0.01))
    return book


def _snapshot(clob: str, tokens: list[str]) -> dict[str, Any]:
    output = {}
    if not tokens:
        return output
    workers = min(32, max(1, len(tokens)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one_book, clob, token): token for token in tokens}
        for future in as_completed(futures):
            token = futures[future]
            try:
                book = future.result()
            except Exception:
                book = None
            if book is not None:
                output[token] = book
    return output


def fetch_books(clob: str, tokens: list[str]):
    """Two concurrent snapshots; final snapshot carries stability evidence."""
    first = _snapshot(clob, tokens)
    time.sleep(0.05)
    second = _snapshot(clob, tokens)
    if any(token not in first or token not in second for token in tokens):
        return {}
    for token in tokens:
        a, b = first[token], second[token]
        tick = max(getattr(a, "tick", 0.01), getattr(b, "tick", 0.01))
        b.prior_best_ask = a.asks[0][0]
        b.snapshot_stable = abs(b.asks[0][0] - a.asks[0][0]) <= tick + 1e-12
        b.snapshot_gap_ms = max(0, int(getattr(b, "received_ms", 0)) - int(getattr(a, "received_ms", 0)))
    return second


def max_executable_shares(books, fees, *, cash_room: float, max_trade_usd: float, min_edge: float, slippage_bps: float):
    if not books:
        return None
    received = [int(getattr(book, "received_ms", 0)) for book in books]
    if any(ts <= 0 for ts in received):
        return None
    now_ms = time.monotonic_ns() // 1_000_000
    if now_ms - min(received) > 2000:
        return None
    if max(received) - min(received) > 1000:
        return None
    if any(not bool(getattr(book, "snapshot_stable", False)) for book in books):
        return None
    return BASE_MAX_EXECUTABLE(
        books, fees, cash_room=cash_room, max_trade_usd=max_trade_usd,
        min_edge=min_edge, slippage_bps=slippage_bps,
    )


def atomic_json(path, value):
    if isinstance(value, dict):
        value = dict(value)
        value["cross_leg_freshness_guard"] = "per_token_monotonic_receive_time"
        value["snapshot_stability_guard"] = "two_concurrent_snapshots_within_one_tick"
        value["max_leg_age_ms"] = 2000
        value["max_cross_leg_skew_ms"] = 1000
        value["sequential_legging_unwind_model"] = False
        value["hard_arb_evidence_scope"] = "synchronized_paper_discovery_until_sequential_legging_is_ported"
    return BASE_ATOMIC_JSON(path, value)


BASE_ATOMIC_JSON = base.atomic_json


def main() -> int:
    base.fetch_books = fetch_books
    base.max_executable_shares = max_executable_shares
    base.atomic_json = atomic_json
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
