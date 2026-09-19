#!/usr/bin/env python3
"""Full-ladder PAPER capacity replay for LEAD_LAG_TAKER_V1.

Only uses ask ladders retained at the actual arrival timestamp.  Historical
trades without ``metadata.capacity_book`` are excluded rather than imputed.
No network calls and no order submission are possible from this module.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_external_fair_research import fee_per_share
from v7_lead_lag_capacity_replay import (
    MODEL_FAMILY,
    STRATEGY,
    finite,
    iter_jsonl,
    peak_concurrent_cash,
    summarize_distribution,
)

DEFAULT_PRICE_IMPACT_CAPS = (0.0, 0.01, 0.02, 0.05, 0.10)

@dataclass(frozen=True)
class LadderTrade:
    order_id: str
    market_id: str
    opened_ms: int
    final_ms: int
    won: bool
    book_snapshot_id: str
    ask_levels: tuple[tuple[float, float], ...]
    fee_schedule: dict[str, Any]

    @property
    def best_ask(self) -> float:
        return self.ask_levels[0][0]

    @property
    def total_shares(self) -> float:
        return sum(size for _, size in self.ask_levels)

    @property
    def total_notional_usd(self) -> float:
        return sum(price * size for price, size in self.ask_levels)


def _is_lead_lag_order(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return (
        row.get("event_type") == "ORDER_SUBMITTED"
        and row.get("strategy") == STRATEGY
        and metadata.get("model_family") == MODEL_FAMILY
        and metadata.get("paper_forward_test") is True
    )

def _parse_levels(value: Any) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list):
        raise ValueError("capacity_book.ask_levels must be a list")
    levels: list[tuple[float, float]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("capacity_book ask level must be an object")
        price, size = finite(raw.get("price")), finite(raw.get("size"))
        if price is None or size is None or not 0 < price < 1 or size <= 0:
            raise ValueError("invalid retained ask level")
        levels.append((float(price), float(size)))
    if not levels:
        raise ValueError("retained ask ladder is empty")
    levels.sort()
    return tuple(levels)


def collect_ladder_trades(path: Path) -> list[LadderTrade]:
    finals: dict[str, dict[str, Any]] = {}
    orders: list[dict[str, Any]] = []
    for row in iter_jsonl(path):
        order_id = str(row.get("order_id") or "")
        if row.get("event_type") == "FINAL" and order_id:
            finals[order_id] = row
        elif _is_lead_lag_order(row):
            orders.append(row)

    trades: list[LadderTrade] = []
    for row in orders:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        capacity = metadata.get("capacity_book") if isinstance(metadata.get("capacity_book"), dict) else None
        if capacity is None:
            continue
        if capacity.get("schema") != "polymarket_v7_lead_lag_capacity_book_v1":
            raise ValueError("unknown lead-lag capacity-book schema")
        if capacity.get("observed_ladder_complete_as_received") is not True:
            raise ValueError("capacity-book is not explicitly complete as received")
        if row.get("paper_only") is not True or row.get("authenticated_execution") is not False:
            raise ValueError("full-ladder replay refuses non-PAPER evidence")
        if row.get("real_order_submission") not in (None, False):
            raise ValueError("full-ladder replay refuses real-order evidence")
        order_id = str(row.get("order_id") or "")
        final = finals.get(order_id)
        if final is None:
            continue
        final_meta = final.get("metadata") if isinstance(final.get("metadata"), dict) else {}
        won = final_meta.get("won")
        if not isinstance(won, bool):
            raise ValueError(f"missing settlement outcome for {order_id}")
        levels = _parse_levels(capacity.get("ask_levels"))
        schedule = capacity.get("fee_schedule")
        if not isinstance(schedule, dict):
            raise ValueError("capacity_book fee schedule missing")
        for price, _ in levels:
            if not math.isfinite(fee_per_share(price, schedule)):
                raise ValueError("capacity_book contains unusable fee schedule")
        snapshot = str(capacity.get("book_snapshot_id") or "")
        if snapshot and row.get("book_snapshot_id") and snapshot != str(row["book_snapshot_id"]):
            raise ValueError("capacity-book snapshot identity mismatch")
        opened = int(finite(row.get("recorded_ts_ms")) or 0)
        closed = int(finite(final.get("recorded_ts_ms")) or 0)
        if not opened or not closed or closed < opened:
            raise ValueError("invalid ladder trade timestamps")
        trades.append(LadderTrade(
            order_id=order_id,
            market_id=str(row.get("market_id") or ""),
            opened_ms=opened,
            final_ms=closed,
            won=won,
            book_snapshot_id=snapshot,
            ask_levels=levels,
            fee_schedule=schedule,
        ))
    return sorted(trades, key=lambda trade: (trade.opened_ms, trade.market_id))

def _allowed_levels(trade: LadderTrade, price_impact_cap: float) -> tuple[tuple[float, float], ...]:
    ceiling = min(1.0, trade.best_ask + price_impact_cap + 1e-12)
    return tuple((price, size) for price, size in trade.ask_levels if price <= ceiling)


def execute_ladder(
    trade: LadderTrade, *, target: float, dimension: str,
    price_impact_cap: float, strict_full_fill: bool,
) -> dict[str, Any]:
    levels = _allowed_levels(trade, price_impact_cap)
    available_shares = sum(size for _, size in levels)
    available_notional = sum(price * size for price, size in levels)
    if dimension == "shares":
        requested_shares = target
        full = available_shares + 1e-12 >= requested_shares
    elif dimension == "notional_usd":
        requested_shares = math.inf
        full = available_notional + 1e-12 >= target
    else:
        raise ValueError(f"unsupported ladder dimension {dimension}")
    if strict_full_fill and not full:
        return {"full": False, "filled_shares": 0.0, "notional_usd": 0.0, "fee_usd": 0.0,
                "entry_cash_usd": 0.0, "vwap": None, "max_price": None, "pnl_usd": 0.0}

    remaining = target
    filled = notional = fee = 0.0
    max_price: float | None = None
    for price, size in levels:
        if dimension == "shares":
            take = min(size, remaining)
            remaining -= take
        else:
            take = min(size, remaining / price)
            remaining -= take * price
        if take <= 0:
            continue
        filled += take
        notional += take * price
        fee += take * fee_per_share(price, trade.fee_schedule)
        max_price = price
        if remaining <= 1e-12:
            break
    payout = filled if trade.won else 0.0
    return {
        "full": full,
        "filled_shares": filled,
        "notional_usd": notional,
        "fee_usd": fee,
        "entry_cash_usd": notional + fee,
        "vwap": notional / filled if filled else None,
        "max_price": max_price,
        "pnl_usd": payout - notional - fee,
    }

def ladder_scenario(
    trades: list[LadderTrade], *, target: float, dimension: str,
    price_impact_cap: float, strict_full_fill: bool = True,
) -> dict[str, Any]:
    executions = [
        (trade, execute_ladder(
            trade,
            target=target,
            dimension=dimension,
            price_impact_cap=price_impact_cap,
            strict_full_fill=strict_full_fill,
        ))
        for trade in trades
    ]
    filled = [(trade, result) for trade, result in executions if result["filled_shares"] > 0]
    full = sum(bool(result["full"]) for _, result in executions)
    peak_rows = [
        (trade.opened_ms, trade.final_ms, float(result["entry_cash_usd"]))
        for trade, result in filled
    ]
    cash = [float(result["entry_cash_usd"]) for _, result in filled]
    pnls = [float(result["pnl_usd"]) for _, result in filled]
    vwaps = [float(result["vwap"]) for _, result in filled if result["vwap"] is not None]
    return {
        "dimension": dimension,
        "target": target,
        "price_impact_cap": price_impact_cap,
        "evidence_markets": len(trades),
        "executed_markets": len(filled),
        "full_fill_markets": full,
        "full_fill_rate": full / len(trades) if trades else None,
        "total_filled_shares": sum(float(result["filled_shares"]) for _, result in filled),
        "total_entry_cash_usd": sum(cash),
        "peak_concurrent_entry_cash_usd": peak_concurrent_cash(peak_rows),
        "realized_pnl_usd": sum(pnls),
        "mean_realized_pnl_usd": statistics.mean(pnls) if pnls else None,
        "mean_vwap": statistics.mean(vwaps) if vwaps else None,
        "max_vwap": max(vwaps) if vwaps else None,
        "max_price_paid": max(
            (float(result["max_price"]) for _, result in filled if result["max_price"] is not None),
            default=None,
        ),
    }


def _capacity_at_cap(trades: list[LadderTrade], price_impact_cap: float) -> dict[str, Any]:
    share_values: list[float] = []
    notional_values: list[float] = []
    for trade in trades:
        levels = _allowed_levels(trade, price_impact_cap)
        share_values.append(sum(size for _, size in levels))
        notional_values.append(sum(price * size for price, size in levels))
    return {
        "shares": summarize_distribution(share_values),
        "notional_usd": summarize_distribution(notional_values),
    }

def build_ladder_report(
    ledger: Path, *, share_grid: tuple[float, ...], notional_grid: tuple[float, ...],
    price_impact_caps: tuple[float, ...] = DEFAULT_PRICE_IMPACT_CAPS,
) -> dict[str, Any]:
    trades = collect_ladder_trades(ledger)
    if not trades:
        return {
            "available": False,
            "evidence_markets": 0,
            "price_impact_caps": list(price_impact_caps),
            "reason": "NO_RETAINED_FULL_ASK_LADDERS_YET",
            "interpretation": (
                "Future PAPER entries retain the complete arrival ask ladder. "
                "Until such settled evidence exists, multi-level VWAP/slippage is not estimated."
            ),
        }
    by_cap: dict[str, Any] = {}
    for cap in price_impact_caps:
        label = f"{int(round(cap * 100))}c_from_best_ask"
        by_cap[label] = {
            "price_impact_cap": cap,
            "capacity": _capacity_at_cap(trades, cap),
            "strict_fixed_share_grid": [
                ladder_scenario(trades, target=x, dimension="shares", price_impact_cap=cap)
                for x in share_grid
            ],
            "strict_fixed_notional_grid": [
                ladder_scenario(trades, target=x, dimension="notional_usd", price_impact_cap=cap)
                for x in notional_grid
            ],
        }
    return {
        "available": True,
        "evidence_markets": len(trades),
        "price_impact_caps": list(price_impact_caps),
        "by_price_impact_cap": by_cap,
        "interpretation": (
            "Exact PAPER counterfactual using only retained arrival ask levels. "
            "Strict scenarios require the entire target to be visible inside the stated price-impact cap."
        ),
    }

def write_ladder_scenario_csv(path: Path, report: dict[str, Any]) -> None:
    import csv
    import json

    rows: list[dict[str, Any]] = []
    for label, section in (report.get("by_price_impact_cap") or {}).items():
        for scenario_name in ("strict_fixed_share_grid", "strict_fixed_notional_grid"):
            for row in section.get(scenario_name) or []:
                rows.append({"price_impact_label": label, "scenario": scenario_name, **row})
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row}) if rows else [
        "price_impact_label", "scenario", "dimension", "target", "price_impact_cap",
        "evidence_markets", "full_fill_markets", "full_fill_rate", "realized_pnl_usd",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
                for key, value in row.items()
            })
