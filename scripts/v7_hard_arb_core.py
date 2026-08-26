#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from polymarket_fees import (
    FeeDetails,
    parse_fee_details,
    resolve_fee_details,
)


# Canonical V7 PAPER hard-arbitrage core.
#
# This module intentionally owns all primitives required by v7_hard_arb_guard.
# It has no dependency on V3/V4/V5/V6 runtime code.  The public-market reads,
# full-depth paper execution, sequential leg revalidation and fail-closed unwind
# semantics are preserved from the proven predecessor while the V7 guard adds
# receive/exchange-time freshness and authoritative-fee requirements.


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def arr(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def get_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": "polymarket-v7-hard-arb-paper/1",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def atomic_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def market_tokens(raw: dict[str, Any]) -> tuple[str, str] | None:
    ids = [str(x) for x in arr(raw.get("clobTokenIds"))]
    outcomes = [str(x).lower() for x in arr(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yes_index, no_index = 0, 1
    for index, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == "yes":
            yes_index = index
        elif outcome == "no":
            no_index = index
    if yes_index >= len(ids) or no_index >= len(ids) or yes_index == no_index:
        return None
    return ids[yes_index], ids[no_index]


def discover_event_ids(
    gamma: str,
    market_limit: int,
    min_liq: float,
    max_events: int,
) -> list[str]:
    """Discover NegRisk event ids inside a bounded market universe."""
    ids: list[str] = []
    offset = 0
    remaining = max(0, int(market_limit))
    event_cap = max(0, int(max_events))
    while remaining > 0 and offset < 5000 and len(ids) < event_cap:
        page_size = min(100, remaining)
        query = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
                "order": "liquidityNum",
                "ascending": "false",
            }
        )
        raw = get_json(gamma.rstrip("/") + "/markets?" + query)
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
            if finite(market.get("liquidityNum"), finite(market.get("liquidity"), 0.0)) < min_liq:
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


def event_spec(gamma: str, event_id: str) -> list[dict[str, Any]] | None:
    event = get_json(f"{gamma.rstrip('/')}/events/{event_id}")
    if (
        not isinstance(event, dict)
        or not event.get("negRisk")
        or event.get("negRiskAugmented")
    ):
        return None
    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) < 2:
        return None
    clean: list[dict[str, Any]] = []
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


@dataclass(frozen=True)
class BookFill:
    requested_shares: float
    filled_shares: float
    raw_cash: float
    stressed_cash: float
    fee: float
    raw_vwap: float
    stressed_vwap: float
    slippage_cost: float
    complete: bool

    @property
    def all_in_unit_price(self) -> float:
        if self.filled_shares <= 0:
            return math.nan
        return (self.stressed_cash + self.fee) / self.filled_shares


def round_fee_usdc(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def fee_amount(
    shares: float,
    price: float,
    details: FeeDetails,
    taker: bool,
) -> float:
    if not taker or not details.enabled or shares <= 0 or not (0 < price < 1):
        return 0.0
    rate = max(0.0, finite(details.rate, 0.0))
    exponent = max(0.0, finite(details.exponent, 0.0))
    if rate <= 0:
        return 0.0
    fee = shares * rate * ((price * (1.0 - price)) ** exponent)
    return round_fee_usdc(fee)


def _levels(raw: Iterable[Any]) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, dict):
            price, size = finite(item.get("price")), finite(item.get("size"), 0.0)
        elif isinstance(item, Sequence) and len(item) >= 2:
            price, size = finite(item[0]), finite(item[1], 0.0)
        else:
            continue
        if math.isfinite(price) and 0 < price < 1 and size > 0:
            levels.append((price, size))
    return levels


def walk_book_for_shares(
    levels: Iterable[Any],
    shares: float,
    details: FeeDetails,
    *,
    buy: bool,
    slippage_bps: float = 0.0,
    require_full: bool = True,
) -> BookFill | None:
    requested = max(0.0, finite(shares, 0.0))
    if requested <= 0:
        return None
    parsed = _levels(levels)
    parsed.sort(key=lambda x: x[0], reverse=not buy)
    remaining = requested
    filled = raw_cash = 0.0
    for price, size in parsed:
        take = min(remaining, size)
        if take <= 0:
            continue
        filled += take
        raw_cash += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0 or (require_full and remaining > 1e-9):
        return None
    raw_vwap = raw_cash / filled
    stress = max(0.0, finite(slippage_bps, 0.0)) / 10000.0
    stressed_vwap = min(1.0, raw_vwap * (1.0 + stress)) if buy else max(0.0, raw_vwap * (1.0 - stress))
    stressed_cash = filled * stressed_vwap
    fee = fee_amount(filled, stressed_vwap, details, taker=True)
    return BookFill(
        requested_shares=requested,
        filled_shares=filled,
        raw_cash=raw_cash,
        stressed_cash=stressed_cash,
        fee=fee,
        raw_vwap=raw_vwap,
        stressed_vwap=stressed_vwap,
        slippage_cost=abs(stressed_cash - raw_cash),
        complete=remaining <= 1e-9,
    )


