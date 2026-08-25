#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

try:
    from v6_market_common import (
        TapeFlow,
        fee_per_share,
        fill_probability_proxy,
        finite,
        parse_array,
        request_json,
        resolve_fee_details,
    )
except ModuleNotFoundError:
    from scripts.v6_market_common import (
        TapeFlow,
        fee_per_share,
        fill_probability_proxy,
        finite,
        parse_array,
        request_json,
        resolve_fee_details,
    )


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in fields} for row in rows])
    os.replace(tmp, path)


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


class Market:
    def __init__(self, raw: dict[str, Any]):
        ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
        outcomes = [str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
        if len(ids) < 2:
            raise ValueError("missing tokens")
        yi, ni = 0, 1
        for i, name in enumerate(outcomes[: len(ids)]):
            if name == "yes":
                yi = i
            elif name == "no":
                ni = i
        self.raw = raw
        self.id = str(raw.get("id") or "")
        self.condition = str(raw.get("conditionId") or "")
        self.event = str(raw.get("eventId") or self.condition or self.id)
        events = raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            self.event = str(events[0].get("id") or self.event)
        self.slug = str(raw.get("slug") or self.id)
        self.yes = ids[yi]
        self.no = ids[ni]
        self.liquidity = max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0)))


class Book:
    def __init__(self, raw: dict[str, Any]):
        self.token = str(raw.get("asset_id") or "")
        self.tick = max(1e-6, finite(raw.get("tick_size"), 0.01))
        self.min_order = max(1.0, finite(raw.get("min_order_size"), 1.0))
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        for key, values in (("bids", self.bids), ("asks", self.asks)):
            for row in raw.get(key, []):
                if not isinstance(row, dict):
                    continue
                price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
                if math.isfinite(price) and 0 < price < 1 and size > 0:
                    values.append((price, size))
        self.bids.sort(reverse=True)
        self.asks.sort()

    @property
    def bid(self) -> float:
        return self.bids[0][0] if self.bids else math.nan

    @property
    def ask(self) -> float:
        return self.asks[0][0] if self.asks else math.nan

    @property
    def spread(self) -> float:
        return self.ask - self.bid if math.isfinite(self.ask) and math.isfinite(self.bid) else math.nan

    @property
    def mid(self) -> float:
        return 0.5 * (self.ask + self.bid) if math.isfinite(self.ask) and math.isfinite(self.bid) else math.nan

    def touch_size(self, bid_side: bool) -> float:
        levels = self.bids if bid_side else self.asks
        if not levels:
            return 0.0
        best = levels[0][0]
        return sum(size for price, size in levels if abs(price - best) <= 1e-12)

    def depth(self, bid_side: bool, n: int = 5) -> float:
        levels = self.bids if bid_side else self.asks
        if not levels:
            return 0.0
        best = levels[0][0]
        scale = max(1e-4, 3 * self.tick)
        return sum(size * math.exp(-abs(price - best) / scale) for price, size in levels[:n])

    def micro(self) -> float:
        db, da = self.depth(True), self.depth(False)
        if not math.isfinite(self.bid) or not math.isfinite(self.ask):
            return math.nan
        return (self.ask * db + self.bid * da) / (db + da) if db + da > 1e-12 else self.mid


def discover(gamma: str, limit: int, min_liquidity: float) -> list[Market]:
    output: list[Market] = []
    offset = 0
    while len(output) < limit and offset < 5000:
        query = urllib.parse.urlencode(
            {"active": "true", "closed": "false", "limit": 100, "offset": offset, "order": "liquidityNum", "ascending": "false"}
        )
        root = request_json(gamma.rstrip("/") + "/markets?" + query)
        batch = root if isinstance(root, list) else root.get("markets", []) if isinstance(root, dict) else []
        if not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            try:
                market = Market(raw)
            except ValueError:
                continue
            if market.id and market.condition and market.liquidity >= min_liquidity:
                output.append(market)
            if len(output) >= limit:
                break
        if len(batch) < 100:
            break
        offset += 100
    return output


