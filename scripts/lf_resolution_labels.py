#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import external_intelligence as ext

SCHEMA = "polymarket_lf_resolution_label_report_v1"


def read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def write_jsonl_gz(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    ext.write_jsonl_gz(path, rows)


def terminal_label_inventory(
    observations: Sequence[dict[str, Any]], prices: Sequence[dict[str, Any]], now: int
) -> dict[str, Any]:
    latest_meta: dict[str, dict[str, Any]] = {}
    for row in observations:
        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue
        previous = latest_meta.get(market_id)
        if previous is None or ext.integer(row.get("observed_ts")) >= ext.integer(previous.get("observed_ts")):
            latest_meta[market_id] = dict(row)

    resolved = {
        str(row.get("market_id"))
        for row in prices
        if row.get("resolved_outcome") in (0, 1) and str(row.get("market_id") or "")
    }
    expired = {
        market_id
        for market_id, row in latest_meta.items()
        if ext.integer(row.get("end_ts")) > 0 and ext.integer(row.get("end_ts")) < now
    }
    missing = sorted(expired.difference(resolved))
    return {
        "observed_markets": len(latest_meta),
        "expired_markets": len(expired),
        "resolved_labels": len(expired.intersection(resolved)),
        "missing_resolution_labels": len(missing),
        "missing_market_ids": missing,
    }


def fetch_resolution(
    market_id: str, resolver: Callable[[str], Any] | None = None
) -> dict[str, Any] | None:
    if resolver is None:
        resolver = lambda value: ext.request_json(f"{ext.PM_GAMMA}/markets/{value}")
    payload = resolver(market_id)
    raw = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(raw, dict):
        return None
    market = ext.parse_pm_market(raw)
    if market is None or market.resolved_outcome not in (0, 1):
        return None
    return {
        "schema": ext.PRICE_SCHEMA,
        "observed_ts": max(ext.integer(market.end_ts), int(time.time())),
        "observed_utc": ext.iso_utc(max(ext.integer(market.end_ts), int(time.time()))),
        "market_id": market.market_id,
        "event_id": market.event_id,
        "question": market.question,
        "category": market.category,
        "end_ts": market.end_ts,
        "bid": market.bid,
        "ask": market.ask,
        "mid": market.mid,
        "resolved_outcome": market.resolved_outcome,
        "quote_provenance": "gamma_terminal_resolution_label",
    }


def backfill_resolution_labels(
    observations: Sequence[dict[str, Any]],
    prices: Sequence[dict[str, Any]],
    *,
    now: int,
    max_markets: int = 50,
    resolver: Callable[[str], Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    before = terminal_label_inventory(observations, prices, now)
    new_labels: list[dict[str, Any]] = []
    failures: list[str] = []
    for market_id in before["missing_market_ids"][: max(0, int(max_markets))]:
        try:
            label = fetch_resolution(market_id, resolver=resolver)
        except Exception as exc:  # research collector must fail closed per market
            failures.append(f"{market_id}:{type(exc).__name__}")
            continue
        if label is not None:
            new_labels.append(label)

    merged = ext.merge_rows(
        list(prices),
        new_labels,
        key_fields=("market_id", "observed_ts"),
        min_timestamp=0,
        max_rows=max(1, len(prices) + len(new_labels) + 100),
    )
    after = terminal_label_inventory(observations, merged, now)
    report = {
        "schema": SCHEMA,
        "before": before,
        "queried_markets": min(max(0, int(max_markets)), before["missing_resolution_labels"]),
        "labels_added": len(new_labels),
        "failures": failures,
        "after": after,
        "point_in_time_feature_history_unchanged": True,
        "production_change": False,
    }
    return merged, report


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only terminal resolution label backfill")
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--prices-in", type=Path, required=True)
    parser.add_argument("--prices-out", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-markets", type=int, default=50)
    parser.add_argument("--now", type=int, default=int(time.time()))
    args = parser.parse_args()

    observations = read_jsonl_gz(args.observations)
    prices = read_jsonl_gz(args.prices_in)
    merged, report = backfill_resolution_labels(
        observations, prices, now=args.now, max_markets=args.max_markets
    )
    write_jsonl_gz(args.prices_out, merged)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