def _parse_book(raw: dict[str, Any]) -> dict[str, Any] | None:
    token = str(raw.get("asset_id") or "")
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        for item in raw.get(key, []):
            if not isinstance(item, dict):
                continue
            price, qty = finite(item.get("price")), finite(item.get("size"), 0.0)
            if math.isfinite(price) and 0 < price < 1 and qty > 0:
                output.append((price, qty))
    bids.sort(reverse=True)
    asks.sort()
    if not token or not asks:
        return None
    return {
        "token": token,
        "bids": bids,
        "asks": asks,
        "ask": asks[0][0],
        "size": asks[0][1],
        "min_order": max(1.0, finite(raw.get("min_order_size"), 1.0)),
        "ask_depth": sum(qty for _, qty in asks),
    }


def books(clob: str, tokens: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(tokens), 80):
        raw = get_json(
            clob.rstrip("/") + "/books",
            [{"token_id": token} for token in tokens[offset : offset + 80]],
        )
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            book = _parse_book(item)
            if book:
                out[str(book["token"])] = book
    return out


def _hard_fee(
    raw: dict[str, Any],
    clob: str,
    cfg: dict[str, Any],
    sources: Counter[str],
) -> FeeDetails:
    del cfg
    details = resolve_fee_details(raw, clob, get_json)
    sources[details.source] += 1
    return details


def _plan(
    live: dict[str, dict[str, Any]],
    tokens: list[str],
    fees: dict[str, FeeDetails],
    shares: float,
    slip: float,
) -> tuple[float, float, list[dict[str, Any]]] | None:
    cost = raw_cost = 0.0
    fills: list[dict[str, Any]] = []
    for token in tokens:
        book = live.get(token)
        if book is None:
            return None
        fill = walk_book_for_shares(
            book["asks"],
            shares,
            fees[token],
            buy=True,
            slippage_bps=slip,
            require_full=True,
        )
        if fill is None:
            return None
        all_in_cost = fill.stressed_cash + fill.fee
        cost += all_in_cost
        raw_cost += fill.raw_cash
        fills.append(
            {
                "token": token,
                "shares": fill.filled_shares,
                "raw_vwap": fill.raw_vwap,
                "price": fill.stressed_vwap,
                "fee": fill.fee,
                "cost": all_in_cost,
                "slippage": fill.slippage_cost,
            }
        )
    return cost, raw_cost, fills


def _size(
    live: dict[str, dict[str, Any]],
    tokens: list[str],
    fees: dict[str, FeeDetails],
    min_order: float,
    max_shares: float,
    room: float,
    edge_gate: float,
    slip: float,
):
    if room <= 0 or max_shares + 1e-12 < min_order:
        return None
    plan = _plan(live, tokens, fees, min_order, slip)
    if plan is None or plan[0] > room + 1e-9 or 1.0 - plan[0] / min_order <= edge_gate:
        return None
    low, high = min_order, max_shares
    best = (low, *plan)
    for _ in range(34):
        candidate = (low + high) / 2.0
        plan = _plan(live, tokens, fees, candidate, slip)
        if (
            plan
            and plan[0] <= room + 1e-9
            and 1.0 - plan[0] / candidate > edge_gate
        ):
            best = (candidate, *plan)
            low = candidate
        else:
            high = candidate
    return best


