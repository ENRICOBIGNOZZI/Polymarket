#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import v6_micro_taker as micro
from hf_active_token_gate import choose_tokens, load_recent_stats
from v6_queue_filter import FeeDetails, fee_amount, resolve_fee_details, walk_book_for_shares


def finite(v: Any, d: float = math.nan) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return d
    return x if math.isfinite(x) else d


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def trade_key(row: dict[str, str]) -> str:
    tx = row.get("transaction_hash") or ""
    if tx:
        return tx + "|" + (row.get("asset_id") or "") + "|" + (row.get("timestamp") or "")
    return "|".join(
        str(row.get(k) or "")
        for k in ("condition_id", "asset_id", "timestamp", "side", "price", "size")
    )


def load_tape(path: Path, as_of_ms: int) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    out: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                received_ms = int(float(row.get("received_ms") or 0))
                event_ts = int(float(row.get("timestamp") or 0))
            except (TypeError, ValueError):
                continue
            if received_ms <= 0 or received_ms > as_of_ms or event_ts <= 0:
                continue
            out.append(row)
    out.sort(key=lambda r: (int(float(r.get("timestamp") or 0)), int(float(r.get("received_ms") or 0)), trade_key(r)))
    return out


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    micro.append_csv(path, fields, row)


def micro_signal(y: micro.Book, n: micro.Book) -> tuple[float, float] | None:
    mid = y.mid()
    if not math.isfinite(mid):
        return None
    ym, nm = y.micro(), n.micro()
    dy = y.depth(True) + y.depth(False)
    dn = n.depth(True) + n.depth(False)
    wy = math.sqrt(max(0.0, dy)) / (1.0 + 20.0 * max(0.0, y.spread()))
    wn = math.sqrt(max(0.0, dn)) / (1.0 + 20.0 * max(0.0, n.spread()))
    q = mid
    if math.isfinite(ym) and math.isfinite(nm) and wy + wn > 1e-12:
        q = (wy * ym + wn * (1.0 - nm)) / (wy + wn)
    elif math.isfinite(ym):
        q = ym
    parity = abs(ym - (1.0 - nm)) if math.isfinite(ym) and math.isfinite(nm) else 0.25
    liq = (dy + dn) / (dy + dn + 200.0)
    spread_conf = math.exp(-5.0 * (max(0.0, y.spread()) + max(0.0, n.spread())))
    conf = clamp(liq * spread_conf * math.exp(-8.0 * parity), 0.02, 1.0)
    return clamp(q, 0.001, 0.999), conf


def displayed_size(book: micro.Book, price: float, bid_side: bool = True) -> float:
    levels = book.bids if bid_side else book.asks
    tol = 0.25 * max(1e-8, book.tick)
    return sum(q for p, q in levels if abs(p - price) <= tol)


def touch_size(book: micro.Book) -> float:
    return book.bids[0][1] if book.bids else 0.0


@dataclass
class Order:
    market_id: str
    event_id: str
    condition_id: str
    slug: str
    side: str
    token: str
    limit: float
    shares: float
    queue: float
    created_ts: int
    fair: float
    edge: float
    confidence: float
    fee: FeeDetails
    seen: set[str] = field(default_factory=set)


@dataclass
class Position:
    market_id: str
    event_id: str
    condition_id: str
    slug: str
    side: str
    token: str
    shares: float
    entry_price: float
    entry_ts: int
    fee: FeeDetails


@dataclass
class FillLot:
    market_id: str
    token: str
    side: str
    entry_price: float
    shares: float
    entry_ts: int
    fee: FeeDetails
    marks_done: set[str] = field(default_factory=set)


