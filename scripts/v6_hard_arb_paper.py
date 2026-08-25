#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from polymarket_fees import FeeDetails, FeeScheduleUnavailable, fee_per_share, resolve_fee_details


def finite(value: Any, default: float = math.nan) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return x if math.isfinite(x) else default


def arr(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            x = json.loads(value)
            return x if isinstance(x, list) else []
        except json.JSONDecodeError:
            return []
    return []


def get_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "polymarket-v6-hard-arb-paper/3",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def market_tokens(raw: dict) -> tuple[str, str] | None:
    ids = [str(x) for x in arr(raw.get("clobTokenIds"))]
    outcomes = [str(x).lower() for x in arr(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yi, ni = 0, 1
    for i, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == "yes":
            yi = i
        elif outcome == "no":
            ni = i
    return ids[yi], ids[ni]


def discover_event_ids(
    gamma: str,
    market_limit: int,
    min_liq: float,
    max_events: int,
) -> list[str]:
    """Discover NegRisk event ids inside a bounded *market* universe.

    `--markets` is a market-scan budget, not a requested number of NegRisk
    events.  The old implementation kept paging until it found `market_limit`
    event ids and could run beyond Gamma's supported offset range, producing a
    422 despite a healthy public market feed.
    """
    ids: list[str] = []
    offset = 0
    remaining = max(0, int(market_limit))
    event_cap = max(0, int(max_events))

    while remaining > 0 and offset < 5000 and len(ids) < event_cap:
        page_size = min(100, remaining)
        qs = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
                "order": "liquidityNum",
                "ascending": "false",
            }
        )
        raw = get_json(gamma + "/markets?" + qs)
        batch = (
            raw
            if isinstance(raw, list)
            else raw.get("markets", [])
            if isinstance(raw, dict)
            else []
        )
        if not batch:
            break

        for market in batch:
            if not isinstance(market, dict) or not market.get("negRisk"):
                continue
            if finite(
                market.get("liquidityNum"), finite(market.get("liquidity"), 0)
            ) < min_liq:
                continue
            event_id = str(market.get("eventId") or "")
            events = market.get("events")
            if (
                not event_id
                and isinstance(events, list)
                and events
                and isinstance(events[0], dict)
            ):
                event_id = str(events[0].get("id") or "")
            if event_id and event_id not in ids:
                ids.append(event_id)
                if len(ids) >= event_cap:
                    break

        consumed = len(batch)
        remaining -= consumed
        offset += consumed
        if consumed < page_size:
            break

    return ids


def event_spec(gamma: str, event_id: str) -> list[dict] | None:
    event = get_json(f"{gamma}/events/{event_id}")
    if (
        not isinstance(event, dict)
        or not event.get("negRisk")
        or event.get("negRiskAugmented")
    ):
        return None
    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) < 2:
        return None
    clean = []
    for market in markets:
        if not isinstance(market, dict) or market_tokens(market) is None:
            return None
        if (
            market.get("closed")
            or market.get("active") is False
            or market.get("enableOrderBook") is False
            or market.get("acceptingOrders") is False
        ):
            return None
        clean.append(market)
    return clean