def _unwind(
    clob: str,
    legs: list[dict[str, Any]],
    fees: dict[str, FeeDetails],
    slip: float,
):
    residual: list[dict[str, Any]] = []
    received = pnl = 0.0
    for leg in reversed(legs):
        token = str(leg["token"])
        shares = max(0.0, finite(leg.get("shares"), 0.0))
        cost_basis = max(0.0, finite(leg.get("cost"), 0.0))
        try:
            book = books(clob, [token]).get(token)
        except Exception:
            book = None
        if not book or not book.get("bids"):
            residual.append(dict(leg))
            continue
        fill = walk_book_for_shares(
            book["bids"],
            shares,
            fees[token],
            buy=False,
            slippage_bps=slip,
            require_full=False,
        )
        if fill is None:
            residual.append(dict(leg))
            continue
        sold = fill.filled_shares
        allocated_basis = cost_basis * min(1.0, sold / max(shares, 1e-12))
        proceeds = fill.stressed_cash - fill.fee
        received += proceeds
        pnl += proceeds - allocated_basis
        remaining = max(0.0, shares - sold)
        if remaining > 1e-9:
            rest = dict(leg)
            rest["shares"] = remaining
            rest["cost"] = max(0.0, cost_basis - allocated_basis)
            residual.append(rest)
    residual.reverse()
    return residual, received, pnl


def _abort_mark(clob: str, aborting: dict[str, Any]) -> float:
    value = 0.0
    for bundle in aborting.values():
        for leg in bundle.get("legs", []):
            token = str(leg.get("token") or "")
            shares = max(0.0, finite(leg.get("shares"), 0.0))
            try:
                book = books(clob, [token]).get(token)
                bid = book["bids"][0][0] if book and book.get("bids") else math.nan
            except Exception:
                bid = math.nan
            if math.isfinite(bid):
                value += shares * bid
    return value


