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
USER_AGENT = "Polymarket-HF-Research/1.0"


def now_s() -> int:
    return int(time.time())


def now_ms() -> int:
    return int(time.time() * 1000)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "1", "yes"}:
            return True
        if s in {"false", "0", "no"}:
            return False
    return default


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def request_json(url: str, payload: Any | None = None, timeout: float = 20.0) -> tuple[Any, int]:
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body), now_ms()


@dataclass(frozen=True)
class Fee:
    rate: float
    exponent: float
    taker_only: bool
    source: str


@dataclass(frozen=True)
class Market:
    market_id: str
    condition_id: str
    event_id: str
    slug: str
    yes_token: str
    no_token: str
    liquidity: float
    volume24h: float
    fee: Fee


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
        tolerance = 0.25 * max(1e-8, self.tick_size)
        levels = self.bids if bid_side else self.asks
        return sum(x.size for x in levels if abs(x.price - price) <= tolerance)

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
        remaining = shares
        proceeds = 0.0
        sold = 0.0
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
    limit_price: float
    shares: float
    queue_ahead: float
    static_edge: float
    adjusted_edge: float
    confidence: float
    improvement_ticks: int
    flow: Flow | None
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
    fill_notional: float = 0.0
    first_fill_event_ts: int | None = None
    first_fill_received_ts: int | None = None
    last_fill_event_ts: int | None = None


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


def parse_market(raw: dict[str, Any], min_liquidity: float) -> Market | None:
    market_id = str(raw.get("id") or "")
    condition = str(raw.get("conditionId") or "")
    if not market_id or not condition:
        return None
    if not _bool(raw.get("active"), True) or _bool(raw.get("closed"), False):
        return None
    if not _bool(raw.get("enableOrderBook"), True) or not _bool(raw.get("acceptingOrders"), True):
        return None
    liquidity = _number(raw.get("liquidityNum", raw.get("liquidity")), 0.0)
    if liquidity + 1e-12 < min_liquidity:
        return None
    tokens, outcomes = _array(raw.get("clobTokenIds")), _array(raw.get("outcomes"))
    if len(tokens) < 2:
        return None
    yes_idx, no_idx = 0, 1
    for i, outcome in enumerate(outcomes[: len(tokens)]):
        label = str(outcome).lower()
        if label == "yes":
            yes_idx = i
        elif label == "no":
            no_idx = i
    event_id = str(raw.get("eventId") or "")
    if not event_id:
        events = raw.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            event_id = str(events[0].get("id") or "")
    if not event_id:
        event_id = condition

    fee_schedule = raw.get("feeSchedule")
    if isinstance(fee_schedule, dict):
        fee = Fee(
            rate=_number(fee_schedule.get("rate"), -1.0),
            exponent=_number(fee_schedule.get("exponent"), 1.0),
            taker_only=_bool(fee_schedule.get("takerOnly"), True),
            source="gamma_feeSchedule",
        )
        if fee.rate < 0:
            return None
    elif "feesEnabled" in raw and not _bool(raw.get("feesEnabled"), False):
        fee = Fee(0.0, 1.0, True, "gamma_fees_disabled")
    else:
        # Research is fail-closed on fee provenance rather than using a generic fallback.
        return None

    return Market(
        market_id=market_id,
        condition_id=condition,
        event_id=event_id,
        slug=str(raw.get("slug") or market_id),
        yes_token=str(tokens[yes_idx]),
        no_token=str(tokens[no_idx]),
        liquidity=liquidity,
        volume24h=_number(raw.get("volume24hr"), 0.0),
        fee=fee,
    )