class ActiveMaker:
    def __init__(self, args: argparse.Namespace):
        self.a = args
        self.cfg = json.loads(args.config.read_text())
        self.gamma = str(self.cfg["gamma_url"])
        self.clob = str(self.cfg["clob_url"])
        self.cash = float(args.starting_capital)
        self.peak = self.cash
        self.killed = False
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.fill_lots: list[FillLot] = []
        self.fee_cache: dict[str, FeeDetails] = {}
        self.counters: Counter[str] = Counter()
        self.realized_pnl = 0.0
        self.closed_shares = 0.0
        self.run_dir = args.run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.order_log = self.run_dir / "orders.csv"
        self.fill_log = self.run_dir / "fills.csv"
        self.mark_log = self.run_dir / "markouts.csv"
        self.equity_log = self.run_dir / "equity.csv"

    def fee_details(self, m: micro.Market) -> FeeDetails:
        if m.condition not in self.fee_cache:
            self.fee_cache[m.condition] = resolve_fee_details(
                {"conditionId": m.condition},
                self.clob,
                micro.request_json,
                float(m.fee_rate),
                float(m.fee_exp),
            )
        return self.fee_cache[m.condition]

    def log_order(self, ts: int, action: str, o: Order, *, pfill: float = 0.0, toxicity: float = 0.0, fill_ev: float = 0.0) -> None:
        append_csv(
            self.order_log,
            ["timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price", "remaining_shares", "queue_ahead", "signal_edge", "confidence", "pfill_proxy", "toxicity", "fill_ev_proxy"],
            {
                "timestamp": ts,
                "action": action,
                "market_id": o.market_id,
                "slug": o.slug,
                "side": o.side,
                "token_id": o.token,
                "limit_price": o.limit,
                "remaining_shares": o.shares,
                "queue_ahead": o.queue,
                "signal_edge": o.edge,
                "confidence": o.confidence,
                "pfill_proxy": pfill,
                "toxicity": toxicity,
                "fill_ev_proxy": fill_ev,
            },
        )
        self.counters[action] += 1

    def log_fill(self, ts: int, action: str, p: Position, shares: float, price: float, fee: float, pnl: float, reason: str) -> None:
        append_csv(
            self.fill_log,
            ["timestamp", "market_id", "slug", "action", "side", "shares", "price", "fee", "pnl", "reason"],
            {
                "timestamp": ts,
                "market_id": p.market_id,
                "slug": p.slug,
                "action": action,
                "side": p.side,
                "shares": shares,
                "price": price,
                "fee": fee,
                "pnl": pnl,
                "reason": reason,
            },
        )
        self.counters[action] += 1

    def reserved(self) -> float:
        return sum(max(0.0, o.shares * o.limit) for o in self.orders.values())

    def position_cost(self) -> float:
        return sum(max(0.0, p.shares * p.entry_price) for p in self.positions.values())

    def event_committed(self, event_id: str) -> float:
        return sum(o.shares * o.limit for o in self.orders.values() if o.event_id == event_id) + sum(
            p.shares * p.entry_price for p in self.positions.values() if p.event_id == event_id
        )

    def equity(self, books: dict[str, micro.Book]) -> float:
        e = self.cash
        for p in self.positions.values():
            b = books.get(p.token)
            px = b.bid() if b is not None else p.entry_price
            e += p.shares * (px if math.isfinite(px) else p.entry_price)
        return e

    def add_fill(self, o: Order, shares: float, fill_ts: int) -> None:
        p = self.positions.get(o.market_id)
        if p is None:
            p = Position(o.market_id, o.event_id, o.condition_id, o.slug, o.side, o.token, shares, o.limit, fill_ts, o.fee)
            self.positions[o.market_id] = p
        else:
            new = p.shares + shares
            p.entry_price = (p.shares * p.entry_price + shares * o.limit) / max(new, 1e-12)
            p.shares = new
            p.entry_ts = min(p.entry_ts, fill_ts)
        self.fill_lots.append(FillLot(o.market_id, o.token, o.side, o.limit, shares, fill_ts, o.fee))
        self.log_fill(fill_ts, "BUY_MAKER", p, shares, o.limit, 0.0, 0.0, "public_taker_sell_consumed_queue")

    def process_orders(self, now: int, now_ms: int, books: dict[str, micro.Book], tape: list[dict[str, str]], active_stats: dict[str, Any]) -> None:
        erase: set[str] = set()
        for mid, o in list(self.orders.items()):
            if self.killed:
                self.log_order(now, "CANCEL_KILL", o)
                erase.add(mid)
                continue
            active_until = o.created_ts + self.a.ttl_seconds
            for row in tape:
                key = trade_key(row)
                if key in o.seen:
                    continue
                event_ts = int(float(row.get("timestamp") or 0))
                if event_ts < o.created_ts or event_ts > active_until:
                    continue
                if (row.get("asset_id") or "") != o.token:
                    continue
                o.seen.add(key)
                if (row.get("side") or "").upper() != "SELL":
                    continue
                px, size = finite(row.get("price")), max(0.0, finite(row.get("size"), 0.0))
                tick = books[o.token].tick if o.token in books else 0.01
                if not math.isfinite(px) or size <= 0.0 or px > o.limit + 0.25 * max(1e-8, tick):
                    continue
                take_q = min(o.queue, size)
                o.queue -= take_q
                left = size - take_q
                if left <= 1e-12:
                    if take_q > 0:
                        self.log_order(now, "QUEUE_TRADE_DEPLETION", o)
                    continue
                fill = min(o.shares, left)
                if fill <= 1e-12:
                    continue
                fill_cost = fill * o.limit
                other_reserved = max(0.0, self.reserved() - o.shares * o.limit)
                if fill_cost + other_reserved > self.cash + 1e-9:
                    self.log_order(now, "CANCEL_CAPITAL", o)
                    erase.add(mid)
                    break
                self.cash -= fill_cost
                o.shares -= fill
                self.add_fill(o, fill, event_ts)
                self.log_order(now, "FILL" if o.shares <= 1e-12 else "PARTIAL_FILL", o)
                if o.shares <= 1e-12:
                    erase.add(mid)
                    break
            if mid in erase:
                continue
            b = books.get(o.token)
            if b is not None and now < active_until and o.queue > 0.0:
                visible = displayed_size(b, o.limit, True)
                if visible + 1e-12 < o.queue:
                    o.queue = max(0.0, visible)
                    self.log_order(now, "QUEUE_CANCEL_DEPLETION", o)
            if now >= active_until:
                self.log_order(now, "CANCEL_TTL", o)
                erase.add(mid)
                continue
            if self.a.activity_gate and now - o.created_ts >= self.a.dead_queue_grace_seconds:
                s = active_stats.get(o.token)
                if s is None:
                    pfill = 0.0
                else:
                    expected_sell = max(0.0, s.sell_shares) * max(0, active_until - now) / max(1, self.a.activity_lookback_seconds)
                    pfill = min(1.0, expected_sell / max(1e-12, o.queue + o.shares))
                if pfill < self.a.dead_queue_pfill:
                    self.log_order(now, "CANCEL_DEAD_FLOW", o, pfill=pfill)
                    erase.add(mid)
                    continue
            if b is not None:
                bb = b.bid()
                if math.isfinite(bb) and bb > o.limit + 0.5 * max(1e-6, b.tick):
                    self.log_order(now, "CANCEL_STALE", o)
                    erase.add(mid)
        for mid in erase:
            self.orders.pop(mid, None)

    def process_positions(self, now: int, books: dict[str, micro.Book], markets: dict[str, micro.Market]) -> None:
        for mid, p in list(self.positions.items()):
            if mid in self.orders:
                continue
            b = books.get(p.token)
            if b is None:
                continue
            exit_now = self.killed or now - p.entry_ts >= self.a.hold_seconds
            m = markets.get(mid)
            if m is not None:
                y, n = books.get(m.yes), books.get(m.no)
                if y is not None and n is not None:
                    sig = micro_signal(y, n)
                    if sig is not None:
                        q, _ = sig
                        fair = q if p.side == "YES" else 1.0 - q
                        bid = b.bid()
                        if math.isfinite(bid) and fair <= bid + 0.25 * max(0.0, b.spread()):
                            exit_now = True
            if not exit_now:
                continue
            f = walk_book_for_shares(b.bids, p.shares, p.fee, buy=False, slippage_bps=self.a.slippage_bps, require_full=False)
            if f is None or f.filled_shares <= 1e-12:
                continue
            sold = min(p.shares, f.filled_shares)
            alloc = sold * p.entry_price
            proceeds = f.stressed_cash - f.fee
            pnl = proceeds - alloc
            self.cash += proceeds
            p.shares -= sold
            self.realized_pnl += pnl
            self.closed_shares += sold
            reason = "drawdown_kill" if self.killed else "max_hold_or_micro_reversal"
            self.log_fill(now, "SELL_TAKER" if p.shares <= 1e-9 else "SELL_TAKER_PARTIAL", p, sold, f.stressed_vwap, f.fee, pnl, reason)
            if p.shares <= 1e-9:
                del self.positions[mid]

    def record_markouts(self, now: int, books: dict[str, micro.Book]) -> None:
        horizons = (45, 60, 300)
        for lot in self.fill_lots:
            b = books.get(lot.token)
            if b is None:
                continue
            for h in horizons:
                if now - lot.entry_ts < h:
                    continue
                for mult in (1.0, 1.5, 2.0):
                    key = f"{h}:{mult}"
                    if key in lot.marks_done:
                        continue
                    f = walk_book_for_shares(
                        b.bids,
                        lot.shares,
                        lot.fee,
                        buy=False,
                        slippage_bps=self.a.slippage_bps * mult,
                        require_full=True,
                    )
                    available = f is not None and f.filled_shares + 1e-9 >= lot.shares
                    exit_unit = (f.stressed_cash - f.fee) / lot.shares if available and f is not None else math.nan
                    mark = exit_unit - lot.entry_price if math.isfinite(exit_unit) else math.nan
                    append_csv(
                        self.mark_log,
                        ["observed_ts", "market_id", "token_id", "side", "entry_ts", "horizon_seconds", "cost_stress", "shares", "entry_price", "executable_exit_unit", "markout_per_share", "available"],
                        {
                            "observed_ts": now,
                            "market_id": lot.market_id,
                            "token_id": lot.token,
                            "side": lot.side,
                            "entry_ts": lot.entry_ts,
                            "horizon_seconds": h,
                            "cost_stress": mult,
                            "shares": lot.shares,
                            "entry_price": lot.entry_price,
                            "executable_exit_unit": exit_unit if math.isfinite(exit_unit) else "",
                            "markout_per_share": mark if math.isfinite(mark) else "",
                            "available": int(available),
                        },
                    )
                    lot.marks_done.add(key)

    def tick(self, allow_new_entries: bool) -> None:
        now_ms = int(time.time() * 1000)
        now = now_ms // 1000
        markets_list = micro.discover(self.gamma, self.a.markets, self.a.min_liquidity)
        markets = {m.id: m for m in markets_list}
        # Ensure books remain observable for positions/fill lots even if the liquidity ranking moves.
        wanted_ids = set(self.positions) | {x.market_id for x in self.fill_lots}
        missing = wanted_ids.difference(markets)
        for mid in list(missing):
            try:
                raw = micro.request_json(self.gamma.rstrip("/") + "/markets/" + mid)
                if isinstance(raw, dict):
                    m = micro.Market(raw)
                    markets[m.id] = m
                    markets_list.append(m)
            except Exception:
                pass
        books = micro.fetch_books(self.clob, markets_list)
        tape = load_tape(self.a.trade_tape, now_ms)
        stats = load_recent_stats(self.a.trade_tape, now_ms, self.a.activity_lookback_seconds)
        chosen = choose_tokens(
            stats,
            min_trades=self.a.min_recent_trades,
            min_sell_shares=self.a.min_recent_sell_shares,
            min_sell_share=self.a.min_sell_share,
            max_sell_share=self.a.max_sell_share,
            max_tokens=self.a.max_active_tokens,
        )
        active = {s.token_id: s for s in chosen}

        eq = self.equity(books)
        self.peak = max(self.peak, eq)
        if self.peak > 0.0 and 1.0 - eq / self.peak >= self.a.max_drawdown:
            self.killed = True
        self.process_orders(now, now_ms, books, tape, active)
        self.process_positions(now, books, markets)
        self.record_markouts(now, books)
        eq = self.equity(books)
        self.peak = max(self.peak, eq)
        if self.peak > 0.0 and 1.0 - eq / self.peak >= self.a.max_drawdown:
            self.killed = True

        signals = 0
        if allow_new_entries and not self.killed:
            for m in markets_list:
                if m.id in self.orders or m.id in self.positions:
                    continue
                y, n = books.get(m.yes), books.get(m.no)
                if y is None or n is None:
                    continue
                mid = y.mid()
                if not math.isfinite(mid) or mid <= float(self.cfg.get("min_mid", 0.02)) or mid >= float(self.cfg.get("max_mid", 0.98)):
                    continue
                max_spread = float(self.cfg.get("max_spread", 0.15))
                if y.spread() > max_spread or n.spread() > max_spread:
                    continue
                sig = micro_signal(y, n)
                if sig is None:
                    continue
                q_yes, conf = sig
                if conf < 0.10:
                    continue
                fd = self.fee_details(m)
                choices: list[tuple[float, str, str, micro.Book, float, float, float, int, float, float, float]] = []
                for side, token, b, fair in (("YES", m.yes, y, q_yes), ("NO", m.no, n, 1.0 - q_yes)):
                    if self.a.activity_gate and token not in active:
                        continue
                    bid, ask, spread = b.bid(), b.ask(), b.spread()
                    if not math.isfinite(bid) or not math.isfinite(ask) or not math.isfinite(spread) or bid <= 0.0 or ask <= bid:
                        continue
                    adverse = self.a.adverse_selection_mult * spread * (1.0 - conf)
                    future_bid = clamp(fair - 0.5 * spread, 0.001, 0.999) * (1.0 - self.a.slippage_bps / 10000.0)
                    exit_fee = fee_amount(1.0, future_bid, fd, taker=True)
                    improve = 0
                    for k in range(1, self.a.improve_ticks + 1):
                        cand = bid + k * b.tick
                        if cand >= ask - 1e-12:
                            break
                        cand_edge = future_bid - exit_fee - cand - adverse
                        if cand_edge > self.a.min_edge:
                            improve = k
                        else:
                            break
                    limit = bid + improve * b.tick
                    if limit >= ask - 1e-12:
                        continue
                    edge = future_bid - exit_fee - limit - adverse
                    if edge <= self.a.min_edge:
                        continue
                    queue = displayed_size(b, limit, True)
                    active_s = active.get(token)
                    if self.a.activity_gate:
                        assert active_s is not None
                        expected_sell = active_s.sell_shares * self.a.ttl_seconds / max(1, self.a.activity_lookback_seconds)
                        toxicity = max(0.0, (active_s.sell_share - 0.50) / 0.50)
                    else:
                        expected_sell = 0.0
                        toxicity = 0.0
                    choices.append((edge, side, token, b, fair, limit, queue, improve, expected_sell, toxicity, conf))
                if not choices:
                    continue
                signals += 1

                reserved = self.reserved()
                pos_cost = self.position_cost()
                available_cash = max(0.0, self.cash - reserved)
                event_room = self.a.max_event_fraction * eq - self.event_committed(m.event)
                gross_room = self.a.max_gross_fraction * eq - pos_cost - reserved
                current_dd_dollars = max(0.0, self.peak - eq)
                loss_room = self.a.max_drawdown * self.peak - current_dd_dollars - pos_cost - reserved
                max_cash = min(
                    self.a.max_order_usd,
                    available_cash,
                    self.a.max_market_fraction * eq,
                    max(0.0, event_room),
                    max(0.0, gross_room),
                    max(0.0, loss_room),
                )
                if max_cash <= 0.0:
                    continue

                ranked = []
                for edge, side, token, b, fair, limit, queue, improve, expected_sell, toxicity, conf in choices:
                    shares = min(max_cash / limit, max(b.min_order, 0.25 * max(1.0, touch_size(b))))
                    if shares < b.min_order or shares * limit > available_cash + 1e-9:
                        continue
                    if improve == 0 and queue > self.a.max_queue_multiple * max(shares, 1e-12):
                        qorder = Order(m.id, m.event, m.condition, m.slug, side, token, limit, shares, queue, now, fair, edge, conf, fd)
                        self.log_order(now, "SKIP_QUEUE", qorder)
                        continue
                    if self.a.activity_gate:
                        pfill = min(0.99, max(0.0, expected_sell) / max(1e-12, queue + shares))
                        fill_ev = pfill * edge - self.a.adverse_selection_mult * max(0.0, b.spread()) * toxicity
                        if pfill < self.a.min_pfill_proxy or fill_ev <= 0.0:
                            qorder = Order(m.id, m.event, m.condition, m.slug, side, token, limit, shares, queue, now, fair, edge, conf, fd)
                            self.log_order(now, "SKIP_FILL_EV", qorder, pfill=pfill, toxicity=toxicity, fill_ev=fill_ev)
                            continue
                    else:
                        pfill, fill_ev = 0.0, edge
                    ranked.append((fill_ev, pfill, toxicity, edge, side, token, limit, shares, queue, fair, conf, fd, improve))
                if not ranked:
                    continue
                ranked.sort(reverse=True, key=lambda x: (x[0], x[3]))
                fill_ev, pfill, toxicity, edge, side, token, limit, shares, queue, fair, conf, fd, improve = ranked[0]
                o = Order(m.id, m.event, m.condition, m.slug, side, token, limit, shares, queue, now, fair, edge, conf, fd)
                self.orders[m.id] = o
                self.log_order(now, "POST", o, pfill=pfill, toxicity=toxicity, fill_ev=fill_ev)
                if improve:
                    self.counters["INSIDE_IMPROVE"] += 1

        drawdown = 1.0 - eq / self.peak if self.peak > 0 else 0.0
        append_csv(
            self.equity_log,
            ["timestamp", "cash", "equity", "reserved_cash", "resting_orders", "positions", "peak_equity", "drawdown", "killed", "signals", "active_tokens"],
            {
                "timestamp": now,
                "cash": self.cash,
                "equity": eq,
                "reserved_cash": self.reserved(),
                "resting_orders": len(self.orders),
                "positions": len(self.positions),
                "peak_equity": self.peak,
                "drawdown": drawdown,
                "killed": int(self.killed),
                "signals": signals,
                "active_tokens": len(active),
            },
        )
        self.counters["TICKS"] += 1

    def summary(self) -> dict[str, Any]:
        marks: dict[str, dict[str, float | int | None]] = {}
        rows = []
        if self.mark_log.exists():
            with self.mark_log.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        for h in (45, 60, 300):
            for mult in (1.0, 1.5, 2.0):
                xs = [finite(r.get("markout_per_share")) for r in rows if int(float(r.get("horizon_seconds") or 0)) == h and abs(finite(r.get("cost_stress"), 0.0) - mult) < 1e-9 and int(float(r.get("available") or 0)) == 1]
                xs = [x for x in xs if math.isfinite(x)]
                marks[f"{h}s_{mult}x"] = {
                    "n": len(xs),
                    "mean": sum(xs) / len(xs) if xs else None,
                    "positive_fraction": sum(x > 0 for x in xs) / len(xs) if xs else None,
                }
        return {
            "schema": "v6_hf_active_maker_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "activity_gate": bool(self.a.activity_gate),
            "authorized_envelope": {
                "markets": self.a.markets,
                "min_liquidity": self.a.min_liquidity,
                "min_edge": self.a.min_edge,
                "max_trade_usd": self.a.max_order_usd,
                "max_market_fraction": self.a.max_market_fraction,
                "max_event_fraction": self.a.max_event_fraction,
                "max_gross_fraction": self.a.max_gross_fraction,
                "max_drawdown": self.a.max_drawdown,
            },
            "counters": dict(self.counters),
            "realized_pnl": self.realized_pnl,
            "closed_shares": self.closed_shares,
            "fill_conditioned_pnl_per_share": self.realized_pnl / self.closed_shares if self.closed_shares > 0 else None,
            "resting_orders": len(self.orders),
            "open_positions": len(self.positions),
            "cash": self.cash,
            "peak_equity": self.peak,
            "killed": self.killed,
            "markouts": marks,
        }

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        entry_end = started + self.a.entry_seconds
        stop = entry_end + self.a.tail_seconds
        while time.monotonic() < stop:
            allow = time.monotonic() < entry_end
            try:
                self.tick(allow)
            except Exception as exc:
                self.counters["TICK_ERRORS"] += 1
                print(f"active_maker_tick_error={type(exc).__name__}:{exc}", flush=True)
            remaining = stop - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.a.interval_seconds, remaining))
        out = self.summary()
        (self.run_dir / "summary.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(out, sort_keys=True))
        return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research-only active-flow HF maker paper simulator")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--trade-tape", type=Path, required=True)
    p.add_argument("--markets", type=int, default=1000)
    p.add_argument("--min-liquidity", type=float, default=2.0)
    p.add_argument("--min-edge", type=float, default=0.00005)
    p.add_argument("--max-order-usd", type=float, default=125.0)
    p.add_argument("--starting-capital", type=float, default=1200.0)
    p.add_argument("--max-market-fraction", type=float, default=0.05)
    p.add_argument("--max-event-fraction", type=float, default=0.15)
    p.add_argument("--max-gross-fraction", type=float, default=0.70)
    p.add_argument("--max-drawdown", type=float, default=0.15)
    p.add_argument("--ttl-seconds", type=int, default=60)
    p.add_argument("--hold-seconds", type=int, default=60)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--adverse-selection-mult", type=float, default=0.15)
    p.add_argument("--improve-ticks", type=int, default=1)
    p.add_argument("--max-queue-multiple", type=float, default=8.0)
    p.add_argument("--activity-gate", action="store_true")
    p.add_argument("--activity-lookback-seconds", type=int, default=120)
    p.add_argument("--min-recent-trades", type=int, default=2)
    p.add_argument("--min-recent-sell-shares", type=float, default=5.0)
    p.add_argument("--min-sell-share", type=float, default=0.05)
    p.add_argument("--max-sell-share", type=float, default=0.80)
    p.add_argument("--max-active-tokens", type=int, default=250)
    p.add_argument("--min-pfill-proxy", type=float, default=0.005)
    p.add_argument("--dead-queue-grace-seconds", type=int, default=30)
    p.add_argument("--dead-queue-pfill", type=float, default=0.02)
    p.add_argument("--entry-seconds", type=int, default=180)
    p.add_argument("--tail-seconds", type=int, default=320)
    p.add_argument("--interval-seconds", type=int, default=10)
    a = p.parse_args()
    if a.markets < 1 or a.markets > 1000:
        p.error("markets must be in [1,1000]")
    if a.min_liquidity < 2.0:
        p.error("min liquidity may not be below the authorized $2 floor")
    if a.min_edge < 0.00005:
        p.error("min edge may not be below the authorized 0.5 bp floor")
    if a.max_order_usd > 125.0:
        p.error("max order exceeds authorized paper envelope")
    if a.max_market_fraction > 0.05 or a.max_event_fraction > 0.15 or a.max_gross_fraction > 0.70:
        p.error("concentration/gross cap exceeds authorized paper envelope")
    if a.max_drawdown > 0.15:
        p.error("max drawdown exceeds hard safety")
    return a


def main() -> int:
    a = parse_args()
    ActiveMaker(a).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