def _hard_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=500)
    parser.add_argument("--min-liquidity", type=float, default=10)
    parser.add_argument("--max-events", type=int, default=80)
    parser.add_argument("--min-edge", type=float, default=0.0002)
    parser.add_argument("--max-trade-usd", type=float, default=60)
    parser.add_argument("--slippage-bps", type=float, default=5)
    parser.add_argument("--leg-latency-ms", type=int, default=100)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = cfg["gamma_url"], cfg["clob_url"]
    starting_capital = float(cfg["starting_capital"])
    max_drawdown = float(cfg.get("max_drawdown", 0.15))
    max_gross = float(cfg.get("max_gross_fraction", 0.45))
    max_event = float(cfg.get("max_event_fraction", 0.08))
    now = int(time.time())
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {
            "cash": starting_capital,
            "peak": starting_capital,
            "killed": False,
            "bundles": {},
            "aborting": {},
            "realized_pnl": 0.0,
        }
    )
    cash = finite(state.get("cash"), starting_capital)
    peak = max(starting_capital, finite(state.get("peak"), starting_capital))
    realized = finite(state.get("realized_pnl"), 0.0)
    open_bundles = state.get("bundles") if isinstance(state.get("bundles"), dict) else {}
    aborting = state.get("aborting") if isinstance(state.get("aborting"), dict) else {}
    failures: list[str] = []
    sources: Counter[str] = Counter()
    scanned = positive = entered = sequential_aborts = 0
    best_edge = 0.0

    for event_id, bundle in list(open_bundles.items()):
        try:
            event = get_json(f"{gamma.rstrip('/')}/events/{event_id}")
            if isinstance(event, dict) and event.get("closed"):
                payout = float(bundle["shares"])
                cash += payout
                pnl = payout - float(bundle["cost"])
                realized += pnl
                append_csv(
                    args.run_dir / "fills.csv",
                    ["timestamp", "event_id", "action", "shares", "cost", "payout", "net_edge", "pnl"],
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

    for event_id, bundle in list(aborting.items()):
        legs = bundle.get("legs") if isinstance(bundle.get("legs"), list) else []
        fee_map: dict[str, FeeDetails] = {}
        for leg in legs:
            details = _hard_fee(
                leg.get("market") if isinstance(leg.get("market"), dict) else {},
                clob,
                cfg,
                sources,
            )
            fee_map[str(leg["token"])] = details
        residual, received, unwind_pnl = _unwind(clob, legs, fee_map, args.slippage_bps)
        cash += received
        realized += unwind_pnl
        if residual:
            bundle["legs"] = residual
            bundle["cost"] = sum(finite(item.get("cost"), 0.0) for item in residual)
        else:
            del aborting[event_id]

    locked = sum(float(bundle["cost"]) for bundle in open_bundles.values())
    abort_cost = sum(float(bundle.get("cost") or 0.0) for bundle in aborting.values())
    locked_profit = sum(float(bundle["shares"]) - float(bundle["cost"]) for bundle in open_bundles.values())
    equity = cash + locked + (_abort_mark(clob, aborting) if aborting else 0.0)
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = bool(state.get("killed")) or drawdown >= max_drawdown

    if not killed and not aborting:
        try:
            event_ids = discover_event_ids(gamma, args.markets, args.min_liquidity, args.max_events)
        except Exception as exc:
            event_ids = []
            failures.append(f"discover:{type(exc).__name__}:{exc}")
        for event_id in event_ids:
            if event_id in open_bundles:
                continue
            try:
                markets = event_spec(gamma, event_id)
                if markets is None:
                    continue
                tokens: list[str] = []
                fee_map: dict[str, FeeDetails] = {}
                market_by_token: dict[str, dict[str, Any]] = {}
                for market in markets:
                    yes_token, _ = market_tokens(market) or ("", "")
                    if not yes_token:
                        raise RuntimeError("missing_yes_token")
                    tokens.append(yes_token)
                    market_by_token[yes_token] = market
                    fee_map[yes_token] = _hard_fee(market, clob, cfg, sources)
                live = books(clob, tokens)
                if any(token not in live for token in tokens):
                    continue
                scanned += 1

                # Initial all-or-none FOK sizing uses the full displayed depth.
                # PAPER fills below remain sequential and every remaining leg is
                # re-read/revalidated immediately before the next fill.
                min_size = min(float(live[token]["ask_depth"]) for token in tokens)
                min_order = max(float(live[token]["min_order"]) for token in tokens)
                effective_equity = max(1.0, equity)
                room = min(
                    args.max_trade_usd,
                    max(0.0, max_gross * effective_equity - locked - abort_cost),
                    max(0.0, max_event * effective_equity),
                    cash,
                )
                initial_plan = (
                    _plan(live, tokens, fee_map, min_order, args.slippage_bps)
                    if min_size + 1e-12 >= min_order
                    else None
                )
                if initial_plan:
                    cost_per_share = initial_plan[0] / min_order
                    edge = 1.0 - cost_per_share
                    best_edge = max(best_edge, edge)
                    positive += int(edge > 0)
                sized = _size(
                    live,
                    tokens,
                    fee_map,
                    min_order,
                    min_size,
                    room,
                    args.min_edge,
                    args.slippage_bps,
                )
                if sized is None:
                    continue
                shares = sized[0]
                order = sorted(tokens, key=lambda token: float(live[token]["ask_depth"]))
                filled: list[dict[str, Any]] = []
                execution_cost = 0.0
                failure = ""
                for leg_index, token in enumerate(order):
                    if leg_index and args.leg_latency_ms > 0:
                        time.sleep(args.leg_latency_ms / 1000.0)
                    remaining = order[leg_index:]
                    fresh = books(clob, remaining)
                    if any(item not in fresh for item in remaining):
                        failure = "book_missing"
                        break
                    remaining_plan = _plan(
                        fresh,
                        remaining,
                        fee_map,
                        shares,
                        args.slippage_bps,
                    )
                    if remaining_plan is None:
                        failure = "fok_depth"
                        break
                    guaranteed_edge = 1.0 - (execution_cost + remaining_plan[0]) / shares
                    if guaranteed_edge <= args.min_edge:
                        failure = "edge_revalidation"
                        break
                    current_fill = next(item for item in remaining_plan[2] if item["token"] == token)
                    leg_cost = float(current_fill["cost"])
                    if leg_cost > cash + 1e-9:
                        failure = "capital"
                        break
                    cash -= leg_cost
                    execution_cost += leg_cost
                    filled.append({**current_fill, "market": market_by_token[token]})
                    append_csv(
                        args.run_dir / "leg_fills.csv",
                        ["timestamp", "event_id", "action", "token", "shares", "price", "fee", "cost", "detail"],
                        {
                            "timestamp": int(time.time()),
                            "event_id": event_id,
                            "action": "BUY_LEG_FOK",
                            "token": token,
                            "shares": shares,
                            "price": current_fill["price"],
                            "fee": current_fill["fee"],
                            "cost": leg_cost,
                            "detail": f"leg={leg_index + 1}/{len(order)} edge={guaranteed_edge:.8f}",
                        },
                    )
                if failure:
                    sequential_aborts += 1
                    residual, received, unwind_pnl = _unwind(
                        clob,
                        filled,
                        fee_map,
                        args.slippage_bps,
                    )
                    cash += received
                    realized += unwind_pnl
                    if residual:
                        aborting[event_id] = {
                            "shares": shares,
                            "cost": sum(float(item["cost"]) for item in residual),
                            "legs": residual,
                            "reason": failure,
                            "opened_ts": now,
                        }
                    if aborting:
                        break
                    continue

                edge = 1.0 - execution_cost / shares
                if edge <= args.min_edge:
                    raise RuntimeError("post_execution_edge")
                open_bundles[event_id] = {
                    "shares": shares,
                    "cost": execution_cost,
                    "net_edge": edge,
                    "raw_edge": 1.0 - sum(float(item["raw_vwap"]) for item in filled),
                    "opened_ts": now,
                    "legs": len(markets),
                    "leg_latency_ms": args.leg_latency_ms,
                }
                locked += execution_cost
                locked_profit += shares - execution_cost
                equity = cash + locked
                entered += 1
                best_edge = max(best_edge, edge)
                append_csv(
                    args.run_dir / "fills.csv",
                    ["timestamp", "event_id", "action", "shares", "cost", "payout", "net_edge", "pnl"],
                    {
                        "timestamp": int(time.time()),
                        "event_id": event_id,
                        "action": "BUY_COMPLETE_YES_SET_SEQUENTIAL",
                        "shares": shares,
                        "cost": execution_cost,
                        "payout": shares,
                        "net_edge": edge,
                        "pnl": 0.0,
                    },
                )
            except Exception as exc:
                if len(failures) < 20:
                    failures.append(f"event:{event_id}:{type(exc).__name__}:{exc}")

    locked = sum(float(bundle["cost"]) for bundle in open_bundles.values())
    abort_cost = sum(float(bundle.get("cost") or 0.0) for bundle in aborting.values())
    locked_profit = sum(float(bundle["shares"]) - float(bundle["cost"]) for bundle in open_bundles.values())
    equity = cash + locked + (_abort_mark(clob, aborting) if aborting else 0.0)
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = killed or drawdown >= max_drawdown
    out = {
        "timestamp": int(time.time()),
        "cash": cash,
        "equity": equity,
        "peak": peak,
        "drawdown": drawdown,
        "killed": killed,
        "bundles": open_bundles,
        "aborting": aborting,
        "gross_exposure": locked + abort_cost,
        "open_positions": len(open_bundles),
        "aborting_bundles": len(aborting),
        "realized_pnl": realized,
        "locked_expected_profit": locked_profit,
        "scanned_events": scanned,
        "positive_candidates": positive,
        "entered": entered,
        "sequential_aborts": sequential_aborts,
        "best_edge": best_edge,
        "failures": failures,
        "fee_sources_last_tick": dict(sources),
        "paper_only": True,
        "authenticated_execution": False,
        "atomic_snapshot_assumption": False,
        "sequential_leg_revalidation": True,
        "leg_latency_ms": args.leg_latency_ms,
        "marking": "complete_sets_cost_basis_abort_legs_bid_mark",
    }
    atomic_json(state_path, out)
    atomic_json(args.run_dir / "status.json", out)
    print(
        f"hard_exec scanned={scanned} positive={positive} entered={entered} "
        f"sequential_aborts={sequential_aborts} aborting={len(aborting)} "
        f"best_edge={best_edge:.8f} realized={realized:.6f}"
    )
    return 0


def self_test() -> int:
    disabled = parse_fee_details({"feesEnabled": False})
    assert disabled is not None
    assert not disabled.enabled
    assert fee_amount(100, 0.5, disabled, True) == 0.0
    active = FeeDetails(True, 0.07, 1.0, True, "selftest")
    assert fee_amount(100, 0.5, active, True) == 1.75
    assert fee_amount(100, 0.5, active, False) == 0.0
    fill = walk_book_for_shares(
        [(0.50, 5), (0.60, 5)],
        8,
        active,
        buy=True,
        slippage_bps=5,
        require_full=True,
    )
    assert fill is not None and fill.complete
    assert fill.raw_vwap > 0.50
    assert fill.all_in_unit_price > fill.stressed_vwap
    partial = walk_book_for_shares(
        [(0.49, 2)],
        5,
        active,
        buy=False,
        slippage_bps=5,
        require_full=False,
    )
    assert partial is not None and partial.filled_shares == 2 and not partial.complete
    print("v7_hard_arb_core_self_test=ok")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        return self_test()
    if len(sys.argv) > 1 and sys.argv[1] == "hard":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
    return _hard_main()


if __name__ == "__main__":
    raise SystemExit(main())