def discover_markets(limit: int, min_liquidity: float) -> list[Market]:
    requested = min(max(1, limit), 2000)
    out: list[Market] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < requested and offset < 10000:
        query = urllib.parse.urlencode({
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": offset,
            "order": "liquidityNum",
            "ascending": "false",
            "liquidity_num_min": f"{min_liquidity:.12g}",
        })
        raw, _ = request_json(f"{GAMMA_URL}/markets?{query}")
        rows = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError("unexpected Gamma market response")
        for item in rows:
            if not isinstance(item, dict):
                continue
            market = parse_market(item, min_liquidity)
            if market and market.market_id not in seen:
                out.append(market)
                seen.add(market.market_id)
                if len(out) >= requested:
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
        result: list[Level] = []
        for item in raw.get(key, []) if isinstance(raw.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            p, s = _number(item.get("price"), -1.0), _number(item.get("size"), 0.0)
            if 0 < p < 1 and s > 0:
                result.append(Level(p, s))
        return result
    return Book(token, levels("bids"), levels("asks"),
                max(1e-6, _number(raw.get("tick_size"), 0.01)),
                max(0.0, _number(raw.get("min_order_size"), 1.0)))


def fetch_books(tokens: list[str], batch: int = 40) -> dict[str, Book]:
    out: dict[str, Book] = {}
    for pos in range(0, len(tokens), batch):
        payload = [{"token_id": token} for token in tokens[pos : pos + batch]]
        raw, _ = request_json(f"{CLOB_URL}/books", payload)
        if not isinstance(raw, list):
            raise RuntimeError("unexpected CLOB books response")
        for item in raw:
            if isinstance(item, dict):
                book = parse_book(item)
                if book:
                    out[book.token_id] = book
    return out


def fetch_trades(condition_id: str, start_ts: int, end_ts: int) -> tuple[list[Trade], int]:
    query = urllib.parse.urlencode({
        "market": condition_id,
        "limit": 10000,
        "takerOnly": "true",
        "start": max(0, start_ts),
        "end": max(0, end_ts),
    })
    raw, received_ms = request_json(f"{DATA_URL}/trades?{query}")
    rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
    out: list[Trade] = []
    seen: set[str] = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        token = str(item.get("asset") or "")
        side = str(item.get("side") or "").upper()
        ts = int(_number(item.get("timestamp"), 0))
        price, size = _number(item.get("price"), -1.0), _number(item.get("size"), 0.0)
        if not token or ts <= 0 or not (0 < price < 1) or size <= 0:
            continue
        trade_id = ":".join([
            str(item.get("transactionHash") or ""), token, str(ts), side,
            f"{price:.12g}", f"{size:.12g}",
        ])
        if trade_id in seen:
            continue
        seen.add(trade_id)
        out.append(Trade(trade_id, token, side, price, size, ts))
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


def flow_stats(trades: list[Trade], token_id: str, decision_ts: int, lookback: int, limit_price: float) -> Flow:
    selected = [t for t in trades if t.token_id == token_id and decision_ts - lookback <= t.ts <= decision_ts]
    buy = sum(t.size for t in selected if t.side == "BUY")
    sell = sum(t.size for t in selected if t.side == "SELL")
    compatible = [t for t in selected if t.side == "SELL" and t.price <= limit_price + 1e-12]
    total = buy + sell
    imbalance = (buy - sell) / total if total > 1e-12 else 0.0
    age = decision_ts - max((t.ts for t in selected), default=0) if selected else None
    return Flow(len(selected), buy, sell, sum(t.size for t in compatible), len(compatible), age, imbalance)


def fill_probability_proxy(flow: Flow, queue_ahead: float, shares: float) -> float:
    burden = max(1e-12, queue_ahead + shares)
    clearance = max(0.0, flow.compatible_sell_volume) / burden
    recurrence = min(1.0, max(0.0, flow.compatible_sell_prints) / 3.0)
    return min(1.0, clearance) * recurrence


def activity_eligible(flow: Flow, min_trades: int, min_sell_prints: int, max_event_age: int,
                      min_fill_proxy: float, queue_ahead: float, shares: float,
                      max_sell_toxicity: float) -> bool:
    if flow.trade_count < min_trades or flow.compatible_sell_prints < min_sell_prints:
        return False
    if flow.last_event_age is None or flow.last_event_age > max_event_age:
        return False
    if fill_probability_proxy(flow, queue_ahead, shares) < min_fill_proxy:
        return False
    sell_toxicity = max(0.0, -flow.signed_imbalance)
    return sell_toxicity <= max_sell_toxicity


def build_candidates(markets: list[Market], books: dict[str, Book], flows: dict[str, list[Trade]],
                     decision_ts: int, args: argparse.Namespace) -> tuple[list[Candidate], list[Candidate]]:
    baseline: list[Candidate] = []
    active: list[Candidate] = []
    for market in markets:
        yes, no = books.get(market.yes_token), books.get(market.no_token)
        if not yes or not no:
            continue
        if not (0.01 < yes.midpoint() < 0.99):
            continue
        if yes.spread() > args.max_spread or no.spread() > args.max_spread:
            continue
        q_yes, confidence = micro_signal(yes, no)
        if confidence < args.min_confidence:
            continue
        choices: list[tuple[str, str, Book, float]] = [
            ("YES", market.yes_token, yes, q_yes),
            ("NO", market.no_token, no, 1.0 - q_yes),
        ]
        per_market: list[Candidate] = []
        for side, token, book, fair in choices:
            bid, ask, spread = book.best_bid(), book.best_ask(), book.spread()
            if not (math.isfinite(bid) and math.isfinite(ask) and ask > bid > 0):
                continue
            future_bid = max(0.001, min(0.999, fair - 0.5 * spread)) * (1.0 - args.slippage_bps / 10000.0)
            exit_fee = fee_per_share(future_bid, market.fee)
            generic_adverse = args.adverse_selection_mult * spread * (1.0 - confidence)
            tick = book.tick_size
            limit = bid
            improvement = 0
            base_edge = future_bid - exit_fee - bid - generic_adverse
            if base_edge <= args.min_edge:
                continue

            max_cash = min(args.max_order_usd,
                           args.max_market_fraction * args.starting_capital,
                           args.max_event_fraction * args.starting_capital,
                           args.max_gross_fraction * args.starting_capital)
            shares = max_cash / max(limit, 1e-12)
            touch = book.displayed_size(True, bid)
            shares = min(shares, max(book.min_order_size, 0.25 * max(1.0, touch)))
            if shares < book.min_order_size:
                continue

            recent = flows.get(market.condition_id, [])
            flow = flow_stats(recent, token, decision_ts, args.recent_lookback_seconds, bid) if recent else Flow(0,0,0,0,0,None,0)
            toxicity_penalty = args.toxicity_mult * spread * max(0.0, -flow.signed_imbalance)
            adjusted = base_edge - toxicity_penalty
            queue = book.displayed_size(True, bid)
            proxy = fill_probability_proxy(flow, queue, shares)

            # Spend at most one tick, and only after activity is established and the tick is paid for
            # after the directional toxicity penalty.
            if args.improve_ticks > 0 and flow.compatible_sell_prints >= args.min_sell_prints:
                candidate_limit = bid + tick
                if candidate_limit < ask - 1e-12:
                    candidate_edge = future_bid - exit_fee - candidate_limit - generic_adverse - toxicity_penalty
                    if candidate_edge > args.min_edge:
                        limit = candidate_limit
                        improvement = 1
                        adjusted = candidate_edge
                        queue = book.displayed_size(True, limit)
                        flow = flow_stats(recent, token, decision_ts, args.recent_lookback_seconds, limit)
                        proxy = fill_probability_proxy(flow, queue, shares)

            static = Candidate("baseline_static", market, side, token, limit, shares, queue,
                               base_edge, base_edge, confidence, improvement, flow, proxy, base_edge)
            per_market.append(static)

            if adjusted > args.min_edge and activity_eligible(
                    flow, args.min_recent_trades, args.min_sell_prints, args.max_event_age_seconds,
                    args.min_fill_probability, queue, shares, args.max_sell_toxicity):
                active_score = proxy * adjusted
                active.append(Candidate("active_flow", market, side, token, limit, shares, queue,
                                        base_edge, adjusted, confidence, improvement, flow, proxy, active_score))

        if per_market:
            baseline.append(max(per_market, key=lambda x: x.static_edge))

    baseline.sort(key=lambda x: x.static_edge, reverse=True)
    active.sort(key=lambda x: x.score, reverse=True)
    return baseline, active


def consume(order: ShadowOrder, trades: list[Trade], received_ts: int) -> None:
    tick = max(1e-6, order.candidate.market and 0.01)
    # Use the actual token book tick from candidate construction only for price tolerance through a conservative 1e-6.
    for trade in trades:
        if trade.trade_id in order.seen_ids:
            continue
        if trade.ts <= order.created_ts or trade.ts > order.expires_ts:
            continue
        order.seen_ids.add(trade.trade_id)
        if trade.token_id != order.candidate.token_id or trade.side != "SELL":
            continue
        if trade.price > order.candidate.limit_price + 0.25 * tick:
            continue
        remaining_flow = trade.size
        queue_used = min(order.queue_ahead, remaining_flow)
        order.queue_ahead -= queue_used
        remaining_flow -= queue_used
        fill = min(order.remaining, max(0.0, remaining_flow))
        if fill <= 0:
            continue
        order.remaining -= fill
        order.filled += fill
        order.fill_notional += fill * order.candidate.limit_price
        if order.first_fill_event_ts is None:
            order.first_fill_event_ts = trade.ts
            order.first_fill_received_ts = received_ts
        order.last_fill_event_ts = trade.ts
        if order.remaining <= 1e-12:
            order.remaining = 0.0
            break


def candidate_to_dict(c: Candidate) -> dict[str, Any]:
    flow = c.flow
    return {
        "arm": c.arm, "market_id": c.market.market_id, "condition_id": c.market.condition_id,
        "event_id": c.market.event_id, "slug": c.market.slug, "side": c.side, "token_id": c.token_id,
        "liquidity": c.market.liquidity, "volume24h": c.market.volume24h,
        "fee_source": c.market.fee.source, "limit_price": c.limit_price, "shares": c.shares,
        "queue_ahead": c.queue_ahead, "static_edge": c.static_edge, "adjusted_edge": c.adjusted_edge,
        "confidence": c.confidence, "improvement_ticks": c.improvement_ticks,
        "fill_probability_proxy": c.fill_probability_proxy, "score": c.score,
        "flow": None if flow is None else {
            "trade_count": flow.trade_count, "buy_volume": flow.buy_volume, "sell_volume": flow.sell_volume,
            "compatible_sell_volume": flow.compatible_sell_volume,
            "compatible_sell_prints": flow.compatible_sell_prints,
            "last_event_age": flow.last_event_age, "signed_imbalance": flow.signed_imbalance,
        },
    }


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    started = now_s()
    markets = discover_markets(args.markets, args.min_liquidity)
    tokens = [token for m in markets for token in (m.yes_token, m.no_token)]
    books = fetch_books(tokens)

    activity_markets = sorted(markets, key=lambda m: (m.volume24h, m.liquidity), reverse=True)[: args.activity_scan_markets]
    lookback_start = started - args.recent_lookback_seconds
    flows: dict[str, list[Trade]] = {}
    activity_receive_ms: dict[str, int] = {}
    activity_errors: list[str] = []
    for market in activity_markets:
        try:
            rows, received_ms = fetch_trades(market.condition_id, lookback_start, started)
            flows[market.condition_id] = rows
            activity_receive_ms[market.condition_id] = received_ms
        except Exception as exc:
            activity_errors.append(f"{market.market_id}:{type(exc).__name__}:{exc}")

    baseline, active = build_candidates(markets, books, flows, started, args)
    selected: dict[str, list[Candidate]] = {
        "baseline_static": baseline[: args.max_orders_per_arm],
        "active_flow": active[: args.max_orders_per_arm],
    }
    orders: list[ShadowOrder] = []
    for arm, candidates in selected.items():
        for c in candidates:
            orders.append(ShadowOrder(c, started, now_ms(), started + args.order_ttl_seconds,
                                      c.shares, c.queue_ahead))

    max_horizon = max(args.markout_seconds)
    end_wall = started + args.order_ttl_seconds + max_horizon + args.markout_buffer_seconds
    outcomes: dict[tuple[str, str, str], FillOutcome] = {}
    markout_books_errors = 0

    while now_s() <= end_wall:
        current = now_s()
        # Replay only the finite public flow available to each independent counterfactual arm.
        condition_to_orders: dict[str, list[ShadowOrder]] = {}
        for order in orders:
            if order.remaining <= 1e-12 or current > order.expires_ts + args.trade_index_lag_seconds:
                continue
            condition_to_orders.setdefault(order.candidate.market.condition_id, []).append(order)
        for condition, group in condition_to_orders.items():
            try:
                rows, received_ms = fetch_trades(condition, started, min(current, started + args.order_ttl_seconds))
            except Exception:
                continue
            # Each arm is an independent counterfactual. Within an arm there is at most one order per market/token.
            for order in group:
                consume(order, rows, received_ms // 1000)
                if order.filled <= 0 or order.first_fill_received_ts is None or order.first_fill_event_ts is None:
                    continue
                key = (order.candidate.arm, order.candidate.market.market_id, order.candidate.token_id)
                if key not in outcomes:
                    outcomes[key] = FillOutcome(
                        order.candidate.arm, order.candidate.market.market_id, order.candidate.side,
                        order.candidate.token_id, order.filled, order.candidate.limit_price,
                        order.first_fill_event_ts, order.first_fill_received_ts,
                    )
                else:
                    outcomes[key].shares = order.filled

        filled_tokens = list({out.token_id for out in outcomes.values()})
        fresh_books: dict[str, Book] = {}
        if filled_tokens:
            try:
                fresh_books = fetch_books(filled_tokens)
            except Exception:
                markout_books_errors += 1
        for out in outcomes.values():
            book = fresh_books.get(out.token_id)
            if not book:
                continue
            elapsed = current - out.first_fill_received_ts
            for horizon in args.markout_seconds:
                key = str(horizon)
                if elapsed < horizon or key in out.markouts:
                    continue
                px = book.executable_bid(out.shares, args.slippage_bps)
                if px is None:
                    out.markouts[key] = {"observed_ts": current, "executable_bid": None, "pnl_per_share_pre_fee": None}
                    continue
                out.markouts[key] = {
                    "observed_ts": current,
                    "executable_bid": px,
                    "pnl_per_share_pre_fee": px - out.entry_price,
                }
                if horizon == args.exit_horizon_seconds and out.realized_pnl is None:
                    market = next((o.candidate.market for o in orders
                                   if o.candidate.arm == out.arm and o.candidate.market.market_id == out.market_id), None)
                    if market is not None:
                        fee = fee_per_share(px, market.fee) * out.shares
                        out.realized_exit_price = px
                        out.exit_fee = fee
                        out.realized_pnl = out.shares * (px - out.entry_price) - fee
        if current >= end_wall:
            break
        time.sleep(max(1, args.poll_seconds))

    arm_results: dict[str, Any] = {}
    for arm in selected:
        arm_orders = [o for o in orders if o.candidate.arm == arm]
        arm_outcomes = [o for o in outcomes.values() if o.arm == arm]
        realized = [x.realized_pnl for x in arm_outcomes if x.realized_pnl is not None]
        filled_shares = sum(o.filled for o in arm_orders)
        arm_results[arm] = {
            "candidates_available": len(baseline if arm == "baseline_static" else active),
            "orders": len(arm_orders),
            "fill_orders": sum(o.filled > 0 for o in arm_orders),
            "filled_shares": filled_shares,
            "fill_rate": sum(o.filled > 0 for o in arm_orders) / len(arm_orders) if arm_orders else 0.0,
            "realized_round_trips": len(realized),
            "fill_conditioned_pnl": sum(realized),
            "fill_conditioned_pnl_per_share": (sum(realized) / sum(x.shares for x in arm_outcomes if x.realized_pnl is not None)
                                               if any(x.realized_pnl is not None for x in arm_outcomes) else None),
            "orders_detail": [candidate_to_dict(o.candidate) | {
                "filled_shares": o.filled,
                "remaining_shares": o.remaining,
                "queue_remaining": o.queue_ahead,
                "first_fill_event_ts": o.first_fill_event_ts,
                "first_fill_received_ts": o.first_fill_received_ts,
            } for o in arm_orders],
            "outcomes": [x.__dict__ for x in arm_outcomes],
        }

    return {
        "schema": "hf_active_flow_maker_probe_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_money_execution": False,
        "generated_ts": now_s(),
        "decision_ts": started,
        "universe": {
            "requested_markets": args.markets,
            "discovered_fee_verified_markets": len(markets),
            "books": len(books),
            "activity_scan_markets": len(activity_markets),
            "activity_conditions_with_data": sum(bool(v) for v in flows.values()),
            "activity_errors": activity_errors[:20],
        },
        "safety": {
            "starting_capital": args.starting_capital,
            "min_liquidity": args.min_liquidity,
            "min_edge": args.min_edge,
            "kelly_ceiling": 0.25,
            "max_order_usd": args.max_order_usd,
            "max_market_fraction": args.max_market_fraction,
            "max_event_fraction": args.max_event_fraction,
            "max_gross_fraction": args.max_gross_fraction,
            "max_drawdown": args.max_drawdown,
        },
        "method": {
            "recent_lookback_seconds": args.recent_lookback_seconds,
            "min_recent_trades": args.min_recent_trades,
            "min_sell_prints": args.min_sell_prints,
            "max_event_age_seconds": args.max_event_age_seconds,
            "min_fill_probability": args.min_fill_probability,
            "max_sell_toxicity": args.max_sell_toxicity,
            "toxicity_mult": args.toxicity_mult,
            "order_ttl_seconds": args.order_ttl_seconds,
            "exit_horizon_seconds": args.exit_horizon_seconds,
            "markout_seconds": args.markout_seconds,
            "counterfactual_arms_are_independent": True,
            "fee_provenance_fail_closed": True,
        },
        "arms": arm_results,
        "markout_book_errors": markout_books_errors,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = ["# HF active-flow maker probe", "", "| Arm | Orders | Fill orders | Filled shares | Closed PnL | PnL/share |", "|---|---:|---:|---:|---:|---:|"]
    for arm, data in result["arms"].items():
        pps = data.get("fill_conditioned_pnl_per_share")
        lines.append(f"| {arm} | {data['orders']} | {data['fill_orders']} | {data['filled_shares']:.6f} | {data['fill_conditioned_pnl']:.6f} | {'' if pps is None else f'{pps:.8f}'} |")
    lines.extend(["", f"- fee-verified discovery: {result['universe']['discovered_fee_verified_markets']} markets",
                  f"- activity scan: {result['universe']['activity_scan_markets']} markets",
                  "- all fills are public-tape, event-time FIFO shadow fills; no authenticated orders are submitted."])
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
        raise SystemExit("research settings exceed the authorized aggression floor")
    if args.max_order_usd > 125.0 or args.max_market_fraction > 0.05 or args.max_event_fraction > 0.15 or args.max_gross_fraction > 0.70:
        raise SystemExit("research settings exceed authorized concentration ceilings")
    if args.max_drawdown > 0.15:
        raise SystemExit("max drawdown may not exceed 15%")
    return args


def main() -> int:
    args = parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    result = run_probe(args)
    (output / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "result.md").write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "baseline": result["arms"]["baseline_static"] | {"orders_detail": [], "outcomes": []},
        "active_flow": result["arms"]["active_flow"] | {"orders_detail": [], "outcomes": []},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