def fetch_books(clob: str, markets: list[Market]) -> dict[str, Book]:
    tokens = [token for market in markets for token in (market.yes, market.no)]
    output: dict[str, Book] = {}
    for i in range(0, len(tokens), 80):
        root = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in tokens[i : i + 80]])
        for raw in root if isinstance(root, list) else []:
            if not isinstance(raw, dict):
                continue
            book = Book(raw)
            if book.token and book.bids and book.asks:
                output[book.token] = book
    return output


def micro_signal(yes: Book, no: Book) -> tuple[float, float]:
    mid = yes.mid
    if not math.isfinite(mid):
        return 0.5, 0.0
    y, n = yes.micro(), no.micro()
    dy = yes.depth(True) + yes.depth(False)
    dn = no.depth(True) + no.depth(False)
    wy = math.sqrt(max(0.0, dy)) / (1.0 + 20.0 * max(0.0, yes.spread))
    wn = math.sqrt(max(0.0, dn)) / (1.0 + 20.0 * max(0.0, no.spread))
    q = mid
    if math.isfinite(y) and math.isfinite(n) and wy + wn > 1e-12:
        q = (wy * y + wn * (1.0 - n)) / (wy + wn)
    elif math.isfinite(y):
        q = y
    parity = abs(y - (1.0 - n)) if math.isfinite(y) and math.isfinite(n) else 0.25
    liquidity_conf = (dy + dn) / (dy + dn + 200.0)
    spread_conf = math.exp(-5.0 * (yes.spread + no.spread))
    confidence = max(0.02, min(1.0, liquidity_conf * spread_conf * math.exp(-8.0 * parity)))
    return max(0.001, min(0.999, q)), confidence


def load_tape_rows(path: Path, cutoff: int, now: int) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return [
                dict(row) for row in csv.DictReader(handle)
                if cutoff <= int(finite(row.get("timestamp"), 0.0)) <= now + 30
            ]
    except OSError:
        return []


