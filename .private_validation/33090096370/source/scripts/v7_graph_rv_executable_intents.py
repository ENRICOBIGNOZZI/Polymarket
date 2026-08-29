#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from v7_graph_rv_intents import FIELDS, atomic_csv, books, discover, parse_market
from v7_market_common import fee_per_share, request_json, resolve_fee_details


def scan(cfg: dict[str, Any], now: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    gamma = str(cfg["gamma_url"]).rstrip("/")
    clob = str(cfg["clob_url"]).rstrip("/")
    markets = discover(gamma, int(cfg.get("market_limit", 1000)), float(cfg.get("min_liquidity", 2.0)))
    current = books(clob, [market.yes_token for market in markets])
    event_ids = list(dict.fromkeys(market.event_id for market in markets if market.neg_risk and market.event_id))
    v7 = cfg.get("v7") or {}
    min_edge = float(cfg.get("min_net_edge", 0.00005))
    allocation = float(v7.get("relative_value_capital_fraction", 0.34))
    capital = float(cfg.get("starting_capital", 10000.0))
    max_notional = max(0.0, allocation * capital)
    rows: list[dict[str, Any]] = []
    stats = {"events_considered": 0, "events_complete": 0, "bundles": 0, "taker_edge_rejects": 0}
    for event_id in event_ids[: int(v7.get("hard_arb_max_events", 80))]:
        stats["events_considered"] += 1
        try:
            event = request_json(f"{gamma}/events/{event_id}")
        except Exception:
            continue
        if not isinstance(event, dict) or not event.get("negRisk") or event.get("negRiskAugmented"):
            continue
        raw_markets = event.get("markets")
        if not isinstance(raw_markets, list) or len(raw_markets) < 2:
            continue
        parsed = [parse_market(raw) for raw in raw_markets if isinstance(raw, dict)]
        if len(parsed) != len(raw_markets) or any(market is None for market in parsed):
            continue
        event_markets = [market for market in parsed if market is not None]
        missing = [market.yes_token for market in event_markets if market.yes_token not in current]
        if missing:
            try:
                current.update(books(clob, missing))
            except Exception:
                continue
        if any(market.yes_token not in current for market in event_markets):
            continue
        taker_cost = 0.0
        verified = True
        for market in event_markets:
            book = current[market.yes_token]
            fee = resolve_fee_details(market.raw, clob, market.condition_id, market.yes_token)
            if not fee.verified:
                verified = False
                break
            taker_cost += book.ask + fee_per_share(book.ask, fee, taker=True)
        edge = 1.0 - taker_cost
        if not verified or edge <= min_edge:
            stats["taker_edge_rejects"] += 1
            continue
        end_ts = max((market.end_ts for market in event_markets), default=0)
        execution_deadline = now + int(v7.get("graph_execution_timeout_seconds", 300))
        hold_deadline = max(now + int(v7.get("graph_hold_seconds", 3600)), end_ts + 3600 if end_ts else now + 3600)
        bundle_id = f"GRAPH_RV:{event_id}:{now // 3600}"
        for market in event_markets:
            book = current[market.yes_token]
            rows.append({
                "bundle_id": bundle_id,
                "strategy": "GRAPH_RV",
                "event_id": event_id,
                "created_ts": now,
                "mode": "TAKER_EXECUTABLE",
                "expected_edge": edge,
                "max_notional": max_notional,
                "market_id": market.market_id,
                "side": "YES",
                "weight": 1.0,
                "limit_price": book.ask,
                "execution_deadline_ts": execution_deadline,
                "hold_deadline_ts": hold_deadline,
            })
        stats["events_complete"] += 1
        stats["bundles"] += 1
    return rows, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="V7 Graph/RV executable taker-intent scanner")
    parser.add_argument("--config", type=Path, default=Path("config/paper_v7.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    v7 = cfg.get("v7") or {}
    if cfg.get("paper_only") is not True or v7.get("authenticated_execution") is not False or v7.get("real_order_submission") is not False:
        raise SystemExit("PAPER-only/authenticated-disabled V7 config required")
    now = int(time.time())
    failures: list[str] = []
    try:
        rows, stats = scan(cfg, now)
    except Exception as exc:
        rows, stats = [], {"events_considered": 0, "events_complete": 0, "bundles": 0, "taker_edge_rejects": 0}
        failures.append(f"{type(exc).__name__}:{exc}")
    rows.sort(key=lambda row: (float(row["expected_edge"]), row["bundle_id"]), reverse=True)
    atomic_csv(args.output, rows)
    status = {
        "schema": "polymarket_v7_graph_rv_executable_scan_v1",
        "timestamp": now,
        "paper_only": True,
        "intent_rows": len(rows),
        "bundles": len({row["bundle_id"] for row in rows}),
        "graph_rv": stats,
        "admission": "all_taker_executable_post_authoritative_fee_edge",
        "maker_or_mixed_requires_direct_joint_model": True,
        "product_of_marginals_forbidden": True,
        "failures": failures,
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.status.with_name(args.status.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
