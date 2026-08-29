#!/usr/bin/env python3
"""V7 PAPER binary complete-set maker.

The worker is deliberately narrow and fail-closed.  It trades only a frozen,
prospectively registered cohort; quotes post-only BUYs on YES and NO; measures
fills from public tape event/receive time after our own arrival; models FIFO and
cancel latency; unwinds unmatched inventory against full visible bid depth; and
writes every economic event to the canonical V7 ledger.

No authenticated order path exists here.  Rewards/rebates are never assumed in
expected value and may only be added later from separately audited realized cash
flows.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import v7_execution_core as execution
import v7_execution_ledger as ledger
import v7_market_common as market_common
import v7_model_book_snapshot as snapshots

STRATEGY = "maker_complete_set"
STATE_SCHEMA = "polymarket_v7_complete_set_maker_state_v1"
MARKOUT_HORIZONS_SECONDS = (1, 10, 45, 60, 300)
MIN_AUTHORIZED_EDGE = 0.00005  # 0.5 bp per share
MAX_AUTHORIZED_KELLY = 0.25


class MakerContractError(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    market_id: str
    event_id: str
    prospective_not_before_ms: int


@dataclass(frozen=True)
class Market:
    market_id: str
    event_id: str
    condition_id: str
    yes_token: str
    no_token: str
    liquidity: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class FullBook:
    token_id: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    tick: float
    min_order: float
    exchange_ts_ms: int
    received_ts_ms: int
    snapshot_hash: str

    @property
    def bid(self) -> float:
        return self.bids[0][0]

    @property
    def ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def touch_size(self, bid_side: bool) -> float:
        levels = self.bids if bid_side else self.asks
        best = levels[0][0]
        return sum(size for price, size in levels if abs(price - best) <= 1e-12)

    def depth(self, bid_side: bool, n: int = 5) -> float:
        levels = self.bids if bid_side else self.asks
        best = levels[0][0]
        scale = max(1e-4, 3.0 * self.tick)
        return sum(size * math.exp(-abs(price - best) / scale) for price, size in levels[:n])

    def microprice(self) -> float:
        db, da = self.depth(True), self.depth(False)
        return (self.ask * db + self.bid * da) / (db + da) if db + da > 1e-12 else self.mid

    def causal(self) -> snapshots.CausalBook:
        return snapshots.CausalBook(
            token_id=self.token_id,
            bid=self.bid,
            ask=self.ask,
            bid_size=self.bids[0][1],
            ask_size=self.asks[0][1],
            min_order=self.min_order,
            exchange_ts_ms=self.exchange_ts_ms,
            received_ts_ms=self.received_ts_ms,
            snapshot_hash=self.snapshot_hash,
        )


@dataclass(frozen=True)
class TapeTrade:
    trade_id: str
    token_id: str
    side: str
    price: float
    size: float
    event_ts_ms: int
    received_ms: int


def _finite(value: Any, default: float = math.nan) -> float:
    return market_common.finite(value, default)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{time.time_ns()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MakerContractError("config:not_object")
    if value.get("paper_only") is not True or value.get("authenticated_execution") is not False:
        raise MakerContractError("config:not_paper_only")
    kelly = _finite(value.get("kelly_fraction"))
    if not math.isfinite(kelly) or not 0.0 < kelly <= MAX_AUTHORIZED_KELLY:
        raise MakerContractError("config:kelly_outside_authority")
    edge = _finite(value.get("min_post_cost_edge"))
    if not math.isfinite(edge) or edge + 1e-15 < MIN_AUTHORIZED_EDGE:
        raise MakerContractError("config:min_edge_below_authority")
    if _finite(value.get("min_liquidity"), -1.0) < 2.0:
        raise MakerContractError("config:min_liquidity_below_authority")
    if int(value.get("market_limit", 0)) <= 0 or int(value.get("market_limit", 0)) > 1000:
        raise MakerContractError("config:market_limit_outside_authority")
    capital = _finite(value.get("paper_capital_usd"))
    if not math.isfinite(capital) or capital <= 0.0:
        raise MakerContractError("config:paper_capital_invalid")
    candidates = value.get("candidate_market_ids")
    if not isinstance(candidates, list) or not candidates:
        raise MakerContractError("config:frozen_cohort_missing")
    seen: set[str] = set()
    for row in candidates:
        if not isinstance(row, dict):
            raise MakerContractError("config:candidate_invalid")
        mid = str(row.get("market_id") or "")
        event = str(row.get("event_id") or "")
        not_before = int(row.get("prospective_not_before_ms") or 0)
        if not mid or not event or not_before <= 0 or mid in seen:
            raise MakerContractError("config:candidate_invalid")
        seen.add(mid)
    if _finite(value.get("assumed_rewards_usd"), 0.0) != 0.0:
        raise MakerContractError("config:unrealized_rewards_forbidden")
    return value


def candidates_from_config(cfg: dict[str, Any]) -> dict[str, Candidate]:
    return {
        str(row["market_id"]): Candidate(
            str(row["market_id"]), str(row["event_id"]), int(row["prospective_not_before_ms"])
        )
        for row in cfg["candidate_market_ids"]
    }


def _parse_market(raw: dict[str, Any]) -> Market | None:
    ids = [str(x) for x in market_common.parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().lower() for x in market_common.parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return None
    yi, ni = 0, 1
    for index, name in enumerate(outcomes[: len(ids)]):
        if name == "yes":
            yi = index
        elif name == "no":
            ni = index
    market_id = str(raw.get("id") or "")
    condition = str(raw.get("conditionId") or "")
    event_id = str(raw.get("eventId") or condition or market_id)
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        event_id = str(events[0].get("id") or event_id)
    liquidity = max(0.0, _finite(raw.get("liquidityNum"), _finite(raw.get("liquidity"), 0.0)))
    if not market_id or not condition:
        return None
    return Market(market_id, event_id, condition, ids[yi], ids[ni], liquidity, raw)


def discover_frozen_markets(
    gamma: str,
    candidates: dict[str, Candidate],
    *,
    market_limit: int,
    min_liquidity: float,
    request_json: Callable[..., Any] = market_common.request_json,
) -> dict[str, Market]:
    wanted = set(candidates)
    found: dict[str, Market] = {}
    offset = 0
    scanned = 0
    while wanted - set(found) and scanned < max(100, int(market_limit)) and offset < 5000:
        batch_limit = min(100, max(1, int(market_limit) - scanned))
        query = urllib.parse.urlencode({
            "active": "true", "closed": "false", "limit": batch_limit,
            "offset": offset, "order": "liquidityNum", "ascending": "false",
        })
        root = request_json(gamma.rstrip("/") + "/markets?" + query)
        rows = root if isinstance(root, list) else root.get("markets", []) if isinstance(root, dict) else []
        if not rows:
            break
        scanned += len(rows)
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            market = _parse_market(raw)
            if market is None or market.market_id not in wanted or market.liquidity < min_liquidity:
                continue
            expected_event = candidates[market.market_id].event_id
            if market.event_id != expected_event:
                continue
            found[market.market_id] = market
        if len(rows) < batch_limit:
            break
        offset += batch_limit
    return found


def _levels(rows: Any, reverse: bool) -> tuple[tuple[float, float], ...]:
    out: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        price, size = _finite(row.get("price")), _finite(row.get("size"), 0.0)
        if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
            out.append((price, size))
    out.sort(reverse=reverse)
    return tuple(out)


def fetch_full_books(
    clob: str,
    tokens: Iterable[str],
    *,
    batch_size: int = 80,
    request_json: Callable[..., Any] = market_common.request_json,
) -> dict[str, FullBook]:
    unique = list(dict.fromkeys(str(token) for token in tokens if str(token)))
    out: dict[str, FullBook] = {}
    for index in range(0, len(unique), max(1, int(batch_size))):
        batch = unique[index:index + max(1, int(batch_size))]
        raw = request_json(clob.rstrip("/") + "/books", [{"token_id": token} for token in batch])
        received = _now_ms()
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            causal = snapshots.parse_causal_book(row, received_ts_ms=received)
            bids, asks = _levels(row.get("bids"), True), _levels(row.get("asks"), False)
            if causal is None or not bids or not asks:
                continue
            tick = max(1e-6, _finite(row.get("tick_size"), 0.01))
            out[causal.token_id] = FullBook(
                causal.token_id, bids, asks, tick, causal.min_order,
                causal.exchange_ts_ms, causal.received_ts_ms, causal.snapshot_hash,
            )
    return out


def full_depth_sell_vwap(book: FullBook, shares: float) -> float | None:
    target = max(0.0, float(shares))
    if target <= 1e-12:
        return None
    remaining, proceeds = target, 0.0
    for price, size in book.bids:
        take = min(remaining, size)
        proceeds += take * price
        remaining -= take
        if remaining <= 1e-12:
            return proceeds / target
    return None


def _fair_probabilities(yes: FullBook, no: FullBook) -> tuple[float, float]:
    yes_fair = 0.5 * (yes.mid + (1.0 - no.mid))
    yes_fair = min(0.999, max(0.001, yes_fair))
    return yes_fair, 1.0 - yes_fair


def _inside_price(book: FullBook) -> float | None:
    candidate = book.bid + book.tick
    if candidate < book.ask - 1e-12 and candidate < 1.0:
        return candidate
    return None


def _tape_flow(trades: list[TapeTrade], now_s: int) -> market_common.TapeFlow:
    rows = [market_common.TapeTrade(t.event_ts_ms // 1000, t.token_id, t.side, t.price, t.size) for t in trades]
    return market_common.TapeFlow(rows, now=now_s)


def _maker_state(
    book: FullBook,
    *,
    token: str,
    fair: float,
    limit: float,
    target_shares: float,
    details: market_common.FeeDetails,
    flow: market_common.TapeFlow,
    cfg: dict[str, Any],
) -> execution.MakerState:
    ttl = int(cfg["ttl_seconds"])
    lookback = max(60, int(cfg["flow_lookback_seconds"]))
    rate = flow.compatible_sell_rate(token, limit, lookback_seconds=lookback)
    compatible_volume = rate * ttl
    bid_depth, ask_depth = book.depth(True), book.depth(False)
    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-9)
    ofi = flow.signed_flow(token, lookback_seconds=lookback)
    queue = book.touch_size(True) if abs(limit - book.bid) <= max(1e-9, 0.25 * book.tick) else 0.0
    exit_vwap = full_depth_sell_vwap(book, target_shares)
    executable_exit = exit_vwap if exit_vwap is not None else book.bid
    entry_fee = market_common.fee_per_share(limit, details, taker=False)
    exit_fee = market_common.fee_per_share(executable_exit, details, taker=True)
    adverse = max(0.0, _finite(cfg.get("adverse_markout_fraction"), 0.10) * book.spread)
    partial_loss = max(0.0, limit + entry_fee - executable_exit + exit_fee)
    capital_rate = max(0.0, _finite(cfg.get("capital_cost_bps_per_hour"), 0.0)) / 10000.0 / 3600.0
    return execution.MakerState(
        side="BUY", limit_price=limit, fair_exit_price=fair,
        queue_ahead=queue, own_size=target_shares, compatible_flow=compatible_volume,
        flow_horizon_seconds=ttl, ofi=ofi, imbalance=imbalance,
        microprice=book.microprice(), midpoint=book.mid,
        displayed_depth=max(1e-9, bid_depth + ask_depth),
        entry_fee_per_share=entry_fee, exit_fee_per_share=exit_fee,
        slippage_per_share=max(0.0, book.bid - executable_exit),
        adverse_markout_per_share=adverse,
        partial_unwind_loss_per_share=partial_loss,
        expected_partial_fraction=_finite(cfg.get("expected_partial_fraction"), 0.25),
        capital_usd=target_shares * limit,
        capital_time_rate_per_second=capital_rate,
        expected_rest_seconds=ttl,
        latency_seconds=max(0.0, int(cfg["cancel_latency_ms"]) / 1000.0),
    )


def conservative_joint_distribution(p_yes: float, p_no: float, haircut: float) -> execution.JointStateDistribution:
    p1, p2 = execution.clamp(p_yes, 0.0, 1.0), execution.clamp(p_no, 0.0, 1.0)
    lower, upper = max(0.0, p1 + p2 - 1.0), min(p1, p2)
    p_both = min(upper, max(lower, execution.clamp(haircut, 0.0, 1.0) * upper))
    probs = {0: 1.0 - p1 - p2 + p_both, 1: p1 - p_both, 2: p2 - p_both, 3: p_both}
    dist = execution.JointStateDistribution(2, probs, observations=0)
    dist.validate()
    return dist


def _bundle_state_pnl(
    *,
    qty: float,
    yes_limit: float,
    no_limit: float,
    yes_book: FullBook,
    no_book: FullBook,
    yes_fee: market_common.FeeDetails,
    no_fee: market_common.FeeDetails,
) -> dict[int, float] | None:
    y_exit, n_exit = full_depth_sell_vwap(yes_book, qty), full_depth_sell_vwap(no_book, qty)
    if y_exit is None or n_exit is None:
        return None
    y_entry_fee = market_common.fee_per_share(yes_limit, yes_fee, taker=False)
    n_entry_fee = market_common.fee_per_share(no_limit, no_fee, taker=False)
    y_exit_fee = market_common.fee_per_share(y_exit, yes_fee, taker=True)
    n_exit_fee = market_common.fee_per_share(n_exit, no_fee, taker=True)
    return {
        0: 0.0,
        1: qty * (y_exit - yes_limit - y_entry_fee - y_exit_fee),
        2: qty * (n_exit - no_limit - n_entry_fee - n_exit_fee),
        3: qty * (1.0 - yes_limit - no_limit - y_entry_fee - n_entry_fee),
    }


def _target_shares(
    yes: FullBook,
    no: FullBook,
    *,
    yes_limit: float,
    no_limit: float,
    cfg: dict[str, Any],
    flow: market_common.TapeFlow,
) -> float | None:
    cost = yes_limit + no_limit
    if cost <= 0.0:
        return None
    capital_shares = _finite(cfg["paper_capital_usd"]) * _finite(cfg["kelly_fraction"]) / cost
    lookback = max(60, int(cfg["flow_lookback_seconds"]))
    ttl = int(cfg["ttl_seconds"])
    y_flow = flow.compatible_sell_rate(yes.token_id, yes_limit, lookback_seconds=lookback) * ttl
    n_flow = flow.compatible_sell_rate(no.token_id, no_limit, lookback_seconds=lookback) * ttl
    flow_cap = max(0.0, min(y_flow, n_flow)) * max(0.0, _finite(cfg.get("flow_size_fraction"), 0.25))
    minimum = max(yes.min_order, no.min_order)
    if flow_cap + 1e-12 < minimum:
        return None
    shares = min(capital_shares, flow_cap)
    return shares if shares + 1e-12 >= minimum else None


def choose_quote(
    market: Market,
    yes: FullBook,
    no: FullBook,
    *,
    yes_fee: market_common.FeeDetails,
    no_fee: market_common.FeeDetails,
    recent_trades: list[TapeTrade],
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    if not yes_fee.verified or not no_fee.verified:
        return None
    flow = _tape_flow(recent_trades, max(yes.received_ts_ms, no.received_ts_ms) // 1000)
    y_inside, n_inside = _inside_price(yes), _inside_price(no)
    y_options = [("touch", yes.bid)] + ([] if y_inside is None else [("inside", y_inside)])
    n_options = [("touch", no.bid)] + ([] if n_inside is None else [("inside", n_inside)])
    fair_y, fair_n = _fair_probabilities(yes, no)
    best: dict[str, Any] | None = None
    touch_ev: float | None = None
    for y_mode, y_limit in y_options:
        for n_mode, n_limit in n_options:
            qty = _target_shares(yes, no, yes_limit=y_limit, no_limit=n_limit, cfg=cfg, flow=flow)
            if qty is None:
                continue
            y_state = _maker_state(yes, token=yes.token_id, fair=fair_y, limit=y_limit, target_shares=qty, details=yes_fee, flow=flow, cfg=cfg)
            n_state = _maker_state(no, token=no.token_id, fair=fair_n, limit=n_limit, target_shares=qty, details=no_fee, flow=flow, cfg=cfg)
            y_ev, n_ev = execution.maker_fill_conditioned_ev(y_state), execution.maker_fill_conditioned_ev(n_state)
            if min(y_ev.fill_probability, n_ev.fill_probability) + 1e-15 < _finite(cfg["min_fill_probability"]):
                continue
            state_pnl = _bundle_state_pnl(qty=qty, yes_limit=y_limit, no_limit=n_limit, yes_book=yes, no_book=no, yes_fee=yes_fee, no_fee=no_fee)
            if state_pnl is None:
                continue
            full_edge = state_pnl[3] / qty
            if full_edge + 1e-15 < _finite(cfg["min_post_cost_edge"]):
                continue
            dist = conservative_joint_distribution(y_ev.fill_probability, n_ev.fill_probability, _finite(cfg["joint_completion_haircut"]))
            if dist.probabilities[3] + 1e-15 < _finite(cfg["min_joint_completion_probability"]):
                continue
            bundle = execution.joint_bundle_ev(
                dist, state_pnl,
                capital_usd=qty * (y_limit + n_limit),
                capital_time_rate_per_second=max(0.0, _finite(cfg.get("capital_cost_bps_per_hour"), 0.0)) / 10000.0 / 3600.0,
                expected_latency_seconds=float(cfg["ttl_seconds"]) + int(cfg["cancel_latency_ms"]) / 1000.0,
            )
            if y_mode == "touch" and n_mode == "touch":
                touch_ev = bundle.expected_value
            if bundle.expected_value <= 0.0:
                continue
            # Inside-spread improvement must be incremental versus the fully
            # admissible at-touch baseline, never merely "less bad".
            if (y_mode == "inside" or n_mode == "inside") and touch_ev is not None and bundle.expected_value <= touch_ev + 1e-12:
                continue
            row = {
                "market_id": market.market_id, "event_id": market.event_id,
                "qty": qty, "yes_limit": y_limit, "no_limit": n_limit,
                "yes_mode": y_mode, "no_mode": n_mode,
                "yes_fill_probability": y_ev.fill_probability,
                "no_fill_probability": n_ev.fill_probability,
                "joint_distribution": dict(dist.probabilities),
                "joint_completion_probability": dist.probabilities[3],
                "full_completion_edge_per_share": full_edge,
                "expected_bundle_ev": bundle.expected_value,
                "yes_queue": y_state.queue_ahead, "no_queue": n_state.queue_ahead,
                "yes_leg_ev": y_ev.expected_value, "no_leg_ev": n_ev.expected_value,
                "joint_source": "conservative_frechet_haircut_proxy",
            }
            if best is None or row["expected_bundle_ev"] > best["expected_bundle_ev"]:
                best = row
    return best


def _trade_id(row: dict[str, str]) -> str:
    raw = "|".join(str(row.get(key) or "") for key in (
        "transaction_hash", "asset_id", "timestamp", "received_ms", "side", "price", "size"
    ))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_new_tape(path: Path, state: dict[str, Any]) -> list[TapeTrade]:
    watermark = int(state.get("tape_watermark_received_ms") or 0)
    seen_at_watermark = set(str(x) for x in state.get("tape_watermark_trade_ids", []))
    rows: list[TapeTrade] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                received = int(_finite(row.get("received_ms"), 0.0))
                event_ms = int(_finite(row.get("timestamp"), 0.0) * 1000)
                token = str(row.get("asset_id") or "")
                side = str(row.get("side") or "").upper()
                price, size = _finite(row.get("price")), _finite(row.get("size"), 0.0)
                tid = _trade_id(row)
                if received < watermark or (received == watermark and tid in seen_at_watermark):
                    continue
                if received <= 0 or event_ms <= 0 or not token or side not in {"BUY", "SELL"} or not math.isfinite(price) or not 0.0 < price < 1.0 or size <= 0.0:
                    continue
                rows.append(TapeTrade(tid, token, side, price, size, event_ms, received))
    except OSError:
        return []
    rows.sort(key=lambda t: (t.received_ms, t.event_ts_ms, t.trade_id))
    return rows


def advance_tape_watermark(state: dict[str, Any], rows: list[TapeTrade]) -> None:
    if not rows:
        return
    maximum = max(row.received_ms for row in rows)
    ids = sorted({row.trade_id for row in rows if row.received_ms == maximum})
    state["tape_watermark_received_ms"] = maximum
    state["tape_watermark_trade_ids"] = ids


def trade_can_fill(trade: TapeTrade, order: dict[str, Any], *, cancel_effective_ms: int | None = None) -> bool:
    if trade.token_id != str(order["token_id"]) or trade.side != "SELL":
        return False
    if trade.price > float(order["limit_price"]) + 1e-12:
        return False
    if trade.event_ts_ms <= int(order["arrival_event_ms"]) or trade.received_ms <= int(order["arrival_received_ms"]):
        return False
    deadline_event = int(order["arrival_event_ms"]) + int(order["ttl_ms"]) + int(order["cancel_latency_ms"])
    deadline_receive = int(order["arrival_received_ms"]) + int(order["ttl_ms"]) + int(order["cancel_latency_ms"])
    if cancel_effective_ms is not None:
        deadline_receive = min(deadline_receive, int(cancel_effective_ms))
    return trade.event_ts_ms <= deadline_event and trade.received_ms <= deadline_receive


def apply_trade_to_order(trade: TapeTrade, order: dict[str, Any]) -> float:
    if not trade_can_fill(trade, order, cancel_effective_ms=order.get("cancel_effective_ms")):
        return 0.0
    remaining_public = max(0.0, trade.size)
    queue = max(0.0, float(order.get("queue_remaining", 0.0)))
    consumed_queue = min(queue, remaining_public)
    order["queue_remaining"] = queue - consumed_queue
    remaining_public -= consumed_queue
    if remaining_public <= 1e-12:
        return 0.0
    own_remaining = max(0.0, float(order["target_shares"]) - float(order.get("filled_shares", 0.0)))
    fill = min(own_remaining, remaining_public)
    if fill > 0.0:
        order["filled_shares"] = float(order.get("filled_shares", 0.0)) + fill
    return fill


def _load_state(path: Path, model_sha: str) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict) or not state:
        return {
            "schema": STATE_SCHEMA, "model_sha": model_sha, "paper_only": True,
            "authenticated_execution": False, "bundles": {}, "markout_watch": {},
            "tape_watermark_received_ms": 0, "tape_watermark_trade_ids": [],
        }
    if state.get("schema") != STATE_SCHEMA or state.get("model_sha") != model_sha:
        raise MakerContractError("state:model_sha_or_schema_mismatch")
    if state.get("paper_only") is not True or state.get("authenticated_execution") is not False:
        raise MakerContractError("state:not_paper_only")
    return state


def _fill_id(order_id: str, trade_id: str, offset: int) -> str:
    return hashlib.sha256(f"{order_id}|{trade_id}|{offset}".encode("utf-8")).hexdigest()


def _bundle_id(model_sha: str, market_id: str, decision_ms: int) -> str:
    return hashlib.sha256(f"{model_sha}|{market_id}|{decision_ms}".encode("utf-8")).hexdigest()[:32]


def submit_bundle(
    writer: ledger.CanonicalLedgerWriter,
    state: dict[str, Any],
    market: Market,
    candidate: Candidate,
    quote: dict[str, Any],
    yes: FullBook,
    no: FullBook,
    yes_fee: market_common.FeeDetails,
    no_fee: market_common.FeeDetails,
    *,
    model_sha: str,
    cfg: dict[str, Any],
    decision_ms: int,
    snapshot_id: str,
) -> str:
    bundle_id = _bundle_id(model_sha, market.market_id, decision_ms)
    target = float(quote["qty"])
    required = [
        {"leg_id": "YES", "token_id": market.yes_token, "target_quantity": target},
        {"leg_id": "NO", "token_id": market.no_token, "target_quantity": target},
    ]
    exchange_ms = min(yes.exchange_ts_ms, no.exchange_ts_ms)
    receive_ms = max(yes.received_ts_ms, no.received_ts_ms)
    writer.append(ledger.LedgerEvent(
        event_type="CANDIDATE", strategy=STRATEGY, model_sha=model_sha,
        candidate_id=f"maker:{market.market_id}", bundle_id=bundle_id,
        market_id=market.market_id, event_id=market.event_id,
        exchange_ts_ms=exchange_ms, receive_ts_ms=receive_ms, decision_ts_ms=decision_ms,
        book_snapshot_id=snapshot_id, predicted_alpha=float(quote["full_completion_edge_per_share"]),
        predicted_fill_probability=float(quote["joint_completion_probability"]),
        expected_ev=float(quote["expected_bundle_ev"]),
        metadata={
            "model_family": STRATEGY, "horizon_seconds": int(cfg["ttl_seconds"]),
            "joint_target_legs": required, "joint_distribution": quote["joint_distribution"],
            "joint_source": quote["joint_source"], "prospective_not_before_ms": candidate.prospective_not_before_ms,
            "same_window_discovery_credit": False, "assumed_rewards_usd": 0.0,
        },
    ))
    legs = {
        "YES": (market.yes_token, yes, yes_fee, float(quote["yes_limit"]), float(quote["yes_queue"]), str(quote["yes_mode"]), float(quote["yes_fill_probability"]), float(quote["yes_leg_ev"])),
        "NO": (market.no_token, no, no_fee, float(quote["no_limit"]), float(quote["no_queue"]), str(quote["no_mode"]), float(quote["no_fill_probability"]), float(quote["no_leg_ev"])),
    }
    bundle = {
        "bundle_id": bundle_id, "market_id": market.market_id, "event_id": market.event_id,
        "created_ms": decision_ms, "state": "RESTING", "target_shares": target,
        "first_fill_ms": None, "cancel_requested_ms": None, "cancel_effective_ms": None,
        "final_recorded": False, "snapshot_id": snapshot_id, "legs": {},
    }
    for leg_name, (token, book, details, limit, queue, mode, fill_prob, leg_ev) in legs.items():
        order_id = f"{bundle_id}:{leg_name.lower()}"
        arrival_event = book.exchange_ts_ms
        arrival_received = max(decision_ms, book.received_ts_ms) + int(cfg.get("submission_latency_ms", 0))
        order = {
            "order_id": order_id, "leg_id": leg_name, "token_id": token,
            "limit_price": limit, "queue_remaining": queue, "target_shares": target,
            "filled_shares": 0.0, "entry_notional": 0.0, "entry_fee": 0.0,
            "arrival_event_ms": arrival_event, "arrival_received_ms": arrival_received,
            "ttl_ms": int(cfg["ttl_seconds"]) * 1000,
            "cancel_latency_ms": int(cfg["cancel_latency_ms"]), "cancel_effective_ms": None,
            "cancel_reference_bid": None, "mode": mode, "fee_rate": details.rate,
            "fee_source": details.source, "fee_exponent": details.exponent,
            "fee_taker_only": details.taker_only,
        }
        bundle["legs"][leg_name] = order
        writer.append(ledger.LedgerEvent(
            event_type="ORDER_SUBMITTED", strategy=STRATEGY, model_sha=model_sha,
            candidate_id=f"maker:{market.market_id}", bundle_id=bundle_id,
            order_id=order_id, leg_id=leg_name, market_id=market.market_id,
            event_id=market.event_id, token_id=token, exchange_ts_ms=arrival_event,
            receive_ts_ms=book.received_ts_ms, decision_ts_ms=decision_ms,
            book_snapshot_id=snapshot_id, side="BUY", bid=book.bid, ask=book.ask,
            bid_depth=book.depth(True), ask_depth=book.depth(False), queue_ahead=queue,
            limit_price=limit, predicted_fill_probability=fill_prob, expected_ev=leg_ev,
            intended_action="POST_ONLY_BUY", intended_size=target, order_state="RESTING",
            timeout_ms=int(cfg["ttl_seconds"]) * 1000,
            metadata={"model_family": STRATEGY, "horizon_seconds": int(cfg["ttl_seconds"]), "joint_target_legs": required, "quote_mode": mode, "assumed_rewards_usd": 0.0},
        ))
    state["bundles"][bundle_id] = bundle
    return bundle_id


def _fee_details_from_order(order: dict[str, Any]) -> market_common.FeeDetails:
    return market_common.FeeDetails(
        rate=float(order["fee_rate"]), exponent=float(order["fee_exponent"]),
        taker_only=bool(order["fee_taker_only"]), verified=True, source=str(order["fee_source"]),
    )


def process_trade(writer: ledger.CanonicalLedgerWriter, bundle: dict[str, Any], trade: TapeTrade, *, model_sha: str) -> int:
    fills = 0
    for leg_name, order in bundle["legs"].items():
        before = float(order.get("filled_shares", 0.0))
        fill_qty = apply_trade_to_order(trade, order)
        if fill_qty <= 1e-12:
            continue
        details = _fee_details_from_order(order)
        fill_price = float(order["limit_price"])
        fee = fill_qty * market_common.fee_per_share(fill_price, details, taker=False)
        order["entry_notional"] = float(order.get("entry_notional", 0.0)) + fill_qty * fill_price
        order["entry_fee"] = float(order.get("entry_fee", 0.0)) + fee
        if bundle.get("first_fill_ms") is None:
            bundle["first_fill_ms"] = trade.received_ms
        fid = _fill_id(str(order["order_id"]), trade.trade_id, int(round(before * 1_000_000)))
        writer.append(ledger.LedgerEvent(
            event_type="FILL", strategy=STRATEGY, model_sha=model_sha,
            bundle_id=str(bundle["bundle_id"]), order_id=str(order["order_id"]),
            fill_id=fid, leg_id=leg_name, market_id=str(bundle["market_id"]),
            event_id=str(bundle["event_id"]), token_id=str(order["token_id"]),
            exchange_ts_ms=trade.event_ts_ms, receive_ts_ms=trade.received_ms,
            side="BUY", fill_price=fill_price, filled_size=fill_qty,
            fee=fee, fee_rate=float(order["fee_rate"]), fee_source=str(order["fee_source"]),
            metadata={"model_family": STRATEGY, "horizon_seconds": int(order["ttl_ms"]) // 1000, "public_trade_id": trade.trade_id, "maker": True},
        ))
        bundle.setdefault("fills", []).append({"fill_id": fid, "order_id": order["order_id"], "leg_id": leg_name, "token_id": order["token_id"], "fill_price": fill_price, "filled_size": fill_qty, "fee": fee, "received_ms": trade.received_ms})
        fills += 1
    return fills


def request_cancel(writer: ledger.CanonicalLedgerWriter, bundle: dict[str, Any], books: dict[str, FullBook], *, model_sha: str, now_ms: int, cfg: dict[str, Any]) -> bool:
    if bundle.get("cancel_requested_ms") is not None:
        return True
    for order in bundle["legs"].values():
        book = books.get(str(order["token_id"]))
        if book is None:
            return False
    bundle["cancel_requested_ms"] = now_ms
    bundle["cancel_effective_ms"] = now_ms + int(cfg["cancel_latency_ms"])
    bundle["state"] = "CANCEL_PENDING"
    for order in bundle["legs"].values():
        book = books[str(order["token_id"])]
        order["cancel_reference_bid"] = book.bid
        order["cancel_effective_ms"] = bundle["cancel_effective_ms"]
        writer.append(ledger.LedgerEvent(
            event_type="ORDER_STATE", strategy=STRATEGY, model_sha=model_sha,
            bundle_id=str(bundle["bundle_id"]), order_id=str(order["order_id"]),
            leg_id=str(order["leg_id"]), market_id=str(bundle["market_id"]),
            event_id=str(bundle["event_id"]), token_id=str(order["token_id"]),
            order_state="CANCEL_PENDING", cancel_reason="ttl_expired",
            metadata={"model_family": STRATEGY, "cancel_requested_ms": now_ms, "cancel_effective_ms": bundle["cancel_effective_ms"]},
        ))
    return True


def _entry_totals(bundle: dict[str, Any]) -> tuple[float, float]:
    notional = sum(float(order.get("entry_notional", 0.0)) for order in bundle["legs"].values())
    fees = sum(float(order.get("entry_fee", 0.0)) for order in bundle["legs"].values())
    return notional, fees


def settle_bundle(
    writer: ledger.CanonicalLedgerWriter,
    bundle: dict[str, Any],
    books: dict[str, FullBook],
    *,
    model_sha: str,
    now_ms: int,
    cfg: dict[str, Any],
) -> bool:
    if bundle.get("final_recorded"):
        return True
    orders = bundle["legs"]
    y, n = orders["YES"], orders["NO"]
    y_fill, n_fill = float(y.get("filled_shares", 0.0)), float(n.get("filled_shares", 0.0))
    target = float(bundle["target_shares"])
    fully_complete = y_fill + 1e-12 >= target and n_fill + 1e-12 >= target
    cancel_effective = bundle.get("cancel_effective_ms")
    if not fully_complete and (cancel_effective is None or now_ms < int(cancel_effective)):
        return False
    matched = min(y_fill, n_fill)
    excess = {"YES": max(0.0, y_fill - matched), "NO": max(0.0, n_fill - matched)}
    raw_exit_proceeds = 0.0
    exit_fees = 0.0
    unwind_loss = 0.0
    latency_cost = 0.0
    slippage = 0.0
    for leg_name, qty in excess.items():
        if qty <= 1e-12:
            continue
        order = orders[leg_name]
        book = books.get(str(order["token_id"]))
        if book is None:
            bundle["state"] = "UNWIND_PENDING"
            return False
        vwap = full_depth_sell_vwap(book, qty)
        if vwap is None:
            bundle["state"] = "UNWIND_PENDING"
            return False
        details = _fee_details_from_order(order)
        fee = qty * market_common.fee_per_share(vwap, details, taker=True)
        raw_exit_proceeds += qty * vwap
        exit_fees += fee
        entry_avg = float(order["entry_notional"]) / max(y_fill if leg_name == "YES" else n_fill, 1e-12)
        request_bid = float(order.get("cancel_reference_bid") if order.get("cancel_reference_bid") is not None else book.bid)
        unwind_component = qty * max(0.0, entry_avg - request_bid)
        latency_component = qty * max(0.0, request_bid - book.bid)
        slippage_component = qty * max(0.0, book.bid - vwap)
        unwind_loss += unwind_component
        latency_cost += latency_component
        slippage += slippage_component
        writer.append(ledger.LedgerEvent(
            event_type="EXIT", strategy=STRATEGY, model_sha=model_sha,
            bundle_id=str(bundle["bundle_id"]), order_id=str(order["order_id"]),
            leg_id=leg_name, market_id=str(bundle["market_id"]), event_id=str(bundle["event_id"]),
            token_id=str(order["token_id"]), side="SELL", fill_price=vwap, filled_size=qty,
            fee=fee, fee_rate=float(order["fee_rate"]), fee_source=str(order["fee_source"]),
            slippage=slippage_component, unwind_loss=unwind_component, latency_cost=latency_component,
            metadata={"model_family": STRATEGY, "unwind": True, "full_visible_bid_depth": True, "cancel_reference_bid": request_bid, "effective_top_bid": book.bid},
        ))
    entry_notional, entry_fees = _entry_totals(bundle)
    first_fill = int(bundle.get("first_fill_ms") or now_ms)
    duration_ms = max(0, now_ms - first_fill)
    capital_rate = max(0.0, _finite(cfg.get("capital_cost_bps_per_hour"), 0.0)) / 10000.0
    capital_cost = entry_notional * capital_rate * duration_ms / 3_600_000.0
    realized_cashflow = matched + raw_exit_proceeds
    final_pnl = realized_cashflow - entry_notional - entry_fees - exit_fees - capital_cost
    writer.append(ledger.LedgerEvent(
        event_type="FINAL", strategy=STRATEGY, model_sha=model_sha,
        bundle_id=str(bundle["bundle_id"]), market_id=str(bundle["market_id"]),
        event_id=str(bundle["event_id"]), slippage=slippage, unwind_loss=unwind_loss,
        capital_cost=capital_cost, latency_cost=latency_cost,
        realized_cashflow=realized_cashflow, final_pnl=final_pnl, capital_duration_ms=duration_ms,
        metadata={
            "model_family": STRATEGY, "horizon_seconds": int(cfg["ttl_seconds"]),
            "realized": True, "cost_vector_complete": True, "unwind_accounted": True,
            "terminal_basis": "guaranteed_binary_complete_set_payoff_plus_full_depth_unwind",
            "matched_complete_set_shares": matched, "entry_fee_total": entry_fees,
            "exit_fee_total": exit_fees, "assumed_rewards_usd": 0.0,
        },
    ))
    for order in orders.values():
        writer.append(ledger.LedgerEvent(
            event_type="ORDER_STATE", strategy=STRATEGY, model_sha=model_sha,
            bundle_id=str(bundle["bundle_id"]), order_id=str(order["order_id"]),
            leg_id=str(order["leg_id"]), market_id=str(bundle["market_id"]), event_id=str(bundle["event_id"]),
            token_id=str(order["token_id"]), order_state="DONE" if fully_complete else "CANCELLED",
            cancel_reason=None if fully_complete else "ttl_cancel_effective",
            metadata={"model_family": STRATEGY},
        ))
    bundle["state"] = "FINAL"
    bundle["final_recorded"] = True
    bundle["final_pnl"] = final_pnl
    return True


def update_markouts(
    writer: ledger.CanonicalLedgerWriter,
    state: dict[str, Any],
    books: dict[str, FullBook],
    *,
    model_sha: str,
    now_ms: int,
    cfg: dict[str, Any],
) -> None:
    watches = state.setdefault("markout_watch", {})
    for bundle in state.get("bundles", {}).values():
        for fill in bundle.get("fills", []):
            fid = str(fill["fill_id"])
            if fid in watches:
                continue
            watches[fid] = {
                **fill, "bundle_id": bundle["bundle_id"], "market_id": bundle["market_id"],
                "event_id": bundle["event_id"], "done": [], "censored": [],
            }
    max_delay = int(cfg["markout_max_delay_seconds"]) * 1000
    for fid, watch in list(watches.items()):
        done, censored = set(watch.get("done", [])), set(watch.get("censored", []))
        for horizon in MARKOUT_HORIZONS_SECONDS:
            key = f"{horizon}s"
            if key in done or key in censored:
                continue
            due = int(watch["received_ms"]) + horizon * 1000
            if now_ms < due:
                continue
            book = books.get(str(watch["token_id"]))
            if book is None or now_ms - due > max_delay:
                censored.add(key)
                continue
            vwap = full_depth_sell_vwap(book, float(watch["filled_size"]))
            if vwap is None:
                if now_ms - due > max_delay:
                    censored.add(key)
                continue
            order_bundle = state["bundles"].get(str(watch["bundle_id"]), {})
            order = next((o for o in order_bundle.get("legs", {}).values() if str(o.get("order_id")) == str(watch["order_id"])), None)
            if order is None:
                censored.add(key)
                continue
            details = _fee_details_from_order(order)
            exit_fee = float(watch["filled_size"]) * market_common.fee_per_share(vwap, details, taker=True)
            liquidation = float(watch["filled_size"]) * vwap - exit_fee
            entry_cost = float(watch["filled_size"]) * float(watch["fill_price"]) + float(watch.get("fee", 0.0))
            markout = liquidation - entry_cost
            writer.append(ledger.LedgerEvent(
                event_type="MARKOUT", strategy=STRATEGY, model_sha=model_sha,
                bundle_id=str(watch["bundle_id"]), order_id=str(watch["order_id"]), fill_id=fid,
                leg_id=str(watch["leg_id"]), market_id=str(watch["market_id"]), event_id=str(watch["event_id"]),
                token_id=str(watch["token_id"]), exchange_ts_ms=book.exchange_ts_ms,
                receive_ts_ms=book.received_ts_ms, book_snapshot_id=book.snapshot_hash,
                executable_liquidation_value=liquidation, markouts={key: markout},
                metadata={"model_family": STRATEGY, "horizon_seconds": horizon, "full_visible_bid_depth": True, "label_delay_ms": max(0, now_ms - due)},
            ))
            done.add(key)
        watch["done"] = sorted(done)
        watch["censored"] = sorted(censored)
        if len(done | censored) == len(MARKOUT_HORIZONS_SECONDS):
            watch["complete"] = True


def run_cycle(
    cfg: dict[str, Any],
    *,
    run_dir: Path,
    trade_tape: Path,
    model_sha: str,
    writer: ledger.CanonicalLedgerWriter,
    request_json: Callable[..., Any] = market_common.request_json,
    now_ms: int | None = None,
) -> dict[str, Any]:
    now = int(_now_ms() if now_ms is None else now_ms)
    state_path = run_dir / "maker_state.json"
    state = _load_state(state_path, model_sha)
    cohort = candidates_from_config(cfg)
    markets = discover_frozen_markets(
        str(cfg["gamma_url"]), cohort, market_limit=int(cfg["market_limit"]),
        min_liquidity=float(cfg["min_liquidity"]), request_json=request_json,
    )
    tokens = [token for market in markets.values() for token in (market.yes_token, market.no_token)]
    books = fetch_full_books(str(cfg["clob_url"]), tokens, request_json=request_json)
    new_trades = read_new_tape(trade_tape, state)

    active = [bundle for bundle in state["bundles"].values() if not bundle.get("final_recorded")]
    fills = 0
    for trade in new_trades:
        for bundle in active:
            fills += process_trade(writer, bundle, trade, model_sha=model_sha)
    advance_tape_watermark(state, new_trades)

    for bundle in active:
        if bundle.get("final_recorded"):
            continue
        target = float(bundle["target_shares"])
        y_fill = float(bundle["legs"]["YES"].get("filled_shares", 0.0))
        n_fill = float(bundle["legs"]["NO"].get("filled_shares", 0.0))
        if y_fill + 1e-12 >= target and n_fill + 1e-12 >= target:
            settle_bundle(writer, bundle, books, model_sha=model_sha, now_ms=now, cfg=cfg)
            continue
        expiry = int(bundle["created_ms"]) + int(cfg["ttl_seconds"]) * 1000
        if now >= expiry:
            request_cancel(writer, bundle, books, model_sha=model_sha, now_ms=now, cfg=cfg)
        if bundle.get("cancel_effective_ms") is not None and now >= int(bundle["cancel_effective_ms"]):
            settle_bundle(writer, bundle, books, model_sha=model_sha, now_ms=now, cfg=cfg)

    update_markouts(writer, state, books, model_sha=model_sha, now_ms=now, cfg=cfg)

    open_markets = {str(bundle["market_id"]) for bundle in state["bundles"].values() if not bundle.get("final_recorded")}
    recent_for_decision = [t for t in new_trades if t.received_ms <= now and t.event_ts_ms <= now]
    submitted = 0
    for market_id, candidate in cohort.items():
        if market_id in open_markets or now < candidate.prospective_not_before_ms:
            continue
        market = markets.get(market_id)
        if market is None:
            continue
        yes, no = books.get(market.yes_token), books.get(market.no_token)
        if yes is None or no is None:
            continue
        coherent = snapshots.validate_coherent_books(
            {yes.token_id: yes.causal(), no.token_id: no.causal()},
            [yes.token_id, no.token_id], now_ms=now,
            max_age_ms=int(cfg["max_book_age_ms"]),
            max_exchange_skew_ms=int(cfg["max_cross_leg_exchange_skew_ms"]),
            max_receive_skew_ms=int(cfg["max_cross_leg_receive_skew_ms"]),
        )
        if not coherent.ok or coherent.snapshot_set_id is None:
            continue
        yes_fee = market_common.resolve_fee_details(market.raw, str(cfg["clob_url"]), market.condition_id, market.yes_token)
        no_fee = market_common.resolve_fee_details(market.raw, str(cfg["clob_url"]), market.condition_id, market.no_token)
        quote = choose_quote(market, yes, no, yes_fee=yes_fee, no_fee=no_fee, recent_trades=recent_for_decision, cfg=cfg)
        if quote is None:
            continue
        submit_bundle(writer, state, market, candidate, quote, yes, no, yes_fee, no_fee, model_sha=model_sha, cfg=cfg, decision_ms=now, snapshot_id=coherent.snapshot_set_id)
        submitted += 1

    _atomic_json(state_path, state)
    report = {
        "schema": "polymarket_v7_complete_set_maker_cycle_v1", "model_sha": model_sha,
        "paper_only": True, "authenticated_execution": False,
        "frozen_candidates": len(cohort), "markets_resolved": len(markets),
        "new_tape_trades": len(new_trades), "fills_written": fills, "bundles_submitted": submitted,
        "open_bundles": sum(1 for b in state["bundles"].values() if not b.get("final_recorded")),
        "final_bundles": sum(1 for b in state["bundles"].values() if b.get("final_recorded")),
    }
    _atomic_json(run_dir / "maker_cycle.json", report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=2.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = args.ledger or ledger.canonical_ledger_path(args.run_dir)
    with ledger.CanonicalLedgerWriter(ledger_path, writer_id="v7-complete-set-maker", model_sha=args.model_sha) as writer:
        while True:
            report = run_cycle(cfg, run_dir=args.run_dir, trade_tape=args.trade_tape, model_sha=args.model_sha, writer=writer)
            print(json.dumps(report, sort_keys=True), flush=True)
            if not args.loop:
                break
            time.sleep(max(0.1, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