def books(clob: str, tokens: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(tokens), 80):
        raw = get_json(clob + "/books", [{"token_id": x} for x in tokens[i : i + 80]])
        for book in raw if isinstance(raw, list) else []:
            if not isinstance(book, dict):
                continue
            token = str(book.get("asset_id") or "")
            asks = []
            for level in book.get("asks", []):
                if isinstance(level, dict):
                    price = finite(level.get("price"))
                    size = finite(level.get("size"), 0)
                    if math.isfinite(price) and 0 < price < 1 and size > 0:
                        asks.append((price, size))
            asks.sort()
            if token and asks:
                out[token] = {
                    "ask": asks[0][0],
                    "size": asks[0][1],
                    "min_order": max(1.0, finite(book.get("min_order_size"), 1.0)),
                }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=500)
    parser.add_argument("--min-liquidity", type=float, default=10)
    parser.add_argument("--max-events", type=int, default=80)
    parser.add_argument("--min-edge", type=float, default=0.0002)
    parser.add_argument("--max-trade-usd", type=float, default=60)
    parser.add_argument("--slippage-bps", type=float, default=5)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text())
    gamma, clob = cfg["gamma_url"], cfg["clob_url"]
    starting = float(cfg["starting_capital"])
    max_dd = float(cfg.get("max_drawdown", 0.15))
    max_gross = float(cfg.get("max_gross_fraction", 0.45))
    max_event = float(cfg.get("max_event_fraction", 0.08))
    now = int(time.time())
    args.run_dir.mkdir(parents=True, exist_ok=True)

    state_path = args.run_dir / "state.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {
            "cash": starting,
            "peak": starting,
            "killed": False,
            "bundles": {},
            "realized_pnl": 0.0,
        }
    )
    cash = finite(state.get("cash"), starting)
    peak = max(starting, finite(state.get("peak"), starting))
    open_bundles = state.get("bundles") if isinstance(state.get("bundles"), dict) else {}
    realized = finite(state.get("realized_pnl"), 0.0)
    failures: list[str] = []
    scanned = 0
    candidates = 0
    entered = 0
    best_edge = 0.0
    fee_sources: Counter[str] = Counter()
    fee_unavailable = 0

    # One YES share in every outcome of a verified complete non-augmented NegRisk
    # event pays exactly $1. Until resolution, however, equity is conservatively
    # marked at cost basis; locked profit is reported separately and not booked.
    for event_id, bundle in list(open_bundles.items()):
        try:
            event = get_json(f"{gamma}/events/{event_id}")
            if isinstance(event, dict) and event.get("closed"):
                payout = float(bundle["shares"])
                cash += payout
                pnl = payout - float(bundle["cost"])
                realized += pnl
                append_csv(
                    args.run_dir / "fills.csv",
                    [
                        "timestamp",
                        "event_id",
                        "action",
                        "shares",
                        "cost",
                        "payout",
                        "net_edge",
                        "pnl",
                    ],
                    {
                        "timestamp": now,
                        "event_id": event_id,
                        "action": "SETTLE",
                        "shares": bundle["shares"],
                        "cost": bundle["cost"],
                        "payout": payout,
                        "net_edge": bundle["net_edge"],
                        "pnl": pnl,
                    },
                )
                del open_bundles[event_id]
        except Exception as exc:
            if len(failures) < 20:
                failures.append(f"settle:{event_id}:{type(exc).__name__}")

    locked_cost = sum(float(bundle["cost"]) for bundle in open_bundles.values())
    locked_profit = sum(
        float(bundle["shares"]) - float(bundle["cost"])
        for bundle in open_bundles.values()
    )
    equity = cash + locked_cost
    peak = max(peak, equity)
    drawdown = max(0.0, 1 - equity / peak) if peak else 0.0
    killed = bool(state.get("killed")) or drawdown >= max_dd

    if not killed:
        try:
            event_ids = discover_event_ids(
                gamma,
                args.markets,
                args.min_liquidity,
                args.max_events,
            )
        except Exception as exc:
            event_ids = []
            failures.append(f"discover:{type(exc).__name__}:{exc}")

        slip = max(0.0, args.slippage_bps) / 10000.0
        for event_id in event_ids:
            if event_id in open_bundles:
                continue
            try:
                markets = event_spec(gamma, event_id)
                if markets is None:
                    continue

                tokens: list[str] = []
                fees: list[FeeDetails] = []
                for market in markets:
                    yes, _ = market_tokens(market) or ("", "")
                    tokens.append(yes)
                    details = resolve_fee_details(market, clob, get_json)
                    fee_sources[details.source] += 1
                    fees.append(details)

                live_books = books(clob, tokens)
                if any(token not in live_books for token in tokens):
                    continue

                scanned += 1
                raw = sum(live_books[token]["ask"] for token in tokens)
                prices = [
                    min(0.999999, live_books[token]["ask"] * (1 + slip))
                    for token in tokens
                ]
                cost_per_share = sum(
                    price + fee_per_share(price, fees[i])
                    for i, price in enumerate(prices)
                )
                edge = 1.0 - cost_per_share
                best_edge = max(best_edge, edge)
                candidates += int(edge > 0)
                if edge <= args.min_edge:
                    continue

                min_size = min(live_books[token]["size"] for token in tokens)
                min_order = max(live_books[token]["min_order"] for token in tokens)
                eq = max(1.0, equity)
                room = min(
                    args.max_trade_usd,
                    max(0.0, max_gross * eq - locked_cost),
                    max(0.0, max_event * eq),
                    cash,
                )
                shares = min(min_size, room / max(cost_per_share, 1e-9))
                if shares + 1e-12 < min_order:
                    continue

                cost = shares * cost_per_share
                if cost > cash + 1e-9:
                    continue

                # All-or-none paper admission: every leg has displayed touch depth
                # for the identical share count in the same fetched snapshot.
                cash -= cost
                open_bundles[event_id] = {
                    "shares": shares,
                    "cost": cost,
                    "net_edge": edge,
                    "raw_edge": 1 - raw,
                    "opened_ts": now,
                    "legs": len(markets),
                }
                locked_cost += cost
                locked_profit += shares - cost
                equity = cash + locked_cost
                entered += 1
                append_csv(
                    args.run_dir / "fills.csv",
                    [
                        "timestamp",
                        "event_id",
                        "action",
                        "shares",
                        "cost",
                        "payout",
                        "net_edge",
                        "pnl",
                    ],
                    {
                        "timestamp": now,
                        "event_id": event_id,
                        "action": "BUY_COMPLETE_YES_SET",
                        "shares": shares,
                        "cost": cost,
                        "payout": shares,
                        "net_edge": edge,
                        "pnl": 0.0,
                    },
                )
            except Exception as exc:
                if isinstance(exc, FeeScheduleUnavailable):
                    fee_unavailable += 1
                if len(failures) < 20:
                    failures.append(f"event:{event_id}:{type(exc).__name__}")

    peak = max(peak, equity)
    drawdown = max(0.0, 1 - equity / peak) if peak else 0.0
    killed = killed or drawdown >= max_dd
    state = {
        "timestamp": now,
        "cash": cash,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "killed": killed,
        "bundles": open_bundles,
        "gross_exposure": locked_cost,
        "open_positions": len(open_bundles),
        "realized_pnl": realized,
        "locked_expected_profit": locked_profit,
        "scanned_events": scanned,
        "positive_candidates": candidates,
        "entered": entered,
        "best_edge": best_edge,
        "fee_sources_last_tick": dict(fee_sources),
        "fee_unavailable_last_tick": fee_unavailable,
        "failures": failures,
        "paper_only": True,
        "atomic_snapshot_assumption": True,
        "marking": "cost_basis_until_resolution",
    }
    atomic_json(state_path, state)
    atomic_json(args.run_dir / "status.json", state)
    append_csv(
        args.run_dir / "equity.csv",
        [
            "timestamp",
            "cash",
            "equity",
            "drawdown",
            "gross_exposure",
            "open_positions",
            "locked_expected_profit",
            "scanned_events",
            "positive_candidates",
            "entered",
            "best_edge",
        ],
        state,
    )
    print(
        json.dumps(
            {
                key: state[key]
                for key in (
                    "equity",
                    "drawdown",
                    "open_positions",
                    "locked_expected_profit",
                    "scanned_events",
                    "positive_candidates",
                    "entered",
                    "best_edge",
                    "killed",
                )
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