def trade_key(row: dict[str, str]) -> str:
    return "|".join(
        str(row.get(key) or "") for key in ("transaction_hash", "asset_id", "timestamp", "side", "price", "size")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="V6 flow-aware micro maker paper engine")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=700)
    parser.add_argument("--min-liquidity", type=float, default=10)
    parser.add_argument("--min-edge", type=float, default=0.00035)
    parser.add_argument("--max-order-usd", type=float, default=25)
    parser.add_argument("--ttl-seconds", type=int, default=90)
    parser.add_argument("--hold-seconds", type=int, default=240)
    parser.add_argument("--adverse-selection-mult", type=float, default=0.15)
    parser.add_argument("--flow-lookback-seconds", type=int, default=900)
    parser.add_argument("--min-fill-probability", type=float, default=0.02)
    parser.add_argument("--target-fill-probability", type=float, default=0.15)
    parser.add_argument("--max-improve-ticks", type=int, default=2)
    parser.add_argument("--slippage-bps", type=float, default=5)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = cfg["gamma_url"], cfg["clob_url"]
    starting = float(cfg["starting_capital"])
    max_drawdown = float(cfg.get("max_drawdown", 0.15))
    now = int(time.time())
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {"cash": starting, "peak": starting, "killed": False, "orders": {}, "positions": {}}
    )
    cash = finite(state.get("cash"), starting)
    peak = max(starting, finite(state.get("peak"), starting))
    killed = bool(state.get("killed", False))
    orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    realized_pnl = finite(state.get("realized_pnl"), 0.0)

    try:
        markets = discover(gamma, args.markets, args.min_liquidity)
        books = fetch_books(clob, markets)
    except Exception as exc:
        markets, books = [], {}
        state["failure"] = f"market_data:{type(exc).__name__}:{exc}"
    by_id = {market.id: market for market in markets}
    flow = TapeFlow.from_csv(args.trade_tape, lookback_seconds=args.flow_lookback_seconds, now=now)
    tape_rows = load_tape_rows(args.trade_tape, now - max(args.flow_lookback_seconds, args.ttl_seconds + 30), now)

    order_log_fields = [
        "timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price",
        "remaining_shares", "queue_ahead", "signal_edge", "confidence", "fill_probability",
        "expected_fill_edge", "flow_rate", "fee_source",
    ]
    fill_fields = ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "pnl", "reason"]

    filled_orders = 0
    for market_id, order in list(orders.items()):
        if now - int(finite(order.get("created_ts"), now)) >= args.ttl_seconds:
            append_csv(args.run_dir / "maker_order_log.csv", order_log_fields, {**order, "timestamp": now, "action": "CANCEL_TTL"})
            del orders[market_id]
            continue
        seen = set(order.get("seen_trade_keys") or [])
        token = str(order.get("token_id") or "")
        limit_price = finite(order.get("limit_price"))
        for row in tape_rows:
            key = trade_key(row)
            if key in seen or str(row.get("asset_id") or "") != token:
                continue
            if str(row.get("side") or "").upper() != "SELL":
                continue
            price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
            if not math.isfinite(price) or price > limit_price + 1e-12 or size <= 0:
                continue
            seen.add(key)
            queue = max(0.0, finite(order.get("queue_ahead")))
            consumed_queue = min(queue, size)
            queue -= consumed_queue
            residual = size - consumed_queue
            fill_shares = min(max(0.0, finite(order.get("remaining_shares"))), max(0.0, residual))
            order["queue_ahead"] = queue
            if fill_shares <= 0:
                continue
            market = by_id.get(market_id)
            if market is None:
                continue
            details = resolve_fee_details(market.raw, clob, market.condition, token)
            if not details.verified:
                continue
            entry_fee = fill_shares * fee_per_share(limit_price, details, taker=False)
            fill_cost = fill_shares * limit_price + entry_fee
            if fill_cost > cash + 1e-9:
                continue
            cash -= fill_cost
            remaining = max(0.0, finite(order.get("remaining_shares")) - fill_shares)
            order["remaining_shares"] = remaining
            position = positions.get(market_id)
            if not isinstance(position, dict):
                position = {
                    "market_id": market_id, "event_id": market.event, "slug": market.slug,
                    "side": order["side"], "token_id": token, "shares": 0.0, "cost": 0.0,
                    "entry_ts": now,
                }
            position["shares"] = finite(position.get("shares")) + fill_shares
            position["cost"] = finite(position.get("cost")) + fill_cost
            positions[market_id] = position
            append_csv(
                args.run_dir / "maker_fills.csv", fill_fields,
                {
                    "timestamp": now, "market_id": market_id, "slug": market.slug,
                    "action": "BUY_MAKER" if remaining <= 1e-12 else "BUY_MAKER_PARTIAL",
                    "side": order["side"], "shares": fill_shares, "price": limit_price,
                    "fee": entry_fee, "pnl": 0.0, "reason": "taker_sell_consumed_queue",
                },
            )
            filled_orders += 1
            if remaining <= 1e-12:
                del orders[market_id]
                break
        if market_id in orders:
            order["seen_trade_keys"] = list(seen)[-500:]
            orders[market_id] = order

    slip = max(0.0, args.slippage_bps) / 10000.0
    for market_id, position in list(positions.items()):
        market = by_id.get(market_id)
        if market is None:
            continue
        book = books.get(position["token_id"])
        if book is None or not math.isfinite(book.bid):
            continue
        if now - int(finite(position.get("entry_ts"), now)) < args.hold_seconds and not killed:
            continue
        details = resolve_fee_details(market.raw, clob, market.condition, position["token_id"])
        if not details.verified:
            continue
        price = max(1e-6, book.bid * (1.0 - slip))
        shares = finite(position.get("shares"))
        fee = shares * fee_per_share(price, details, taker=True)
        proceeds = shares * price - fee
        pnl = proceeds - finite(position.get("cost"))
        cash += proceeds
        realized_pnl += pnl
        append_csv(
            args.run_dir / "maker_fills.csv", fill_fields,
            {
                "timestamp": now, "market_id": market_id, "slug": market.slug, "action": "SELL_TAKER",
                "side": position["side"], "shares": shares, "price": price, "fee": fee, "pnl": pnl,
                "reason": "hold_timeout" if not killed else "kill_switch",
            },
        )
        del positions[market_id]

    def reserved_cash() -> float:
        return sum(max(0.0, finite(order.get("remaining_shares")) * finite(order.get("limit_price"))) for order in orders.values())

    def position_mark() -> float:
        value = 0.0
        for position in positions.values():
            book = books.get(str(position.get("token_id") or ""))
            if book and math.isfinite(book.bid):
                value += finite(position.get("shares")) * book.bid
        return value

    equity = cash + position_mark()
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = killed or drawdown >= max_drawdown

    signals = posted = rejected_flow = fee_unverified = 0
    best_edge = best_expected_fill_edge = 0.0
    if not killed:
        for market in markets:
            if market.id in orders or market.id in positions:
                continue
            yes, no = books.get(market.yes), books.get(market.no)
            if yes is None or no is None or not math.isfinite(yes.mid):
                continue
            if yes.mid <= float(cfg.get("min_mid", 0.002)) or yes.mid >= float(cfg.get("max_mid", 0.998)):
                continue
            if yes.spread > float(cfg.get("max_spread", 0.50)) or no.spread > float(cfg.get("max_spread", 0.50)):
                continue
            q_yes, confidence = micro_signal(yes, no)
            if confidence < 0.10:
                continue
            details = resolve_fee_details(market.raw, clob, market.condition, market.yes)
            if not details.verified:
                fee_unverified += 1
                continue

            choices = []
            for side, book, token, fair in (("YES", yes, market.yes, q_yes), ("NO", no, market.no, 1.0 - q_yes)):
                if not math.isfinite(book.bid) or not math.isfinite(book.ask) or book.ask <= book.bid:
                    continue
                adverse = args.adverse_selection_mult * book.spread * (1.0 - confidence)
                max_cash = min(args.max_order_usd, cash - reserved_cash(), float(cfg.get("max_market_fraction", 0.025)) * max(equity, 1.0))
                if max_cash <= 0:
                    continue
                target_shares = max(book.min_order, min(max_cash / max(book.bid, 1e-9), 0.25 * max(1.0, book.touch_size(True))))
                price = book.bid

                def economics(limit: float) -> tuple[float, float, float]:
                    future_bid = max(0.001, min(0.999, fair - 0.5 * book.spread)) * (1.0 - slip)
                    edge = future_bid - fee_per_share(future_bid, details, taker=True) - limit - adverse
                    queue = book.touch_size(True) if abs(limit - book.bid) <= 0.25 * book.tick else 0.0
                    rate = flow.compatible_sell_rate(token, limit, lookback_seconds=args.flow_lookback_seconds)
                    fillp = fill_probability_proxy(
                        queue_ahead=queue, own_shares=target_shares, compatible_flow_per_second=rate,
                        horizon_seconds=args.ttl_seconds, prior_flow_per_second=1.0 / 300.0,
                    )
                    return edge, fillp, rate

                edge, fillp, rate = economics(price)
                ticks = 0
                while (
                    edge > args.min_edge and fillp < args.target_fill_probability
                    and ticks < args.max_improve_ticks and price + book.tick < book.ask - 1e-12
                ):
                    trial = price + book.tick
                    trial_edge, trial_fillp, trial_rate = economics(trial)
                    if trial_edge <= args.min_edge or trial_fillp <= fillp + 1e-9:
                        break
                    price, edge, fillp, rate = trial, trial_edge, trial_fillp, trial_rate
                    ticks += 1
                if edge > args.min_edge:
                    signals += 1
                    best_edge = max(best_edge, edge)
                    best_expected_fill_edge = max(best_expected_fill_edge, edge * fillp)
                    if fillp >= args.min_fill_probability:
                        choices.append((edge * fillp, edge, fillp, rate, side, book, token, price, target_shares, details, confidence))

            if not choices:
                rejected_flow += 1
                continue
            choice = max(choices, key=lambda item: (item[0], item[1]))
            expected_fill_edge, edge, fillp, rate, side, book, token, price, shares, details, confidence = choice
            available = max(0.0, cash - reserved_cash())
            event_committed = sum(
                finite(order.get("remaining_shares")) * finite(order.get("limit_price"))
                for order in orders.values() if str(order.get("event_id") or "") == market.event
            ) + sum(
                finite(position.get("cost")) for position in positions.values() if str(position.get("event_id") or "") == market.event
            )
            event_room = float(cfg.get("max_event_fraction", 0.08)) * max(equity, 1.0) - event_committed
            gross_room = float(cfg.get("max_gross_fraction", 0.45)) * max(equity, 1.0) - reserved_cash()
            room = min(args.max_order_usd, available, max(0.0, event_room), max(0.0, gross_room))
            shares = min(shares, room / max(price, 1e-9))
            if shares + 1e-12 < book.min_order:
                continue
            queue = book.touch_size(True) if abs(price - book.bid) <= 0.25 * book.tick else 0.0
            order = {
                "market_id": market.id, "event_id": market.event, "condition_id": market.condition, "slug": market.slug,
                "side": side, "token_id": token, "limit_price": price, "remaining_shares": shares,
                "queue_ahead": queue, "created_ts": now, "seen_trade_keys": [],
                "signal_edge": edge, "confidence": confidence, "fill_probability": fillp,
                "expected_fill_edge": expected_fill_edge, "flow_rate": rate, "fee_source": details.source,
            }
            orders[market.id] = order
            append_csv(args.run_dir / "maker_order_log.csv", order_log_fields, {**order, "timestamp": now, "action": "POST"})
            posted += 1

    equity = cash + position_mark()
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = killed or drawdown >= max_drawdown
    reserved = reserved_cash()
    state = {
        "timestamp": now, "cash": cash, "equity": equity, "peak": peak, "drawdown": drawdown, "killed": killed,
        "orders": orders, "positions": positions, "realized_pnl": realized_pnl,
        "signals": signals, "posted": posted, "resting_orders": len(orders), "open_positions": len(positions),
        "reserved_cash": reserved, "gross_exposure": reserved + max(0.0, equity - cash),
        "best_edge": best_edge, "best_expected_fill_edge": best_expected_fill_edge,
        "rejected_flow": rejected_flow, "fee_unverified_markets": fee_unverified,
        "fills_last_tick": filled_orders, "paper_only": True,
    }
    atomic_json(state_path, state)
    atomic_json(args.run_dir / "status.json", state)
    atomic_csv(
        args.run_dir / "maker_orders.csv",
        [
            "market_id", "event_id", "condition_id", "slug", "side", "token_id", "limit_price",
            "remaining_shares", "queue_ahead", "created_ts", "signal_edge", "confidence",
            "fill_probability", "expected_fill_edge", "flow_rate", "fee_source",
        ],
        list(orders.values()),
    )
    atomic_csv(
        args.run_dir / "maker_positions.csv",
        ["market_id", "event_id", "slug", "side", "token_id", "shares", "cost", "entry_ts"],
        list(positions.values()),
    )
    atomic_csv(
        args.run_dir / "maker_risk.csv",
        ["cash", "peak_equity", "killed", "realized_pnl"],
        [{"cash": cash, "peak_equity": peak, "killed": 1 if killed else 0, "realized_pnl": realized_pnl}],
    )
    append_csv(
        args.run_dir / "maker_equity.csv",
        ["timestamp", "cash", "equity", "reserved_cash", "resting_orders", "positions", "peak_equity", "drawdown", "killed", "realized_pnl", "signals", "posted", "best_edge", "best_expected_fill_edge"],
        {
            "timestamp": now, "cash": cash, "equity": equity, "reserved_cash": reserved,
            "resting_orders": len(orders), "positions": len(positions), "peak_equity": peak,
            "drawdown": drawdown, "killed": 1 if killed else 0, "realized_pnl": realized_pnl,
            "signals": signals, "posted": posted, "best_edge": best_edge,
            "best_expected_fill_edge": best_expected_fill_edge,
        },
    )
    print(json.dumps({
        "markets": len(markets), "signals": signals, "posted": posted, "resting": len(orders),
        "positions": len(positions), "fills": filled_orders, "reserved": reserved, "equity": equity,
        "best_edge": best_edge, "best_expected_fill_edge": best_expected_fill_edge,
        "rejected_flow": rejected_flow, "fee_unverified": fee_unverified, "killed": killed,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
