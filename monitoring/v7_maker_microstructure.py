#!/usr/bin/env python3
"""Read-only microstructure lab for the V7 PAPER maker canonical ledger.

The lab reconstructs the causal chain ORDER_SUBMITTED -> FILL -> MARKOUT and
replays the PAPER inventory accounting to attribute realized trading PnL to the
microstructure state in which the contributing maker quote was submitted.

It is intentionally evidence-only: it never changes quoting, capital, risk,
orders, model promotion, or the canonical ledger.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STRATEGY = "MICRO_MAKER_PRO"
HORIZONS = ("1s", "10s", "45s", "60s", "300s")
DIMENSIONS = ("spread", "imbalance", "ofi", "toxicity", "queue", "inventory", "latency", "reward")
_EPS = 1e-12
_CACHE_LOCK = threading.Lock()
_CACHE_KEY: tuple[Any, ...] | None = None
_CACHE_VALUE: dict[str, Any] | None = None


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _stat_key(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return -1, -1


def _reward_map(path: Path) -> dict[str, bool]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = root.get("markets") if isinstance(root, dict) and isinstance(root.get("markets"), list) else []
    out: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        market = str(row.get("market_id") or "")
        if not market:
            continue
        intensity = _finite(row.get("reward_intensity"), 0.0) or 0.0
        out[market] = intensity > 0.0
    return out


def _action(row: dict[str, Any]) -> tuple[str, str]:
    exact = str(row.get("intended_action") or _metadata(row).get("action") or "UNKNOWN").upper()
    if exact == "IMPROVE1":
        return "IMPROVE", exact
    if exact in {"FADE1", "FADE2"}:
        return "FADE", exact
    if exact == "JOIN":
        return "JOIN", exact
    if exact == "ONE_SIDED":
        side = str(row.get("side") or "").upper()
        px = _finite(row.get("limit_price"))
        bid = _finite(row.get("bid"))
        ask = _finite(row.get("ask"))
        family = "ONE_SIDED"
        if px is not None and bid is not None and ask is not None:
            if side == "BUY":
                family = "JOIN" if abs(px - bid) <= _EPS else "IMPROVE" if px > bid else "FADE"
            elif side == "SELL":
                family = "JOIN" if abs(px - ask) <= _EPS else "IMPROVE" if px < ask else "FADE"
        return family, f"ONE_SIDED/{family}"
    return exact if exact else "UNKNOWN", exact if exact else "UNKNOWN"


def _spread_bucket(value: float | None) -> str:
    if value is None or value < 0.0:
        return "UNKNOWN"
    if value <= 0.005 + _EPS:
        return "<=0.5c"
    if value <= 0.01 + _EPS:
        return "0.5-1c"
    if value <= 0.02 + _EPS:
        return "1-2c"
    return ">2c"


def _signed_bucket(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= -0.25:
        return "NEGATIVE"
    if value >= 0.25:
        return "POSITIVE"
    return "NEUTRAL"


def _toxicity_bucket(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value < 0.25:
        return "LOW"
    if value < 0.50:
        return "MEDIUM"
    if value < 0.75:
        return "HIGH"
    return "TOXIC"


def _queue_bucket(queue: float | None, size: float | None) -> str:
    if queue is None or queue < 0.0 or size is None or size <= 0.0:
        return "UNKNOWN"
    ratio = queue / size
    if ratio <= _EPS:
        return "FRONT"
    if ratio <= 1.0:
        return "<=1x"
    if ratio <= 5.0:
        return "1-5x"
    if ratio <= 20.0:
        return "5-20x"
    return ">20x"


def _latency_bucket(ms: float | None) -> str:
    if ms is None or ms < 0.0:
        return "UNKNOWN"
    if ms < 1.0:
        return "<1ms"
    if ms < 2.0:
        return "1-2ms"
    if ms < 5.0:
        return "2-5ms"
    return ">=5ms"


def _inventory_bucket(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value > 1e-9:
        return "LONG"
    if value < -1e-9:
        return "SHORT"
    return "FLAT"


def _reward_bucket(value: bool | None) -> str:
    if value is True:
        return "REWARDED"
    if value is False:
        return "UNREWARDED"
    return "UNKNOWN"


def _outcome(row: dict[str, Any]) -> str:
    value = str(_metadata(row).get("outcome") or "").upper()
    return value if value in {"YES", "NO"} else "UNKNOWN"


@dataclass
class Agg:
    orders: int = 0
    filled_orders: int = 0
    fills: int = 0
    filled_shares: float = 0.0
    realized_pnl: float = 0.0
    markout_pnl: dict[str, float] = field(default_factory=lambda: {h: 0.0 for h in HORIZONS})
    markout_shares: dict[str, float] = field(default_factory=lambda: {h: 0.0 for h in HORIZONS})
    markout_count: dict[str, int] = field(default_factory=lambda: {h: 0 for h in HORIZONS})

    def as_dict(self) -> dict[str, Any]:
        return {
            "orders": self.orders,
            "filled_orders": self.filled_orders,
            "fills": self.fills,
            "filled_shares": self.filled_shares,
            "realized_pnl": self.realized_pnl,
            "markout_pnl": dict(self.markout_pnl),
            "markout_shares": dict(self.markout_shares),
            "markout_count": dict(self.markout_count),
        }


@dataclass
class Cohort:
    context: dict[str, str]
    quantity: float = 0.0
    cost: float = 0.0


@dataclass
class OutcomeInventory:
    quantity: float = 0.0
    cost: float = 0.0
    cohorts: dict[tuple[str, ...], Cohort] = field(default_factory=dict)

    def add(self, quantity: float, price: float, context: dict[str, str]) -> None:
        self.quantity += quantity
        self.cost += quantity * price
        key = _context_key(context)
        cohort = self.cohorts.get(key)
        if cohort is None:
            cohort = Cohort(dict(context))
            self.cohorts[key] = cohort
        cohort.quantity += quantity
        cohort.cost += quantity * price

    def reduce_pro_rata(self, quantity: float) -> float:
        if quantity <= 0.0 or self.quantity <= _EPS:
            return 0.0
        used = min(quantity, self.quantity)
        fraction = min(1.0, used / self.quantity)
        self.quantity -= used
        self.cost = max(0.0, self.cost * (1.0 - fraction))
        stale: list[tuple[str, ...]] = []
        for key, cohort in self.cohorts.items():
            cohort.quantity *= 1.0 - fraction
            cohort.cost *= 1.0 - fraction
            if cohort.quantity <= _EPS:
                stale.append(key)
        for key in stale:
            self.cohorts.pop(key, None)
        return used


@dataclass
class MarketInventory:
    yes: OutcomeInventory = field(default_factory=OutcomeInventory)
    no: OutcomeInventory = field(default_factory=OutcomeInventory)

    def common_residual(self) -> float:
        return self.yes.quantity - self.no.quantity

    def lane_residual(self, outcome: str) -> float | None:
        residual = self.common_residual()
        if outcome == "YES":
            return residual
        if outcome == "NO":
            return -residual
        return None


def _context_key(context: dict[str, str]) -> tuple[str, ...]:
    return tuple(context.get(key, "UNKNOWN") for key in (
        "action", "variant", "market", "spread", "imbalance", "ofi", "toxicity",
        "queue", "inventory", "latency", "reward",
    ))


def _blank_result() -> dict[str, Any]:
    return {
        "present": False,
        "rows": 0,
        "maker_rows": 0,
        "orders": 0,
        "filled_orders": 0,
        "fills": 0,
        "markouts": {h: 0 for h in HORIZONS},
        "realized_pnl": 0.0,
        "attributed_realized_pnl": 0.0,
        "segments": [],
        "conditionals": [],
        "markets": [],
        "quality": {
            "linked_fills": 0,
            "unlinked_fills": 0,
            "linked_markouts": 0,
            "unlinked_markouts": 0,
            "ofi_exact_orders": 0,
            "ofi_proxy_orders": 0,
            "reward_known_orders": 0,
            "unattributed_sell_fills": 0,
            "unattributed_merge_pnl": 0.0,
            "ofi_source": "exact_metadata_or_quote_to_quote_l5_proxy",
            "reward_source": "event_metadata_or_current_reward_selection",
            "merge_pnl_attribution": "symmetric_outcome_split_then_pro_rata_inventory_cohorts",
        },
    }


def _segment_keys(context: dict[str, str]) -> list[tuple[str, str, str, str]]:
    action = context["action"]
    variant = context["variant"]
    keys = [("ALL", "ALL", "all", "all"), (action, variant, "all", "all")]
    for dimension in DIMENSIONS:
        keys.append((action, variant, dimension, context.get(dimension, "UNKNOWN")))
    return keys


def _touch_order(segments: dict[tuple[str, str, str, str], Agg], context: dict[str, str]) -> None:
    for key in _segment_keys(context):
        segments.setdefault(key, Agg()).orders += 1


def _touch_filled_order(segments: dict[tuple[str, str, str, str], Agg], context: dict[str, str]) -> None:
    for key in _segment_keys(context):
        segments.setdefault(key, Agg()).filled_orders += 1


def _touch_fill(segments: dict[tuple[str, str, str, str], Agg], context: dict[str, str], shares: float) -> None:
    for key in _segment_keys(context):
        agg = segments.setdefault(key, Agg())
        agg.fills += 1
        agg.filled_shares += shares


def _touch_pnl(segments: dict[tuple[str, str, str, str], Agg], context: dict[str, str], pnl: float) -> None:
    if not math.isfinite(pnl) or abs(pnl) <= 0.0:
        return
    for key in _segment_keys(context):
        segments.setdefault(key, Agg()).realized_pnl += pnl


def _touch_markout(
    segments: dict[tuple[str, str, str, str], Agg],
    context: dict[str, str], horizon: str, value: float, shares: float,
) -> None:
    for key in _segment_keys(context):
        agg = segments.setdefault(key, Agg())
        agg.markout_pnl[horizon] += value * shares
        agg.markout_shares[horizon] += shares
        agg.markout_count[horizon] += 1


def _conditional_key(context: dict[str, str]) -> tuple[str, str, str]:
    return context["action"], context["toxicity"], context["queue"]


def _market_key(context: dict[str, str]) -> tuple[str, str]:
    return context["market"], context["action"]


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return rows
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _candidate_context(
    row: dict[str, Any],
    inventory: MarketInventory,
    reward_map: dict[str, bool],
    previous_book: dict[str, tuple[float, float]],
    quality: dict[str, Any],
) -> dict[str, str]:
    meta = _metadata(row)
    market = str(row.get("market_id") or "UNKNOWN")
    token = str(row.get("token_id") or "")
    action, variant = _action(row)
    bid = _finite(row.get("bid"))
    ask = _finite(row.get("ask"))
    bid_depth = _finite(row.get("bid_depth"))
    ask_depth = _finite(row.get("ask_depth"))
    spread = None if bid is None or ask is None else max(0.0, ask - bid)
    imbalance = None
    if bid_depth is not None and ask_depth is not None and bid_depth + ask_depth > _EPS:
        imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)

    ofi = _finite(meta.get("ofi"))
    if ofi is not None:
        quality["ofi_exact_orders"] += 1
    else:
        previous = previous_book.get(token)
        if previous is not None and bid_depth is not None and ask_depth is not None:
            delta_bid = bid_depth - previous[0]
            delta_ask = ask_depth - previous[1]
            denom = abs(delta_bid) + abs(delta_ask)
            ofi = (delta_bid - delta_ask) / denom if denom > _EPS else 0.0
            quality["ofi_proxy_orders"] += 1
    if token and bid_depth is not None and ask_depth is not None:
        previous_book[token] = (bid_depth, ask_depth)

    toxicity = _finite(meta.get("toxicity"))
    receive = _finite(row.get("receive_ts_ms"))
    decision = _finite(row.get("decision_ts_ms"))
    latency_ms = None if receive is None or decision is None else max(0.0, decision - receive)
    outcome = _outcome(row)
    inventory_value = _finite(meta.get("inventory_fraction"))
    if inventory_value is None:
        inventory_value = inventory.lane_residual(outcome)

    rewarded_raw = meta.get("rewarded_market")
    rewarded: bool | None
    if isinstance(rewarded_raw, bool):
        rewarded = rewarded_raw
    elif market in reward_map:
        rewarded = reward_map[market]
    else:
        rewarded = None
    if rewarded is not None:
        quality["reward_known_orders"] += 1

    return {
        "action": action,
        "variant": variant,
        "market": market,
        "spread": _spread_bucket(spread),
        "imbalance": _signed_bucket(imbalance),
        "ofi": _signed_bucket(ofi),
        "toxicity": _toxicity_bucket(toxicity),
        "queue": "UNKNOWN",
        "inventory": _inventory_bucket(inventory_value),
        "latency": _latency_bucket(latency_ms),
        "reward": _reward_bucket(rewarded),
        "outcome": outcome,
    }


def _add_conditional_order(target: dict[tuple[str, str, str], Agg], context: dict[str, str]) -> None:
    target.setdefault(_conditional_key(context), Agg()).orders += 1


def _add_conditional_filled(target: dict[tuple[str, str, str], Agg], context: dict[str, str]) -> None:
    target.setdefault(_conditional_key(context), Agg()).filled_orders += 1


def _add_conditional_fill(target: dict[tuple[str, str, str], Agg], context: dict[str, str], shares: float) -> None:
    agg = target.setdefault(_conditional_key(context), Agg())
    agg.fills += 1
    agg.filled_shares += shares


def _add_conditional_pnl(target: dict[tuple[str, str, str], Agg], context: dict[str, str], pnl: float) -> None:
    target.setdefault(_conditional_key(context), Agg()).realized_pnl += pnl


def _add_conditional_markout(target: dict[tuple[str, str, str], Agg], context: dict[str, str], horizon: str, value: float, shares: float) -> None:
    agg = target.setdefault(_conditional_key(context), Agg())
    agg.markout_pnl[horizon] += value * shares
    agg.markout_shares[horizon] += shares
    agg.markout_count[horizon] += 1


def _add_market_order(target: dict[tuple[str, str], Agg], context: dict[str, str]) -> None:
    target.setdefault(_market_key(context), Agg()).orders += 1


def _add_market_filled(target: dict[tuple[str, str], Agg], context: dict[str, str]) -> None:
    target.setdefault(_market_key(context), Agg()).filled_orders += 1


def _add_market_fill(target: dict[tuple[str, str], Agg], context: dict[str, str], shares: float) -> None:
    agg = target.setdefault(_market_key(context), Agg())
    agg.fills += 1
    agg.filled_shares += shares


def _add_market_pnl(target: dict[tuple[str, str], Agg], market: str, action: str, pnl: float) -> None:
    target.setdefault((market, action), Agg()).realized_pnl += pnl


def _add_market_markout(target: dict[tuple[str, str], Agg], context: dict[str, str], horizon: str, value: float, shares: float) -> None:
    agg = target.setdefault(_market_key(context), Agg())
    agg.markout_pnl[horizon] += value * shares
    agg.markout_shares[horizon] += shares
    agg.markout_count[horizon] += 1


def summarize_maker_microstructure(
    ledger_path: Path,
    reward_selection_path: Path | None = None,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    global _CACHE_KEY, _CACHE_VALUE
    ledger_path = Path(ledger_path)
    reward_path = Path(reward_selection_path) if reward_selection_path is not None else Path("")
    cache_key = (_stat_key(ledger_path), _stat_key(reward_path) if reward_selection_path is not None else (-1, -1))
    if use_cache:
        with _CACHE_LOCK:
            if _CACHE_KEY == cache_key and _CACHE_VALUE is not None:
                return _CACHE_VALUE

    result = _blank_result()
    if not ledger_path.is_file():
        return result
    result["present"] = True
    rewards = _reward_map(reward_path) if reward_selection_path is not None else {}
    rows = _read_rows(ledger_path)
    result["rows"] = len(rows)

    segments: dict[tuple[str, str, str, str], Agg] = {}
    conditionals: dict[tuple[str, str, str], Agg] = {}
    markets: dict[tuple[str, str], Agg] = {}
    inventories: dict[str, MarketInventory] = {}
    previous_book: dict[str, tuple[float, float]] = {}
    candidate_contexts: dict[str, dict[str, str]] = {}
    order_contexts: dict[str, dict[str, str]] = {}
    fill_contexts: dict[str, tuple[dict[str, str], float]] = {}
    filled_orders: set[str] = set()
    quality = result["quality"]

    total_realized = 0.0
    attributed_realized = 0.0

    for row in rows:
        if str(row.get("strategy") or "") != STRATEGY:
            continue
        if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
            continue
        result["maker_rows"] += 1
        event_type = str(row.get("event_type") or "")
        market = str(row.get("market_id") or "UNKNOWN")
        inventory = inventories.setdefault(market, MarketInventory())

        if event_type == "CANDIDATE":
            context = _candidate_context(row, inventory, rewards, previous_book, quality)
            candidate = str(row.get("candidate_id") or "")
            if candidate:
                candidate_contexts[candidate] = context
            continue

        if event_type == "ORDER_SUBMITTED":
            candidate = str(row.get("candidate_id") or "")
            context = dict(candidate_contexts.get(candidate) or _candidate_context(row, inventory, rewards, previous_book, quality))
            context["queue"] = _queue_bucket(_finite(row.get("queue_ahead")), _finite(row.get("intended_size")))
            action, variant = _action(row)
            context["action"] = action
            context["variant"] = variant
            context["market"] = market
            if context.get("outcome") == "UNKNOWN":
                context["outcome"] = _outcome(row)
            order_id = str(row.get("order_id") or "")
            if not order_id:
                continue
            order_contexts[order_id] = context
            _touch_order(segments, context)
            _add_conditional_order(conditionals, context)
            _add_market_order(markets, context)
            result["orders"] += 1
            continue

        if event_type == "FILL":
            result["fills"] += 1
            order_id = str(row.get("order_id") or "")
            context = order_contexts.get(order_id)
            if context is None:
                quality["unlinked_fills"] += 1
                continue
            quality["linked_fills"] += 1
            if order_id not in filled_orders:
                filled_orders.add(order_id)
                _touch_filled_order(segments, context)
                _add_conditional_filled(conditionals, context)
                _add_market_filled(markets, context)
            shares = max(0.0, _finite(row.get("filled_size"), 0.0) or 0.0)
            price = _finite(row.get("fill_price"))
            side = str(row.get("side") or "").upper()
            outcome = context.get("outcome", "UNKNOWN")
            _touch_fill(segments, context, shares)
            _add_conditional_fill(conditionals, context, shares)
            _add_market_fill(markets, context, shares)
            fill_id = str(row.get("fill_id") or "")
            if fill_id:
                fill_contexts[fill_id] = (context, shares)

            if shares <= 0.0 or price is None or outcome not in {"YES", "NO"}:
                continue
            bucket = inventory.yes if outcome == "YES" else inventory.no
            if side == "BUY":
                bucket.add(shares, price, context)
            elif side == "SELL":
                if bucket.quantity + 1e-9 < shares or bucket.quantity <= _EPS:
                    quality["unattributed_sell_fills"] += 1
                    continue
                avg_cost = bucket.cost / bucket.quantity
                pnl = shares * (price - avg_cost)
                bucket.reduce_pro_rata(shares)
                total_realized += pnl
                attributed_realized += pnl
                _touch_pnl(segments, context, pnl)
                _add_conditional_pnl(conditionals, context, pnl)
                _add_market_pnl(markets, market, context["action"], pnl)
            continue

        if event_type == "MARKOUT":
            markouts = row.get("markouts") if isinstance(row.get("markouts"), dict) else {}
            fill_id = str(row.get("fill_id") or "")
            linked = fill_contexts.get(fill_id)
            for horizon in HORIZONS:
                value = _finite(markouts.get(horizon))
                if value is None:
                    continue
                result["markouts"][horizon] += 1
                if linked is None:
                    quality["unlinked_markouts"] += 1
                    continue
                quality["linked_markouts"] += 1
                context, shares = linked
                _touch_markout(segments, context, horizon, value, shares)
                _add_conditional_markout(conditionals, context, horizon, value, shares)
                _add_market_markout(markets, context, horizon, value, shares)
            continue

        if event_type == "FINAL":
            pnl = _finite(row.get("final_pnl"), 0.0) or 0.0
            meta = _metadata(row)
            merged = max(0.0, _finite(meta.get("merged_shares"), 0.0) or 0.0)
            total_realized += pnl
            if merged <= 0.0 or inventory.yes.quantity + 1e-9 < merged or inventory.no.quantity + 1e-9 < merged:
                quality["unattributed_merge_pnl"] += pnl
                continue

            contributions: list[tuple[dict[str, str], float]] = []
            for bucket in (inventory.yes, inventory.no):
                total_qty = bucket.quantity
                if total_qty <= _EPS:
                    continue
                for cohort in bucket.cohorts.values():
                    weight = 0.5 * cohort.quantity / total_qty
                    if weight > 0.0:
                        contributions.append((cohort.context, weight))
            attributed = 0.0
            for context, weight in contributions:
                share_pnl = pnl * weight
                attributed += share_pnl
                _touch_pnl(segments, context, share_pnl)
                _add_conditional_pnl(conditionals, context, share_pnl)
                _add_market_pnl(markets, market, context["action"], share_pnl)
            inventory.yes.reduce_pro_rata(merged)
            inventory.no.reduce_pro_rata(merged)
            attributed_realized += attributed
            quality["unattributed_merge_pnl"] += pnl - attributed

    result["filled_orders"] = len(filled_orders)
    result["realized_pnl"] = total_realized
    result["attributed_realized_pnl"] = attributed_realized
    result["segments"] = [
        {"action": action, "variant": variant, "dimension": dimension, "bucket": bucket, **agg.as_dict()}
        for (action, variant, dimension, bucket), agg in sorted(segments.items())
    ]
    result["conditionals"] = [
        {"action": action, "toxicity": toxicity, "queue": queue, **agg.as_dict()}
        for (action, toxicity, queue), agg in sorted(conditionals.items())
    ]
    result["markets"] = [
        {"market": market, "action": action, **agg.as_dict()}
        for (market, action), agg in sorted(markets.items())
    ]

    if use_cache:
        with _CACHE_LOCK:
            _CACHE_KEY = cache_key
            _CACHE_VALUE = result
    return result
