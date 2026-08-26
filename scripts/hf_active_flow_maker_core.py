#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
DATA_URL = "https://data-api.polymarket.com"
UA = "Polymarket-HF-Research/1.0"


def now_s() -> int:
    return int(time.time())


def now_ms() -> int:
    return int(time.time() * 1000)


def number(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def boolean(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"true", "1", "yes"}:
            return True
        if s in {"false", "0", "no"}:
            return False
    return default


def array(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except json.JSONDecodeError:
            return []
    return []


def request_json(url: str, payload: Any | None = None, timeout: float = 20.0) -> tuple[Any, int]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result, now_ms()


@dataclass(frozen=True)
class Fee:
    rate: float
    exponent: float
    taker_only: bool
    source: str


@dataclass
class Market:
    market_id: str
    condition_id: str
    event_id: str
    slug: str
    yes_token: str
    no_token: str
    liquidity: float
    volume24h: float
    fee: Fee | None


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: list[Level]
    asks: list[Level]
    tick_size: float
    min_order_size: float

    def best_bid(self) -> float:
        return max((x.price for x in self.bids), default=math.nan)

    def best_ask(self) -> float:
        return min((x.price for x in self.asks), default=math.nan)

    def spread(self) -> float:
        b, a = self.best_bid(), self.best_ask()
        return max(0.0, a - b) if math.isfinite(b) and math.isfinite(a) else math.inf

    def midpoint(self) -> float:
        b, a = self.best_bid(), self.best_ask()
        return 0.5 * (a + b) if math.isfinite(b) and math.isfinite(a) else math.nan

    def displayed_size(self, bid_side: bool, price: float) -> float:
        tol = 0.25 * max(1e-8, self.tick_size)
        levels = self.bids if bid_side else self.asks
        return sum(x.size for x in levels if abs(x.price - price) <= tol)

    def weighted_depth(self, bid_side: bool, n: int = 5) -> float:
        levels = sorted(self.bids if bid_side else self.asks,
                        key=lambda x: x.price, reverse=bid_side)[:n]
        if not levels:
            return 0.0
        best = levels[0].price
        scale = max(1e-4, 3.0 * self.tick_size)
        return sum(max(0.0, x.size) * math.exp(-abs(x.price - best) / scale) for x in levels)

    def microprice(self, n: int = 5) -> float:
        b, a = self.best_bid(), self.best_ask()
        if not (math.isfinite(b) and math.isfinite(a)) or a < b:
            return self.midpoint()
        db, da = self.weighted_depth(True, n), self.weighted_depth(False, n)
        if db + da <= 1e-12:
            return 0.5 * (a + b)
        return min(a, max(b, (a * db + b * da) / (db + da)))

    def executable_bid(self, shares: float, slippage_bps: float) -> float | None:
        if shares <= 0:
            return None
        remaining, proceeds, sold = shares, 0.0, 0.0
        for level in sorted(self.bids, key=lambda x: x.price, reverse=True):
            q = min(remaining, level.size)
            if q <= 0:
                continue
            sold += q
            proceeds += q * level.price
            remaining -= q
            if remaining <= 1e-12:
                break
        if sold + 1e-9 < shares or sold <= 0:
            return None
        return (proceeds / sold) * (1.0 - slippage_bps / 10000.0)


@dataclass(frozen=True)
class Trade:
    trade_id: str
    token_id: str
    side: str
    price: float
    size: float
    ts: int


@dataclass(frozen=True)
class Flow:
    trade_count: int
    buy_volume: float
    sell_volume: float
    compatible_sell_volume: float
    compatible_sell_prints: int
    last_event_age: int | None
    signed_imbalance: float


@dataclass
class Candidate:
    arm: str
    market: Market
    side: str
    token_id: str
    tick_size: float
    limit_price: float
    shares: float
    queue_ahead: float
    static_edge: float
    adjusted_edge: float
    confidence: float
    improvement_ticks: int
    flow: Flow
    fill_probability_proxy: float
    score: float


@dataclass
class ShadowOrder:
    candidate: Candidate
    created_ts: int
    created_ms: int
    expires_ts: int
    remaining: float
    queue_ahead: float
    seen_ids: set[str] = field(default_factory=set)
    filled: float = 0.0
    first_fill_event_ts: int | None = None
    first_fill_received_ts: int | None = None


@dataclass
class FillOutcome:
    arm: str
    market_id: str
    side: str
    token_id: str
    shares: float
    entry_price: float
    first_fill_event_ts: int
    first_fill_received_ts: int
    markouts: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    realized_exit_price: float | None = None
    exit_fee: float | None = None
    realized_pnl: float | None = None


def fee_per_share(price: float, fee: Fee) -> float:
    if fee.rate <= 0:
        return 0.0
    p = min(0.999999, max(0.000001, price))
    return fee.rate * math.pow(p * (1.0 - p), max(0.0, fee.exponent))


def gamma_fee(raw: dict[str, Any]) -> Fee | None:
    schedule = raw.get("feeSchedule")
    if isinstance(schedule, dict):
        rate = number(schedule.get("rate"), -1.0)
        if rate >= 0:
            return Fee(rate, number(schedule.get("exponent"), 1.0),
                       boolean(schedule.get("takerOnly"), True), "gamma_feeSchedule")
    if "feesEnabled" in raw and not boolean(raw.get("feesEnabled"), False):
        return Fee(0.0, 1.0, True, "gamma_fees_disabled")
    return None


def fetch_clob_fee(condition_id: str) -> Fee | None:
    try:
        raw, _ = request_json(f"{CLOB_URL}/clob-markets/{urllib.parse.quote(condition_id)}")
        if isinstance(raw, dict) and isinstance(raw.get("fd"), dict):
            fd = raw["fd"]
            rate = number(fd.get("r"), -1.0)
            if rate >= 0:
                return Fee(rate, number(fd.get("e"), 1.0), boolean(fd.get("to"), True), "clob_market_fd")
    except Exception:
        return None
    return None


def parse_market(raw: dict[str, Any], min_liquidity: float) -> Market | None:
    market_id, condition = str(raw.get("id") or ""), str(raw.get("conditionId") or "")
    if not market_id or not condition:
        return None
    if not boolean(raw.get("active"), True) or boolean(raw.get("closed"), False):
        return None
    if not boolean(raw.get("enableOrderBook"), True) or not boolean(raw.get("acceptingOrders"), True):
        return None
    liquidity = number(raw.get("liquidityNum", raw.get("liquidity")), 0.0)
    if liquidity + 1e-12 < min_liquidity:
        return None
    tokens, outcomes = array(raw.get("clobTokenIds")), array(raw.get("outcomes"))
    if len(tokens) < 2:
        return None
    yi, ni = 0, 1
    for i, outcome in enumerate(outcomes[:len(tokens)]):
        label = str(outcome).lower()
        if label == "yes":
            yi = i
        elif label == "no":
            ni = i
    event_id = str(raw.get("eventId") or "")
    events = raw.get("events")
    if not event_id and isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or "")
    if not event_id:
        event_id = condition
    return Market(market_id, condition, event_id, str(raw.get("slug") or market_id),
                  str(tokens[yi]), str(tokens[ni]), liquidity,
                  number(raw.get("volume24hr"), 0.0), gamma_fee(raw))


def discover_markets(limit: int, min_liquidity: float) -> list[Market]:
    target = min(max(1, limit), 2000)
    out: list[Market] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < target and offset < 10000:
        query = urllib.parse.urlencode({"active": "true", "closed": "false", "limit": 100,
                                        "offset": offset, "order": "liquidityNum", "ascending": "false",
                                        "liquidity_num_min": f"{min_liquidity:.12g}"})
        raw, _ = request_json(f"{GAMMA_URL}/markets?{query}")
        rows = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError("unexpected Gamma market response")
        for item in rows:
            if not isinstance(item, dict):
                continue
            market = parse_market(item, min_liquidity)
            if market and market.market_id not in seen:
                seen.add(market.market_id)
                out.append(market)
                if len(out) >= target:
                    break
        if len(rows) < 100:
            break
        offset += 100
    return out


def parse_book(raw: dict[str, Any]) -> Book | None:
    token = str(raw.get("asset_id") or "")
    if not token:
        return None
    def levels(key: str) -> list[Level]:
        out: list[Level] = []
        rows = raw.get(key, [])
        if not isinstance(rows, list):
            return out
        for item in rows:
            if not isinstance(item, dict):
                continue
            p, s = number(item.get("price"), -1.0), number(item.get("size"), 0.0)
            if 0 < p < 1 and s > 0:
                out.append(Level(p, s))
        return out
    return Book(token, levels("bids"), levels("asks"),
                max(1e-6, number(raw.get("tick_size"), 0.01)),
                max(0.0, number(raw.get("min_order_size"), 1.0)))


def fetch_books(tokens: list[str], batch: int = 40) -> dict[str, Book]:
    result: dict[str, Book] = {}
    unique = list(dict.fromkeys(tokens))
    for pos in range(0, len(unique), batch):
        raw, _ = request_json(f"{CLOB_URL}/books", [{"token_id": x} for x in unique[pos:pos + batch]])
        if not isinstance(raw, list):
            raise RuntimeError("unexpected CLOB books response")
        for item in raw:
            if isinstance(item, dict):
                book = parse_book(item)
                if book:
                    result[book.token_id] = book
    return result


def fetch_trades(condition_id: str, start_ts: int, end_ts: int) -> tuple[list[Trade], int]:
    query = urllib.parse.urlencode({"market": condition_id, "limit": 10000, "takerOnly": "true",
                                    "start": max(0, start_ts), "end": max(0, end_ts)})
    raw, received_ms = request_json(f"{DATA_URL}/trades?{query}")
    rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
    out: list[Trade] = []
    seen: set[str] = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        token, side = str(item.get("asset") or ""), str(item.get("side") or "").upper()
        ts = int(number(item.get("timestamp"), 0))
        price, size = number(item.get("price"), -1.0), number(item.get("size"), 0.0)
        if not token or ts <= 0 or not (0 < price < 1) or size <= 0:
            continue
        key = ":".join([str(item.get("transactionHash") or ""), token, str(ts), side,
                        f"{price:.12g}", f"{size:.12g}"])
        if key in seen:
            continue
        seen.add(key)
        out.append(Trade(key, token, side, price, size, ts))
    out.sort(key=lambda x: (x.ts, x.trade_id))
    return out, received_ms


def micro_signal(yes: Book, no: Book) -> tuple[float, float]:
    mid, y, n = yes.midpoint(), yes.microprice(), no.microprice()
    if not math.isfinite(mid):
        return 0.5, 0.0
    dy = yes.weighted_depth(True) + yes.weighted_depth(False)
    dn = no.weighted_depth(True) + no.weighted_depth(False)
    wy = math.sqrt(max(0.0, dy)) / (1.0 + 20.0 * yes.spread())
    wn = math.sqrt(max(0.0, dn)) / (1.0 + 20.0 * no.spread())
    q = mid
    if math.isfinite(y) and math.isfinite(n) and wy + wn > 1e-12:
        q = (wy * y + wn * (1.0 - n)) / (wy + wn)
    elif math.isfinite(y):
        q = y
    parity = abs(y - (1.0 - n)) if math.isfinite(y) and math.isfinite(n) else 0.25
    liq = (dy + dn) / (dy + dn + 200.0)
    conf = max(0.02, min(1.0, liq * math.exp(-5.0 * (yes.spread() + no.spread())) * math.exp(-8.0 * parity)))
    return max(0.001, min(0.999, q)), conf


def flow_stats(trades: list[Trade], token: str, decision_ts: int, lookback: int, limit: float) -> Flow:
    rows = [t for t in trades if t.token_id == token and decision_ts - lookback <= t.ts <= decision_ts]
    buy = sum(t.size for t in rows if t.side == "BUY")
    sell = sum(t.size for t in rows if t.side == "SELL")
    compatible = [t for t in rows if t.side == "SELL" and t.price <= limit + 1e-12]
    total = buy + sell
    return Flow(len(rows), buy, sell, sum(t.size for t in compatible), len(compatible),
                decision_ts - max((t.ts for t in rows), default=0) if rows else None,
                (buy - sell) / total if total > 1e-12 else 0.0)


def fill_probability_proxy(flow: Flow, queue: float, shares: float) -> float:
    burden = max(1e-12, queue + shares)
    clearance = max(0.0, flow.compatible_sell_volume) / burden
    recurrence = min(1.0, max(0, flow.compatible_sell_prints) / 3.0)
    return min(1.0, clearance) * recurrence


def activity_eligible(flow: Flow, queue: float, shares: float, args: argparse.Namespace) -> bool:
    if flow.trade_count < args.min_recent_trades or flow.compatible_sell_prints < args.min_sell_prints:
        return False
    if flow.last_event_age is None or flow.last_event_age > args.max_event_age_seconds:
        return False
    if fill_probability_proxy(flow, queue, shares) < args.min_fill_probability:
        return False
    return max(0.0, -flow.signed_imbalance) <= args.max_sell_toxicity


def size_at_price(book: Book, limit: float, args: argparse.Namespace) -> float:
    max_cash = min(args.max_order_usd,
                   args.max_market_fraction * args.starting_capital,
                   args.max_event_fraction * args.starting_capital,
                   args.max_gross_fraction * args.starting_capital)
    if max_cash <= 0 or limit <= 0:
        return 0.0
    touch = book.displayed_size(True, book.best_bid())
    shares = min(max_cash / limit, max(book.min_order_size, 0.25 * max(1.0, touch)))
    return shares if shares >= book.min_order_size and shares * limit <= max_cash + 1e-9 else 0.0


def resolve_fee(market: Market) -> Fee | None:
    if market.fee is not None:
        return market.fee
    market.fee = fetch_clob_fee(market.condition_id)
    return market.fee


def build_candidates(markets: list[Market], books: dict[str, Book], flows: dict[str, list[Trade]],
                     decision_ts: int, activity_ids: set[str], args: argparse.Namespace) -> tuple[list[Candidate], list[Candidate]]:
    baseline: list[Candidate] = []
    active: list[Candidate] = []
    for market in markets:
        yes, no = books.get(market.yes_token), books.get(market.no_token)
        if not yes or not no or not (0.01 < yes.midpoint() < 0.99):
            continue
        if yes.spread() > args.max_spread or no.spread() > args.max_spread:
            continue
        q_yes, confidence = micro_signal(yes, no)
        if confidence < args.min_confidence:
            continue
        fee = resolve_fee(market)
        if fee is None:
            continue
        choices: list[Candidate] = []
        for side, token, book, fair in (("YES", market.yes_token, yes, q_yes), ("NO", market.no_token, no, 1.0 - q_yes)):
            bid, ask, spread = book.best_bid(), book.best_ask(), book.spread()
            if not (math.isfinite(bid) and math.isfinite(ask) and ask > bid > 0):
                continue
            future_bid = max(0.001, min(0.999, fair - 0.5 * spread)) * (1.0 - args.slippage_bps / 10000.0)
            exit_fee = fee_per_share(future_bid, fee)
            generic_adverse = args.adverse_selection_mult * spread * (1.0 - confidence)
            base_edge = future_bid - exit_fee - bid - generic_adverse
            if base_edge <= args.min_edge:
                continue
            base_shares = size_at_price(book, bid, args)
            if base_shares <= 0:
                continue
            base_queue = book.displayed_size(True, bid)
            trades = flows.get(market.condition_id, [])
            base_flow = flow_stats(trades, token, decision_ts, args.recent_lookback_seconds, bid)
            base_proxy = fill_probability_proxy(base_flow, base_queue, base_shares)
            choices.append(Candidate("baseline_static", market, side, token, book.tick_size, bid,
                                     base_shares, base_queue, base_edge, base_edge, confidence, 0,
                                     base_flow, base_proxy, base_edge))

            if market.market_id not in activity_ids:
                continue
            toxicity = args.toxicity_mult * spread * max(0.0, -base_flow.signed_imbalance)
            limit, improvement = bid, 0
            adjusted = base_edge - toxicity
            active_flow, queue, shares = base_flow, base_queue, base_shares
            if args.improve_ticks > 0 and base_flow.compatible_sell_prints >= args.min_sell_prints:
                inside = bid + book.tick_size
                if inside < ask - 1e-12:
                    inside_flow = flow_stats(trades, token, decision_ts, args.recent_lookback_seconds, inside)
                    inside_toxicity = args.toxicity_mult * spread * max(0.0, -inside_flow.signed_imbalance)
                    inside_edge = future_bid - exit_fee - inside - generic_adverse - inside_toxicity
                    inside_shares = size_at_price(book, inside, args)
                    if inside_edge > args.min_edge and inside_shares > 0:
                        limit, improvement, adjusted = inside, 1, inside_edge
                        active_flow = inside_flow
                        queue, shares = book.displayed_size(True, inside), inside_shares
            proxy = fill_probability_proxy(active_flow, queue, shares)
            if adjusted > args.min_edge and activity_eligible(active_flow, queue, shares, args):
                active.append(Candidate("active_flow", market, side, token, book.tick_size, limit,
                                        shares, queue, base_edge, adjusted, confidence, improvement,
                                        active_flow, proxy, proxy * adjusted))
        if choices:
            baseline.append(max(choices, key=lambda x: x.static_edge))
    baseline.sort(key=lambda x: x.static_edge, reverse=True)
    active.sort(key=lambda x: x.score, reverse=True)
    return baseline, active


def select_with_caps(candidates: list[Candidate], max_orders: int, args: argparse.Namespace) -> list[Candidate]:
    selected: list[Candidate] = []
    event_notional: dict[str, float] = {}
    gross = 0.0
    for c in candidates:
        if len(selected) >= max_orders:
            break
        notional = c.shares * c.limit_price
        if notional > args.max_order_usd + 1e-9 or notional > args.max_market_fraction * args.starting_capital + 1e-9:
            continue
        if event_notional.get(c.market.event_id, 0.0) + notional > args.max_event_fraction * args.starting_capital + 1e-9:
            continue
        if gross + notional > args.max_gross_fraction * args.starting_capital + 1e-9:
            continue
        selected.append(c)
        event_notional[c.market.event_id] = event_notional.get(c.market.event_id, 0.0) + notional
        gross += notional
    return selected


def consume(order: ShadowOrder, trades: list[Trade], received_ts: int) -> None:
    tolerance = 0.25 * max(1e-8, order.candidate.tick_size)
    for trade in trades:
        if trade.trade_id in order.seen_ids:
            continue
        if trade.ts <= order.created_ts or trade.ts > order.expires_ts:
            continue
        order.seen_ids.add(trade.trade_id)
        if trade.token_id != order.candidate.token_id or trade.side != "SELL":
            continue
        if trade.price > order.candidate.limit_price + tolerance:
            continue
        flow = trade.size
        q = min(order.queue_ahead, flow)
        order.queue_ahead -= q
        flow -= q
        fill = min(order.remaining, max(0.0, flow))
        if fill <= 0:
            continue
        order.remaining -= fill
        order.filled += fill
        if order.first_fill_event_ts is None:
            order.first_fill_event_ts = trade.ts
            order.first_fill_received_ts = received_ts
        if order.remaining <= 1e-12:
            order.remaining = 0.0
            break


def candidate_dict(c: Candidate) -> dict[str, Any]:
    f = c.flow
    return {"arm": c.arm, "market_id": c.market.market_id, "condition_id": c.market.condition_id,
            "event_id": c.market.event_id, "slug": c.market.slug, "side": c.side,
            "token_id": c.token_id, "liquidity": c.market.liquidity, "volume24h": c.market.volume24h,
            "fee_source": c.market.fee.source if c.market.fee else None,
            "limit_price": c.limit_price, "shares": c.shares, "queue_ahead": c.queue_ahead,
            "static_edge": c.static_edge, "adjusted_edge": c.adjusted_edge,
            "confidence": c.confidence, "improvement_ticks": c.improvement_ticks,
            "fill_probability_proxy": c.fill_probability_proxy, "score": c.score,
            "flow": {"trade_count": f.trade_count, "buy_volume": f.buy_volume,
                     "sell_volume": f.sell_volume, "compatible_sell_volume": f.compatible_sell_volume,
                     "compatible_sell_prints": f.compatible_sell_prints,
                     "last_event_age": f.last_event_age, "signed_imbalance": f.signed_imbalance}}


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    run_started = now_s()
    markets = discover_markets(args.markets, args.min_liquidity)
    activity_markets = sorted(markets, key=lambda m: (m.volume24h, m.liquidity), reverse=True)[:args.activity_scan_markets]
    activity_ids = {m.market_id for m in activity_markets}
    flows: dict[str, list[Trade]] = {}
    flow_errors: list[str] = []
    for market in activity_markets:
        end = now_s()
        try:
            rows, _ = fetch_trades(market.condition_id, end - args.recent_lookback_seconds, end)
            flows[market.condition_id] = rows
        except Exception as exc:
            flow_errors.append(f"{market.market_id}:{type(exc).__name__}:{exc}")

    # Books are fetched after activity queries so the quote snapshot is the freshest input at decision time.
    books = fetch_books([t for m in markets for t in (m.yes_token, m.no_token)])
    decision_ts = now_s()
    baseline, active = build_candidates(markets, books, flows, decision_ts, activity_ids, args)
    selected = {"baseline_static": select_with_caps(baseline, args.max_orders_per_arm, args),
                "active_flow": select_with_caps(active, args.max_orders_per_arm, args)}
    orders = [ShadowOrder(c, decision_ts, now_ms(), decision_ts + args.order_ttl_seconds,
                          c.shares, c.queue_ahead)
              for candidates in selected.values() for c in candidates]

    max_horizon = max(args.markout_seconds)
    end_wall = decision_ts + args.order_ttl_seconds + args.trade_index_lag_seconds + max_horizon + args.markout_buffer_seconds
    outcomes: dict[tuple[str, str, str], FillOutcome] = {}
    poll_errors = 0
    markout_errors = 0

    while True:
        current = now_s()
        groups: dict[str, list[ShadowOrder]] = {}
        for order in orders:
            if order.remaining > 1e-12 and current <= order.expires_ts + args.trade_index_lag_seconds:
                groups.setdefault(order.candidate.market.condition_id, []).append(order)
        for condition, group in groups.items():
            try:
                rows, received_ms = fetch_trades(condition, decision_ts,
                                                  min(current, decision_ts + args.order_ttl_seconds))
            except Exception:
                poll_errors += 1
                continue
            received_ts = received_ms // 1000
            for order in group:
                consume(order, rows, received_ts)
                if order.filled <= 0 or order.first_fill_event_ts is None or order.first_fill_received_ts is None:
                    continue
                key = (order.candidate.arm, order.candidate.market.market_id, order.candidate.token_id)
                existing = outcomes.get(key)
                if existing is None:
                    outcomes[key] = FillOutcome(order.candidate.arm, order.candidate.market.market_id,
                                                order.candidate.side, order.candidate.token_id,
                                                order.filled, order.candidate.limit_price,
                                                order.first_fill_event_ts, order.first_fill_received_ts)
                else:
                    existing.shares = order.filled

        filled_tokens = list({x.token_id for x in outcomes.values()})
        fresh: dict[str, Book] = {}
        if filled_tokens:
            try:
                fresh = fetch_books(filled_tokens)
            except Exception:
                markout_errors += 1
        for outcome in outcomes.values():
            book = fresh.get(outcome.token_id)
            if not book:
                continue
            elapsed = current - outcome.first_fill_received_ts
            for horizon in args.markout_seconds:
                h = str(horizon)
                if elapsed < horizon or h in outcome.markouts:
                    continue
                px = book.executable_bid(outcome.shares, args.slippage_bps)
                outcome.markouts[h] = {"observed_ts": current, "receive_time_horizon_seconds": elapsed,
                                       "executable_bid": px,
                                       "pnl_per_share_pre_fee": None if px is None else px - outcome.entry_price}
                if horizon == args.exit_horizon_seconds and outcome.realized_pnl is None and px is not None:
                    market = next((o.candidate.market for o in orders
                                   if o.candidate.arm == outcome.arm and o.candidate.market.market_id == outcome.market_id), None)
                    if market and market.fee:
                        fee = fee_per_share(px, market.fee) * outcome.shares
                        outcome.realized_exit_price = px
                        outcome.exit_fee = fee
                        outcome.realized_pnl = outcome.shares * (px - outcome.entry_price) - fee
        if current >= end_wall:
            break
        time.sleep(max(1, args.poll_seconds))

    arm_results: dict[str, Any] = {}
    for arm, candidates in selected.items():
        arm_orders = [o for o in orders if o.candidate.arm == arm]
        arm_outcomes = [x for x in outcomes.values() if x.arm == arm]
        closed = [x for x in arm_outcomes if x.realized_pnl is not None]
        closed_shares = sum(x.shares for x in closed)
        pnl = sum(float(x.realized_pnl) for x in closed)
        arm_results[arm] = {
            "candidates_available": len(baseline if arm == "baseline_static" else active),
            "orders": len(arm_orders), "fill_orders": sum(o.filled > 0 for o in arm_orders),
            "filled_shares": sum(o.filled for o in arm_orders),
            "fill_rate": sum(o.filled > 0 for o in arm_orders) / len(arm_orders) if arm_orders else 0.0,
            "realized_round_trips": len(closed), "closed_shares": closed_shares,
            "fill_conditioned_pnl": pnl,
            "fill_conditioned_pnl_per_share": pnl / closed_shares if closed_shares > 0 else None,
            "orders_detail": [candidate_dict(o.candidate) | {"filled_shares": o.filled,
                              "remaining_shares": o.remaining, "queue_remaining": o.queue_ahead,
                              "first_fill_event_ts": o.first_fill_event_ts,
                              "first_fill_received_ts": o.first_fill_received_ts} for o in arm_orders],
            "outcomes": [x.__dict__ for x in arm_outcomes],
        }

    return {"schema": "hf_active_flow_maker_probe_v1", "paper_only": True,
            "authenticated_execution": False, "real_money_execution": False,
            "generated_ts": now_s(), "run_started_ts": run_started, "decision_ts": decision_ts,
            "universe": {"requested_markets": args.markets, "discovered_markets": len(markets),
                         "books": len(books), "activity_scan_markets": len(activity_markets),
                         "activity_conditions_with_trades": sum(bool(x) for x in flows.values()),
                         "flow_errors": flow_errors[:20]},
            "safety": {"starting_capital": args.starting_capital, "min_liquidity": args.min_liquidity,
                       "min_edge": args.min_edge, "kelly_ceiling": 0.25,
                       "max_order_usd": args.max_order_usd,
                       "max_market_fraction": args.max_market_fraction,
                       "max_event_fraction": args.max_event_fraction,
                       "max_gross_fraction": args.max_gross_fraction,
                       "max_drawdown": args.max_drawdown},
            "method": {"recent_lookback_seconds": args.recent_lookback_seconds,
                       "min_recent_trades": args.min_recent_trades,
                       "min_sell_prints": args.min_sell_prints,
                       "max_event_age_seconds": args.max_event_age_seconds,
                       "min_fill_probability": args.min_fill_probability,
                       "max_sell_toxicity": args.max_sell_toxicity,
                       "toxicity_mult": args.toxicity_mult,
                       "order_ttl_seconds": args.order_ttl_seconds,
                       "trade_index_lag_seconds": args.trade_index_lag_seconds,
                       "exit_horizon_seconds": args.exit_horizon_seconds,
                       "markout_seconds": args.markout_seconds,
                       "markout_clock": "local_receive_time_after_causal_fill_discovery",
                       "fee_provenance_fail_closed": True,
                       "counterfactual_arms_are_independent": True},
            "arms": arm_results, "poll_errors": poll_errors, "markout_book_errors": markout_errors}


def render_markdown(result: dict[str, Any]) -> str:
    lines = ["# HF active-flow maker probe", "",
             "| Arm | Orders | Fill orders | Filled shares | Closed PnL | PnL/share |",
             "|---|---:|---:|---:|---:|---:|"]
    for arm, data in result["arms"].items():
        pps = data.get("fill_conditioned_pnl_per_share")
        lines.append(f"| {arm} | {data['orders']} | {data['fill_orders']} | {data['filled_shares']:.6f} | {data['fill_conditioned_pnl']:.6f} | {'' if pps is None else f'{pps:.8f}'} |")
    lines += ["", f"- discovered universe: {result['universe']['discovered_markets']} markets",
              f"- activity scan: {result['universe']['activity_scan_markets']} markets",
              "- fills use public tape and FIFO queue depletion only; no authenticated order submission.",
              "- 45/60/300s markouts are executable bids observed after local causal fill discovery."]
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", default="v6_evidence/hf_active_flow")
    p.add_argument("--markets", type=int, default=1000)
    p.add_argument("--activity-scan-markets", type=int, default=120)
    p.add_argument("--min-liquidity", type=float, default=2.0)
    p.add_argument("--starting-capital", type=float, default=1200.0)
    p.add_argument("--min-edge", type=float, default=0.00005)
    p.add_argument("--max-order-usd", type=float, default=125.0)
    p.add_argument("--max-market-fraction", type=float, default=0.05)
    p.add_argument("--max-event-fraction", type=float, default=0.15)
    p.add_argument("--max-gross-fraction", type=float, default=0.70)
    p.add_argument("--max-drawdown", type=float, default=0.15)
    p.add_argument("--max-spread", type=float, default=0.15)
    p.add_argument("--min-confidence", type=float, default=0.10)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--adverse-selection-mult", type=float, default=0.15)
    p.add_argument("--toxicity-mult", type=float, default=0.25)
    p.add_argument("--improve-ticks", type=int, default=1)
    p.add_argument("--recent-lookback-seconds", type=int, default=120)
    p.add_argument("--min-recent-trades", type=int, default=2)
    p.add_argument("--min-sell-prints", type=int, default=2)
    p.add_argument("--max-event-age-seconds", type=int, default=60)
    p.add_argument("--min-fill-probability", type=float, default=0.02)
    p.add_argument("--max-sell-toxicity", type=float, default=0.80)
    p.add_argument("--max-orders-per-arm", type=int, default=8)
    p.add_argument("--order-ttl-seconds", type=int, default=60)
    p.add_argument("--poll-seconds", type=int, default=5)
    p.add_argument("--trade-index-lag-seconds", type=int, default=30)
    p.add_argument("--markout-seconds", default="45,60,300")
    p.add_argument("--exit-horizon-seconds", type=int, default=60)
    p.add_argument("--markout-buffer-seconds", type=int, default=20)
    args = p.parse_args()
    args.markout_seconds = sorted({int(x) for x in args.markout_seconds.split(",") if int(x) > 0})
    if args.exit_horizon_seconds not in args.markout_seconds:
        args.markout_seconds.append(args.exit_horizon_seconds)
        args.markout_seconds.sort()
    if args.min_liquidity < 2.0 or args.min_edge < 0.00005:
        raise SystemExit("research settings exceed authorized aggression floor")
    if (args.max_order_usd > 125.0 or args.max_market_fraction > 0.05 or
            args.max_event_fraction > 0.15 or args.max_gross_fraction > 0.70):
        raise SystemExit("research settings exceed authorized concentration ceilings")
    if args.max_drawdown > 0.15:
        raise SystemExit("max drawdown may not exceed 15%")
    return args


def main() -> int:
    args = parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = run_probe(args)
    (out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "result.md").write_text(render_markdown(result), encoding="utf-8")
    print(render_markdown(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
