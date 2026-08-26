#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import threading
import time
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

from v7_market_common import FeeDetails, finite, parse_array, request_json, resolve_fee_details

FEE_QUANTUM = Decimal("0.00001")

CANDIDATE_FIELDS = [
    "timestamp",
    "bundle_id",
    "strategy",
    "action",
    "expected_edge",
    "event_id",
    "legs",
    "shares",
    "raw_edge",
    "net_edge",
    "capital_used",
    "total_fees",
    "slippage_cost",
    "total_execution_cost",
    "max_leg_age_ms",
    "cross_leg_skew_ms",
    "max_exchange_snapshot_age_ms",
    "exchange_snapshot_skew_ms",
    "fee_sources",
    "executable",
    "reason",
]
FILL_FIELDS = [
    "timestamp",
    "bundle_id",
    "strategy",
    "event_id",
    "action",
    "token",
    "leg_index",
    "leg_count",
    "shares",
    "price",
    "capital_used",
    "fee",
    "total_fees",
    "slippage_cost",
    "total_execution_cost",
    "net_edge",
    "net_pnl",
    "detail",
]
LEG_FIELDS = FILL_FIELDS


@dataclass(frozen=True)
class BookFill:
    requested_shares: float
    filled_shares: float
    raw_cash: float
    stressed_cash: float
    fee: float
    raw_vwap: float
    stressed_vwap: float
    all_in_unit_price: float
    slippage_cost: float
    complete: bool


@dataclass(frozen=True)
class BundlePlan:
    capital_used: float
    raw_cash: float
    total_fees: float
    slippage_cost: float
    fills: tuple[dict[str, Any], ...]


def normalize_timestamp_ms(value: Any) -> int:
    ts = finite(value, 0.0)
    if ts <= 0.0:
        return 0
    if ts >= 1e14:
        ts /= 1000.0
    elif ts < 1e11:
        ts *= 1000.0
    return int(ts)


def _book_field(book: Any, name: str, default: Any = None) -> Any:
    if isinstance(book, dict):
        return book.get(name, default)
    return getattr(book, name, default)


def local_book_freshness(
    live: dict[str, Any],
    tokens: Sequence[str],
    *,
    now_ms: int,
    max_leg_age_ms: int,
    max_cross_leg_skew_ms: int,
) -> tuple[bool, str, int, int]:
    stamps: list[int] = []
    for token in tokens:
        book = live.get(token)
        if book is None:
            return False, "missing_book", 0, 0
        received_ms = int(finite(_book_field(book, "received_ms"), 0.0))
        if received_ms <= 0:
            return False, "missing_receive_timestamp", 0, 0
        stamps.append(received_ms)
    age = max(0, now_ms - min(stamps)) if stamps else 0
    skew = max(stamps) - min(stamps) if stamps else 0
    if age > max_leg_age_ms:
        return False, "max_leg_age", age, skew
    if skew > max_cross_leg_skew_ms:
        return False, "cross_leg_skew", age, skew
    return True, "ok", age, skew


def exchange_book_freshness(
    live: dict[str, Any],
    tokens: Sequence[str],
    *,
    now_ms: int,
    max_snapshot_age_ms: int,
    max_snapshot_skew_ms: int,
) -> tuple[bool, str, int, int]:
    stamps: list[int] = []
    for token in tokens:
        book = live.get(token)
        if book is None:
            return False, "missing_book", 0, 0
        exchange_ms = int(finite(_book_field(book, "exchange_ts_ms"), 0.0))
        if exchange_ms <= 0:
            return False, "missing_exchange_timestamp", 0, 0
        stamps.append(exchange_ms)
    age = max(0, now_ms - min(stamps)) if stamps else 0
    skew = max(stamps) - min(stamps) if stamps else 0
    if age > max_snapshot_age_ms:
        return False, "max_exchange_snapshot_age", age, skew
    if skew > max_snapshot_skew_ms:
        return False, "exchange_snapshot_skew", age, skew
    return True, "ok", age, skew


def _clean_levels(rows: Any, *, buy: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            price = finite(row.get("price"))
            size = finite(row.get("size"), 0.0)
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price = finite(row[0])
            size = finite(row[1], 0.0)
        else:
            continue
        if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
            levels.append((price, size))
    levels.sort(key=lambda pair: pair[0], reverse=not buy)
    return levels


def parse_book(raw: dict[str, Any], *, received_ms: int) -> dict[str, Any] | None:
    token = str(raw.get("asset_id") or raw.get("token_id") or "").strip()
    if not token:
        return None
    bids = _clean_levels(raw.get("bids"), buy=False)
    asks = _clean_levels(raw.get("asks"), buy=True)
    if not bids and not asks:
        return None
    return {
        "token": token,
        "bids": bids,
        "asks": asks,
        "min_order": max(0.0, finite(raw.get("min_order_size"), 0.0)),
        "received_ms": int(received_ms),
        "exchange_ts_ms": normalize_timestamp_ms(raw.get("timestamp")),
    }


def fetch_books(
    clob: str,
    tokens: Sequence[str],
    *,
    stats: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    receipts: list[int] = []
    unique = list(dict.fromkeys(str(token) for token in tokens if str(token)))
    for offset in range(0, len(unique), 80):
        raw = request_json(
            clob.rstrip("/") + "/books",
            [{"token_id": token} for token in unique[offset : offset + 80]],
        )
        received_ms = int(time.time() * 1000)
        receipts.append(received_ms)
        if stats is not None:
            stats["book_batches"] = int(stats.get("book_batches", 0)) + 1
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            parsed = parse_book(item, received_ms=received_ms)
            if parsed is not None:
                output[str(parsed["token"])] = parsed
    if stats is not None:
        stats["book_calls"] = int(stats.get("book_calls", 0)) + 1
        if receipts:
            stats["max_observed_batch_receive_span_ms"] = max(
                int(stats.get("max_observed_batch_receive_span_ms", 0)),
                max(receipts) - min(receipts),
            )
    return output


def round_fee_usdc(value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        return 0.0
    rounded = Decimal(str(value)).quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
    return float(rounded) if rounded >= FEE_QUANTUM else 0.0


def fee_amount(shares: float, price: float, details: FeeDetails, *, taker: bool = True) -> float:
    if shares <= 0.0 or not details.verified or not 0.0 < price < 1.0:
        return 0.0
    if details.rate <= 0.0 or (details.taker_only and not taker):
        return 0.0
    per_share = details.rate * (price * (1.0 - price)) ** max(0.0, details.exponent)
    return round_fee_usdc(shares * per_share)


def walk_book_for_shares(
    levels: Iterable[tuple[float, float]],
    shares: float,
    details: FeeDetails,
    *,
    buy: bool,
    slippage_bps: float = 0.0,
    require_full: bool = True,
) -> BookFill | None:
    target = max(0.0, finite(shares, 0.0))
    if target <= 0.0:
        return None
    remaining = target
    filled = raw_cash = stressed_cash = fees = 0.0
    stress = max(0.0, finite(slippage_bps, 0.0)) / 1e4
    clean = sorted(
        [(finite(p), finite(q, 0.0)) for p, q in levels if math.isfinite(finite(p)) and 0.0 < finite(p) < 1.0 and finite(q, 0.0) > 0.0],
        key=lambda pair: pair[0],
        reverse=not buy,
    )
    for price, size in clean:
        take = min(remaining, size)
        if take <= 0.0:
            continue
        stressed_price = min(0.999999, price * (1.0 + stress)) if buy else max(0.000001, price * (1.0 - stress))
        raw_cash += take * price
        stressed_cash += take * stressed_price
        fees += fee_amount(take, stressed_price, details, taker=True)
        filled += take
        remaining -= take
        if remaining <= 1e-10:
            break
    complete = remaining <= max(1e-9, 1e-8 * target)
    if filled <= 1e-12 or (require_full and not complete):
        return None
    raw_vwap = raw_cash / filled
    stressed_vwap = stressed_cash / filled
    all_in = (stressed_cash + fees) / filled if buy else (stressed_cash - fees) / filled
    return BookFill(
        target,
        filled,
        raw_cash,
        stressed_cash,
        fees,
        raw_vwap,
        stressed_vwap,
        all_in,
        abs(stressed_cash - raw_cash),
        complete,
    )


def _record_freshness(
    live: dict[str, dict[str, Any]],
    tokens: Sequence[str],
    *,
    now_ms: int,
    max_leg_age_ms: int,
    max_cross_leg_skew_ms: int,
    max_exchange_snapshot_age_ms: int,
    max_exchange_snapshot_skew_ms: int,
    stats: dict[str, Any],
) -> tuple[bool, str, int, int, int, int]:
    receive_ok, receive_reason, age, skew = local_book_freshness(
        live,
        tokens,
        now_ms=now_ms,
        max_leg_age_ms=max_leg_age_ms,
        max_cross_leg_skew_ms=max_cross_leg_skew_ms,
    )
    stats["freshness_checks"] = int(stats.get("freshness_checks", 0)) + 1
    stats["max_observed_leg_age_ms"] = max(int(stats.get("max_observed_leg_age_ms", 0)), age)
    stats["max_observed_cross_leg_skew_ms"] = max(int(stats.get("max_observed_cross_leg_skew_ms", 0)), skew)
    if not receive_ok:
        stats["receive_rejections"] = int(stats.get("receive_rejections", 0)) + 1
        return False, receive_reason, age, skew, 0, 0
    exchange_ok, exchange_reason, exchange_age, exchange_skew = exchange_book_freshness(
        live,
        tokens,
        now_ms=now_ms,
        max_snapshot_age_ms=max_exchange_snapshot_age_ms,
        max_snapshot_skew_ms=max_exchange_snapshot_skew_ms,
    )
    stats["max_observed_exchange_snapshot_age_ms"] = max(
        int(stats.get("max_observed_exchange_snapshot_age_ms", 0)), exchange_age
    )
    stats["max_observed_exchange_snapshot_skew_ms"] = max(
        int(stats.get("max_observed_exchange_snapshot_skew_ms", 0)), exchange_skew
    )
    if not exchange_ok:
        stats["exchange_rejections"] = int(stats.get("exchange_rejections", 0)) + 1
        return False, exchange_reason, age, skew, exchange_age, exchange_skew
    return True, "ok", age, skew, exchange_age, exchange_skew


def plan_bundle(
    live: dict[str, dict[str, Any]],
    tokens: Sequence[str],
    fees: dict[str, FeeDetails],
    shares: float,
    slippage_bps: float,
) -> BundlePlan | None:
    capital = raw = total_fees = total_slippage = 0.0
    fills: list[dict[str, Any]] = []
    for token in tokens:
        book = live.get(token)
        details = fees.get(token)
        if book is None or details is None or not details.verified:
            return None
        fill = walk_book_for_shares(
            book.get("asks", []),
            shares,
            details,
            buy=True,
            slippage_bps=slippage_bps,
            require_full=True,
        )
        if fill is None:
            return None
        leg_cost = fill.stressed_cash + fill.fee
        capital += leg_cost
        raw += fill.raw_cash
        total_fees += fill.fee
        total_slippage += fill.slippage_cost
        fills.append(
            {
                "token": token,
                "shares": fill.filled_shares,
                "price": fill.stressed_vwap,
                "raw_vwap": fill.raw_vwap,
                "fee": fill.fee,
                "capital_used": leg_cost,
                "slippage_cost": fill.slippage_cost,
                "total_execution_cost": fill.fee + fill.slippage_cost,
            }
        )
    return BundlePlan(capital, raw, total_fees, total_slippage, tuple(fills))


def max_bundle_for_cash(
    live: dict[str, dict[str, Any]],
    tokens: Sequence[str],
    fees: dict[str, FeeDetails],
    cash_cap: float,
    slippage_bps: float,
    *,
    minimum_shares: float,
) -> tuple[float, BundlePlan] | None:
    cap = max(0.0, finite(cash_cap, 0.0))
    if cap <= 0.0 or not tokens:
        return None
    depth_cap = math.inf
    for token in tokens:
        book = live.get(token)
        if book is None:
            return None
        depth_cap = min(depth_cap, sum(size for _, size in book.get("asks", [])))
    if not math.isfinite(depth_cap) or depth_cap + 1e-12 < minimum_shares:
        return None
    lo = 0.0
    hi = depth_cap
    best: tuple[float, BundlePlan] | None = None
    for _ in range(42):
        mid = (lo + hi) / 2.0
        if mid <= 1e-12:
            break
        plan = plan_bundle(live, tokens, fees, mid, slippage_bps)
        if plan is not None and plan.capital_used <= cap + 1e-9:
            best = (mid, plan)
            lo = mid
        else:
            hi = mid
    if best is None or best[0] + 1e-12 < minimum_shares:
        return None
    return best


def _yes_token(raw: dict[str, Any]) -> str:
    token_ids = [str(item) for item in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(item).strip().lower() for item in parse_array(raw.get("outcomes"))]
    if not token_ids:
        return ""
    yes_index = 0
    for index, outcome in enumerate(outcomes[: len(token_ids)]):
        if outcome == "yes":
            yes_index = index
            break
    return token_ids[yes_index] if yes_index < len(token_ids) else ""


def _event_id(raw: dict[str, Any]) -> str:
    direct = str(raw.get("eventId") or "").strip()
    if direct:
        return direct
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return str(events[0].get("id") or "").strip()
    return ""


def discover_event_ids(gamma: str, limit: int, min_liquidity: float, max_events: int) -> list[str]:
    event_ids: list[str] = []
    offset = 0
    while offset < 5000 and offset < max(100, limit) and len(event_ids) < max_events:
        page_size = min(100, max(1, limit - offset))
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
        payload = request_json(f"{gamma.rstrip('/')}/markets?{query}")
        batch = payload if isinstance(payload, list) else payload.get("markets", []) if isinstance(payload, dict) else []
        if not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict) or not bool(raw.get("negRisk", False)):
                continue
            liquidity = max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0)))
            if liquidity < min_liquidity:
                continue
            event_id = _event_id(raw)
            if event_id and event_id not in event_ids:
                event_ids.append(event_id)
                if len(event_ids) >= max_events:
                    break
        if len(batch) < page_size:
            break
        offset += page_size
    return event_ids


def event_spec(gamma: str, event_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    event = request_json(f"{gamma.rstrip('/')}/events/{event_id}")
    if (
        not isinstance(event, dict)
        or bool(event.get("closed", False))
        or not bool(event.get("negRisk", False))
        or bool(event.get("negRiskAugmented", False))
    ):
        return None
    raw_markets = event.get("markets")
    if not isinstance(raw_markets, list) or len(raw_markets) < 2:
        return None
    legs: list[dict[str, Any]] = []
    for raw in raw_markets:
        if not isinstance(raw, dict):
            return None
        market_id = str(raw.get("id") or "").strip()
        condition_id = str(raw.get("conditionId") or "").strip()
        token = _yes_token(raw)
        if not market_id or not condition_id or not token:
            return None
        legs.append(
            {
                "market_id": market_id,
                "condition_id": condition_id,
                "token": token,
                "raw": raw,
            }
        )
    return event, legs


def resolve_verified_fees(
    legs: Sequence[dict[str, Any]],
    clob: str,
    stats: dict[str, Any],
    sources: Counter[str],
) -> dict[str, FeeDetails] | None:
    output: dict[str, FeeDetails] = {}
    for leg in legs:
        details = resolve_fee_details(
            leg["raw"],
            clob,
            str(leg["condition_id"]),
            str(leg["token"]),
        )
        sources[details.source] += 1
        if not details.verified or details.rate < 0.0:
            stats["unverified_fee_rejections"] = int(stats.get("unverified_fee_rejections", 0)) + 1
            return None
        output[str(leg["token"])] = details
    return output


def append_csv(path: Path, fieldnames: Sequence[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fieldnames})


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)



def normalize_runtime_state(state: dict[str, Any], start: float) -> dict[str, Any]:
    """Normalize the pre-cutover V7-wrapper state without importing legacy code."""
    raw_open = state.get("open_bundles")
    if not isinstance(raw_open, dict):
        raw_open = state.get("bundles") if isinstance(state.get("bundles"), dict) else {}
    open_bundles: dict[str, dict[str, Any]] = {}
    for key, raw in raw_open.items():
        if not isinstance(raw, dict):
            continue
        bundle = dict(raw)
        bundle["event_id"] = str(bundle.get("event_id") or key)
        bundle["capital_used"] = max(
            0.0, finite(bundle.get("capital_used"), finite(bundle.get("cost"), 0.0))
        )
        if "leg_count" not in bundle:
            bundle["leg_count"] = int(finite(bundle.get("legs"), 0.0))
        open_bundles[str(key)] = bundle

    raw_aborting = state.get("aborting") if isinstance(state.get("aborting"), dict) else {}
    aborting: dict[str, dict[str, Any]] = {}
    for key, raw in raw_aborting.items():
        if not isinstance(raw, dict):
            continue
        bundle = dict(raw)
        bundle["event_id"] = str(bundle.get("event_id") or key)
        normalized_legs: list[dict[str, Any]] = []
        for raw_leg in bundle.get("legs", []) if isinstance(bundle.get("legs"), list) else []:
            if not isinstance(raw_leg, dict):
                continue
            leg = dict(raw_leg)
            leg["capital_used"] = max(
                0.0, finite(leg.get("capital_used"), finite(leg.get("cost"), 0.0))
            )
            if "raw_market" not in leg and isinstance(leg.get("market"), dict):
                leg["raw_market"] = leg["market"]
            leg["total_execution_cost"] = max(
                0.0,
                finite(
                    leg.get("total_execution_cost"),
                    finite(leg.get("fee"), 0.0) + finite(leg.get("slippage_cost"), 0.0),
                ),
            )
            normalized_legs.append(leg)
        bundle["legs"] = normalized_legs
        bundle["capital_used"] = sum(
            max(0.0, finite(leg.get("capital_used"), 0.0)) for leg in normalized_legs
        )
        aborting[str(key)] = bundle

    return {
        "cash": max(0.0, finite(state.get("cash"), start)),
        "peak": max(start, finite(state.get("peak"), start)),
        "killed": bool(state.get("killed", False)),
        "open_bundles": open_bundles,
        "aborting": aborting,
        "realized_pnl_total": finite(
            state.get("realized_pnl_total"), finite(state.get("realized_pnl"), 0.0)
        ),
    }


def abort_mark(
    clob: str,
    aborting: dict[str, Any],
    *,
    slippage_bps: float,
    stats: dict[str, Any],
) -> float:
    value = 0.0
    for bundle in aborting.values():
        for leg in bundle.get("legs", []) if isinstance(bundle, dict) else []:
            token = str(leg.get("token") or "")
            shares = max(0.0, finite(leg.get("shares"), 0.0))
            if not token or shares <= 0.0:
                continue
            try:
                book = fetch_books(clob, [token], stats=stats).get(token)
            except Exception:
                book = None
            if book and book.get("bids"):
                value += shares * max(0.0, book["bids"][0][0] * (1.0 - max(0.0, slippage_bps) / 1e4))
    return value


def attempt_unwind(
    *,
    clob: str,
    bundle_id: str,
    event_id: str,
    legs: list[dict[str, Any]],
    fees: dict[str, FeeDetails],
    slippage_bps: float,
    run_dir: Path,
    stats: dict[str, Any],
) -> tuple[list[dict[str, Any]], float, float, float]:
    residual: list[dict[str, Any]] = []
    received_cash = realized = unwind_cost = 0.0
    for index, leg in enumerate(legs):
        token = str(leg.get("token") or "")
        old_shares = max(0.0, finite(leg.get("shares"), 0.0))
        cost_basis = max(0.0, finite(leg.get("capital_used"), 0.0))
        if not token or old_shares <= 0.0:
            continue
        try:
            book = fetch_books(clob, [token], stats=stats).get(token)
        except Exception:
            book = None
        details = fees.get(token)
        fill = (
            walk_book_for_shares(
                book.get("bids", []),
                old_shares,
                details,
                buy=False,
                slippage_bps=slippage_bps,
                require_full=False,
            )
            if book is not None and details is not None and details.verified
            else None
        )
        if fill is None or fill.filled_shares <= 1e-12:
            residual.append(dict(leg))
            continue
        sold = fill.filled_shares
        fraction = min(1.0, sold / max(old_shares, 1e-12))
        allocated_basis = cost_basis * fraction
        proceeds = fill.stressed_cash - fill.fee
        pnl = proceeds - allocated_basis
        received_cash += proceeds
        realized += pnl
        entry_exec_cost = max(0.0, finite(leg.get("total_execution_cost"), 0.0)) * fraction
        row_cost = entry_exec_cost + fill.fee + fill.slippage_cost
        unwind_cost += row_cost
        append_csv(
            run_dir / "fills.csv",
            FILL_FIELDS,
            {
                "timestamp": int(time.time()),
                "bundle_id": bundle_id,
                "strategy": "HARD_ARB",
                "event_id": event_id,
                "action": "UNWIND",
                "token": token,
                "leg_index": index + 1,
                "leg_count": len(legs),
                "shares": sold,
                "price": fill.stressed_vwap,
                "capital_used": allocated_basis,
                "fee": fill.fee,
                "total_fees": fill.fee,
                "slippage_cost": fill.slippage_cost,
                "total_execution_cost": row_cost,
                "net_pnl": pnl,
                "detail": "forced_partial_bundle_unwind",
            },
        )
        remaining = max(0.0, old_shares - sold)
        if remaining > 1e-9:
            updated = dict(leg)
            updated["shares"] = remaining
            updated["capital_used"] = max(0.0, cost_basis - allocated_basis)
            updated["total_execution_cost"] = max(
                0.0, finite(leg.get("total_execution_cost"), 0.0) - entry_exec_cost
            )
            residual.append(updated)
    return residual, received_cash, realized, unwind_cost


def _initial_stats() -> dict[str, Any]:
    return {
        "book_calls": 0,
        "book_batches": 0,
        "freshness_checks": 0,
        "receive_rejections": 0,
        "exchange_rejections": 0,
        "unverified_fee_rejections": 0,
        "max_observed_leg_age_ms": 0,
        "max_observed_cross_leg_skew_ms": 0,
        "max_observed_exchange_snapshot_age_ms": 0,
        "max_observed_exchange_snapshot_skew_ms": 0,
        "max_observed_batch_receive_span_ms": 0,
    }


def self_test() -> int:
    now_ms = 10_100
    live = {
        "a": {
            "received_ms": 10_000,
            "exchange_ts_ms": 9_990,
            "asks": [(0.40, 10.0), (0.41, 10.0)],
            "bids": [(0.39, 20.0)],
            "min_order": 1.0,
        },
        "b": {
            "received_ms": 10_040,
            "exchange_ts_ms": 10_010,
            "asks": [(0.50, 20.0)],
            "bids": [(0.49, 20.0)],
            "min_order": 1.0,
        },
    }
    ok, reason, age, skew = local_book_freshness(
        live, ["a", "b"], now_ms=now_ms, max_leg_age_ms=200, max_cross_leg_skew_ms=100
    )
    assert ok and reason == "ok" and age == 100 and skew == 40
    ok, reason, age, skew = exchange_book_freshness(
        live, ["a", "b"], now_ms=now_ms, max_snapshot_age_ms=200, max_snapshot_skew_ms=100
    )
    assert ok and reason == "ok" and age == 110 and skew == 20

    free = FeeDetails(0.0, 1.0, True, True, "test:free")
    plan = plan_bundle(live, ["a", "b"], {"a": free, "b": free}, 10.0, 0.0)
    assert plan is not None
    assert abs(plan.capital_used - 9.0) < 1e-9
    assert abs(1.0 - plan.capital_used / 10.0 - 0.1) < 1e-9
    assert plan_bundle(live, ["a", "b"], {"a": free, "b": free}, 21.0, 0.0) is None

    live["b"]["received_ms"] = 10_300
    ok, reason, _, _ = local_book_freshness(
        live, ["a", "b"], now_ms=10_320, max_leg_age_ms=500, max_cross_leg_skew_ms=100
    )
    assert not ok and reason == "cross_leg_skew"
    assert normalize_timestamp_ms(1_787_700_000) == 1_787_700_000_000
    assert normalize_timestamp_ms(1_787_700_000_123) == 1_787_700_000_123
    migrated = normalize_runtime_state(
        {
            "cash": 90.0,
            "peak": 100.0,
            "bundles": {"e1": {"shares": 10.0, "cost": 9.0, "legs": 2}},
            "realized_pnl": 1.25,
            "aborting": {
                "e2": {
                    "legs": [
                        {"token": "a", "shares": 2.0, "cost": 0.8, "fee": 0.01, "market": {"conditionId": "c"}}
                    ]
                }
            },
        },
        100.0,
    )
    assert migrated["open_bundles"]["e1"]["capital_used"] == 9.0
    assert migrated["open_bundles"]["e1"]["event_id"] == "e1"
    assert migrated["realized_pnl_total"] == 1.25
    assert migrated["aborting"]["e2"]["legs"][0]["raw_market"]["conditionId"] == "c"
    print("v7_hard_arb_guard_self_test=ok")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V7 PAPER structural complete-set execution")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--markets", type=int, default=1000)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--max-events", type=int, default=80)
    parser.add_argument("--min-edge", type=float, default=0.00005)
    parser.add_argument("--max-trade-usd", type=float, default=1e100)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--leg-latency-ms", type=int, default=100)
    parser.add_argument("--max-leg-age-ms", type=int, default=2000)
    parser.add_argument("--max-cross-leg-skew-ms", type=int, default=1000)
    parser.add_argument("--max-exchange-snapshot-age-ms", type=int, default=5000)
    parser.add_argument("--max-exchange-snapshot-skew-ms", type=int, default=1000)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not bool(cfg.get("paper_only", True)):
        raise RuntimeError("v7_hard_arb_requires_paper_only_config")
    gamma = str(cfg["gamma_url"])
    clob = str(cfg["clob_url"])
    start = max(0.0, finite(cfg.get("starting_capital"), 0.0))
    max_drawdown = max(0.0, min(1.0, finite(cfg.get("max_drawdown"), 0.15)))
    max_trade_fraction = max(0.0, finite(cfg.get("max_trade_fraction"), 1.0))
    max_gross_fraction = max(0.0, finite(cfg.get("max_gross_fraction"), 1.0))
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    state = read_json(
        state_path,
        {
            "cash": start,
            "peak": start,
            "killed": False,
            "open_bundles": {},
            "aborting": {},
            "realized_pnl_total": 0.0,
        },
    )
    state = normalize_runtime_state(state, start)
    cash = state["cash"]
    peak = state["peak"]
    killed = bool(state["killed"])
    open_bundles = state["open_bundles"]
    aborting = state["aborting"]
    realized_total = state["realized_pnl_total"]
    realized_tick = 0.0
    failures: list[str] = []
    stats = _initial_stats()
    fee_sources: Counter[str] = Counter()
    now = int(time.time())

    # Guaranteed complete sets are settled only after the public event closes.
    for bundle_id, bundle in list(open_bundles.items()):
        try:
            event_id = str(bundle.get("event_id") or "")
            event = request_json(f"{gamma.rstrip('/')}/events/{event_id}")
            if isinstance(event, dict) and bool(event.get("closed", False)):
                shares = max(0.0, finite(bundle.get("shares"), 0.0))
                capital = max(0.0, finite(bundle.get("capital_used"), 0.0))
                payout = shares
                pnl = payout - capital
                cash += payout
                realized_tick += pnl
                append_csv(
                    args.run_dir / "fills.csv",
                    FILL_FIELDS,
                    {
                        "timestamp": now,
                        "bundle_id": bundle_id,
                        "strategy": "HARD_ARB",
                        "event_id": event_id,
                        "action": "CLOSE_STRUCTURAL_PAYOUT",
                        "leg_count": int(finite(bundle.get("leg_count"), 0.0)),
                        "shares": shares,
                        "capital_used": capital,
                        "total_fees": max(0.0, finite(bundle.get("total_fees"), 0.0)),
                        "slippage_cost": max(0.0, finite(bundle.get("slippage_cost"), 0.0)),
                        "total_execution_cost": max(0.0, finite(bundle.get("total_execution_cost"), 0.0)),
                        "net_edge": finite(bundle.get("net_edge"), 0.0),
                        "net_pnl": pnl,
                        "detail": "neg_risk_complete_set_terminal_payout",
                    },
                )
                del open_bundles[bundle_id]
        except Exception as exc:
            if len(failures) < 20:
                failures.append(f"settle:{bundle_id}:{type(exc).__name__}:{exc}")

    # Residual partial bundles block new risk and are unwound before fresh entries.
    for bundle_id, bundle in list(aborting.items()):
        legs = bundle.get("legs") if isinstance(bundle.get("legs"), list) else []
        fee_map: dict[str, FeeDetails] = {}
        for leg in legs:
            raw = leg.get("raw_market") if isinstance(leg, dict) else None
            if not isinstance(raw, dict):
                continue
            token = str(leg.get("token") or "")
            details = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
            fee_sources[details.source] += 1
            if details.verified:
                fee_map[token] = details
        residual, received, pnl, _ = attempt_unwind(
            clob=clob,
            bundle_id=bundle_id,
            event_id=str(bundle.get("event_id") or ""),
            legs=legs,
            fees=fee_map,
            slippage_bps=args.slippage_bps,
            run_dir=args.run_dir,
            stats=stats,
        )
        cash += received
        realized_tick += pnl
        if residual:
            bundle["legs"] = residual
            bundle["capital_used"] = sum(max(0.0, finite(leg.get("capital_used"), 0.0)) for leg in residual)
        else:
            del aborting[bundle_id]

    locked_capital = sum(max(0.0, finite(bundle.get("capital_used"), 0.0)) for bundle in open_bundles.values())
    abort_value = abort_mark(clob, aborting, slippage_bps=args.slippage_bps, stats=stats) if aborting else 0.0
    equity = cash + locked_capital + abort_value
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    killed = killed or drawdown >= max_drawdown

    scanned = positive = entered = sequential_aborts = 0
    best_edge = 0.0
    candidate_rows = 0
    event_ids: list[str] = []
    if not killed and not aborting:
        try:
            event_ids = discover_event_ids(gamma, args.markets, args.min_liquidity, args.max_events)
        except Exception as exc:
            failures.append(f"discover:{type(exc).__name__}:{exc}")

    for event_id in event_ids:
        if any(str(bundle.get("event_id") or "") == event_id for bundle in open_bundles.values()):
            continue
        try:
            spec = event_spec(gamma, event_id)
            if spec is None:
                continue
            _, legs = spec
            tokens = [str(leg["token"]) for leg in legs]
            fees = resolve_verified_fees(legs, clob, stats, fee_sources)
            if fees is None:
                continue
            live = fetch_books(clob, tokens, stats=stats)
            scanned += 1
            freshness = _record_freshness(
                live,
                tokens,
                now_ms=int(time.time() * 1000),
                max_leg_age_ms=args.max_leg_age_ms,
                max_cross_leg_skew_ms=args.max_cross_leg_skew_ms,
                max_exchange_snapshot_age_ms=args.max_exchange_snapshot_age_ms,
                max_exchange_snapshot_skew_ms=args.max_exchange_snapshot_skew_ms,
                stats=stats,
            )
            ok, reason, age, skew, exchange_age, exchange_skew = freshness
            minimum_shares = max(
                [max(0.0, finite(live.get(token, {}).get("min_order"), 0.0)) for token in tokens] + [1.0]
            )
            gross_room = max(0.0, max_gross_fraction * max(equity, 0.0) - locked_capital)
            trade_room = max(0.0, max_trade_fraction * max(equity, 0.0))
            cap = min(max(0.0, args.max_trade_usd), cash, gross_room, trade_room)
            sized = max_bundle_for_cash(
                live,
                tokens,
                fees,
                cap,
                args.slippage_bps,
                minimum_shares=minimum_shares,
            ) if ok else None
            if sized is None:
                candidate_rows += 1
                append_csv(
                    args.run_dir / "candidates.csv",
                    CANDIDATE_FIELDS,
                    {
                        "timestamp": int(time.time()),
                        "bundle_id": f"HARD-{event_id}-{int(time.time() * 1000)}",
                        "strategy": "HARD_ARB",
                        "action": "REJECT",
                        "expected_edge": "",
                        "event_id": event_id,
                        "legs": len(tokens),
                        "max_leg_age_ms": age,
                        "cross_leg_skew_ms": skew,
                        "max_exchange_snapshot_age_ms": exchange_age,
                        "exchange_snapshot_skew_ms": exchange_skew,
                        "fee_sources": json.dumps(dict(fee_sources), sort_keys=True),
                        "executable": 0,
                        "reason": reason if not ok else "insufficient_depth_or_capital",
                    },
                )
                continue
            shares, initial_plan = sized
            raw_edge = 1.0 - initial_plan.raw_cash / shares
            net_edge = 1.0 - initial_plan.capital_used / shares
            best_edge = max(best_edge, net_edge)
            executable = net_edge > args.min_edge
            candidate_rows += 1
            bundle_id = f"HARD-{event_id}-{int(time.time() * 1000)}"
            append_csv(
                args.run_dir / "candidates.csv",
                CANDIDATE_FIELDS,
                {
                    "timestamp": int(time.time()),
                    "bundle_id": bundle_id,
                    "strategy": "HARD_ARB",
                    "action": "SUBMIT" if executable else "REJECT",
                    "expected_edge": net_edge if executable else "",
                    "event_id": event_id,
                    "legs": len(tokens),
                    "shares": shares,
                    "raw_edge": raw_edge,
                    "net_edge": net_edge,
                    "capital_used": initial_plan.capital_used,
                    "total_fees": initial_plan.total_fees,
                    "slippage_cost": initial_plan.slippage_cost,
                    "total_execution_cost": initial_plan.total_fees + initial_plan.slippage_cost,
                    "max_leg_age_ms": age,
                    "cross_leg_skew_ms": skew,
                    "max_exchange_snapshot_age_ms": exchange_age,
                    "exchange_snapshot_skew_ms": exchange_skew,
                    "fee_sources": json.dumps(dict(fee_sources), sort_keys=True),
                    "executable": int(executable),
                    "reason": "positive_post_cost_edge" if executable else "net_edge_gate",
                },
            )
            if not executable:
                continue
            positive += 1

            # Execute the hardest (least ask depth) leg first. Before every leg,
            # refresh all remaining legs and recompute the guaranteed terminal edge.
            ask_depth = {
                token: sum(size for _, size in live.get(token, {}).get("asks", []))
                for token in tokens
            }
            order = sorted(tokens, key=lambda token: (ask_depth.get(token, math.inf), token))
            execution_cost = 0.0
            raw_execution = 0.0
            total_fees = 0.0
            total_slippage = 0.0
            filled_legs: list[dict[str, Any]] = []
            failure_reason = ""
            leg_by_token = {str(leg["token"]): leg for leg in legs}
            for leg_index, token in enumerate(order):
                remaining_tokens = order[leg_index:]
                refreshed = fetch_books(clob, remaining_tokens, stats=stats)
                fresh = _record_freshness(
                    refreshed,
                    remaining_tokens,
                    now_ms=int(time.time() * 1000),
                    max_leg_age_ms=args.max_leg_age_ms,
                    max_cross_leg_skew_ms=args.max_cross_leg_skew_ms,
                    max_exchange_snapshot_age_ms=args.max_exchange_snapshot_age_ms,
                    max_exchange_snapshot_skew_ms=args.max_exchange_snapshot_skew_ms,
                    stats=stats,
                )
                if not fresh[0]:
                    failure_reason = f"freshness:{fresh[1]}"
                    break
                remaining_plan = plan_bundle(refreshed, remaining_tokens, fees, shares, args.slippage_bps)
                if remaining_plan is None:
                    failure_reason = "remaining_depth"
                    break
                guarantee = 1.0 - (execution_cost + remaining_plan.capital_used) / shares
                if guarantee <= args.min_edge:
                    failure_reason = "edge_revalidation"
                    break
                current_fill = next((fill for fill in remaining_plan.fills if fill["token"] == token), None)
                if current_fill is None:
                    failure_reason = "current_leg_missing"
                    break
                leg_cost = float(current_fill["capital_used"])
                if leg_cost > cash + 1e-9:
                    failure_reason = "capital"
                    break
                cash -= leg_cost
                execution_cost += leg_cost
                raw_execution += float(current_fill["raw_vwap"]) * shares
                total_fees += float(current_fill["fee"])
                total_slippage += float(current_fill["slippage_cost"])
                filled = {
                    **current_fill,
                    "raw_market": leg_by_token[token]["raw"],
                }
                filled_legs.append(filled)
                append_csv(
                    args.run_dir / "leg_fills.csv",
                    LEG_FIELDS,
                    {
                        "timestamp": int(time.time()),
                        "bundle_id": bundle_id,
                        "strategy": "HARD_ARB",
                        "event_id": event_id,
                        "action": "BUY_LEG_FOK",
                        "token": token,
                        "leg_index": leg_index + 1,
                        "leg_count": len(order),
                        "shares": shares,
                        "price": current_fill["price"],
                        "capital_used": leg_cost,
                        "fee": current_fill["fee"],
                        "total_fees": current_fill["fee"],
                        "slippage_cost": current_fill["slippage_cost"],
                        "total_execution_cost": current_fill["total_execution_cost"],
                        "net_edge": guarantee,
                        "detail": f"leg={leg_index + 1}/{len(order)} sequential_revalidation",
                    },
                )
                if leg_index + 1 < len(order) and args.leg_latency_ms > 0:
                    time.sleep(max(0, args.leg_latency_ms) / 1000.0)

            if failure_reason:
                sequential_aborts += 1
                residual, received, unwind_pnl, _ = attempt_unwind(
                    clob=clob,
                    bundle_id=bundle_id,
                    event_id=event_id,
                    legs=filled_legs,
                    fees=fees,
                    slippage_bps=args.slippage_bps,
                    run_dir=args.run_dir,
                    stats=stats,
                )
                cash += received
                realized_tick += unwind_pnl
                if residual:
                    aborting[bundle_id] = {
                        "event_id": event_id,
                        "legs": residual,
                        "capital_used": sum(max(0.0, finite(leg.get("capital_used"), 0.0)) for leg in residual),
                        "reason": failure_reason,
                        "opened_ts": now,
                    }
                continue

            final_edge = 1.0 - execution_cost / shares
            if final_edge <= args.min_edge:
                # This should be unreachable because every remaining-set revalidation
                # included already paid legs, but fail closed if arithmetic drifts.
                sequential_aborts += 1
                residual, received, unwind_pnl, _ = attempt_unwind(
                    clob=clob,
                    bundle_id=bundle_id,
                    event_id=event_id,
                    legs=filled_legs,
                    fees=fees,
                    slippage_bps=args.slippage_bps,
                    run_dir=args.run_dir,
                    stats=stats,
                )
                cash += received
                realized_tick += unwind_pnl
                if residual:
                    aborting[bundle_id] = {
                        "event_id": event_id,
                        "legs": residual,
                        "capital_used": sum(max(0.0, finite(leg.get("capital_used"), 0.0)) for leg in residual),
                        "reason": "post_execution_edge",
                        "opened_ts": now,
                    }
                continue

            open_bundles[bundle_id] = {
                "event_id": event_id,
                "shares": shares,
                "capital_used": execution_cost,
                "raw_edge": 1.0 - raw_execution / shares,
                "net_edge": final_edge,
                "total_fees": total_fees,
                "slippage_cost": total_slippage,
                "total_execution_cost": total_fees + total_slippage,
                "leg_count": len(order),
                "opened_ts": now,
            }
            entered += 1
            locked_capital += execution_cost
            append_csv(
                args.run_dir / "fills.csv",
                FILL_FIELDS,
                {
                    "timestamp": int(time.time()),
                    "bundle_id": bundle_id,
                    "strategy": "HARD_ARB",
                    "event_id": event_id,
                    "action": "BUY_COMPLETE_YES_SET_SEQUENTIAL",
                    "leg_count": len(order),
                    "shares": shares,
                    "capital_used": execution_cost,
                    "net_edge": final_edge,
                    "detail": "neg_risk_complete_set;paper_only;all_legs_fok",
                },
            )
            # Recompute current capital room before considering another event.
            equity = cash + locked_capital + abort_mark(
                clob, aborting, slippage_bps=args.slippage_bps, stats=stats
            ) if aborting else cash + locked_capital
        except Exception as exc:
            if len(failures) < 20:
                failures.append(f"event:{event_id}:{type(exc).__name__}:{exc}")

    realized_total += realized_tick
    abort_value = abort_mark(clob, aborting, slippage_bps=args.slippage_bps, stats=stats) if aborting else 0.0
    locked_capital = sum(max(0.0, finite(bundle.get("capital_used"), 0.0)) for bundle in open_bundles.values())
    locked_expected_profit = sum(
        max(0.0, finite(bundle.get("shares"), 0.0) - finite(bundle.get("capital_used"), 0.0))
        for bundle in open_bundles.values()
    )
    equity = cash + locked_capital + abort_value
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak > 0.0 else 0.0
    killed = killed or drawdown >= max_drawdown
    output = {
        "schema": "polymarket_v7_hard_arb_status_v2",
        "timestamp": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "cash": cash,
        "equity_cost_basis": equity,
        "peak": peak,
        "drawdown": drawdown,
        "killed": killed,
        "open_bundles": open_bundles,
        "aborting": aborting,
        "aborting_bundles": len(aborting),
        "realized_pnl_last_tick": realized_tick,
        "realized_pnl_total": realized_total,
        "locked_expected_profit": locked_expected_profit,
        "scanned_events": scanned,
        "positive_candidates": positive,
        "candidate_rows": candidate_rows,
        "entered": entered,
        "sequential_aborts": sequential_aborts,
        "best_edge": best_edge,
        "fee_sources_last_tick": dict(fee_sources),
        "failures": failures,
        "atomic_snapshot_assumption": False,
        "per_token_receive_timestamps": True,
        "exchange_snapshot_timestamps": True,
        "multi_level_depth": True,
        "verified_fees_required": True,
        "sequential_leg_revalidation": True,
        "unwind_on_leg_failure": True,
        "leg_latency_ms": max(0, args.leg_latency_ms),
        "max_leg_age_ms": max(0, args.max_leg_age_ms),
        "max_cross_leg_skew_ms": max(0, args.max_cross_leg_skew_ms),
        "max_exchange_snapshot_age_ms": max(0, args.max_exchange_snapshot_age_ms),
        "max_exchange_snapshot_skew_ms": max(0, args.max_exchange_snapshot_skew_ms),
        "freshness_guard": stats,
        "legacy_runtime_dependency": False,
        "execution_model": "v7_native_depth_fee_freshness_sequential_fok_unwind",
    }
    persisted = dict(output)
    persisted["cash"] = cash
    persisted["peak"] = peak
    persisted["killed"] = killed
    persisted["open_bundles"] = open_bundles
    persisted["aborting"] = aborting
    persisted["realized_pnl_total"] = realized_total
    atomic_json(state_path, persisted)
    atomic_json(args.run_dir / "status.json", output)
    print(
        f"v7_hard_arb scanned={scanned} positive={positive} entered={entered} "
        f"sequential_aborts={sequential_aborts} aborting={len(aborting)} "
        f"best_edge={best_edge:.8f} realized={realized_tick:.6f}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    effective = list(argv) if argv is not None else None
    if effective is None and len(os.sys.argv) > 1 and os.sys.argv[1] == "self-test":
        return self_test()
    if effective is not None and effective == ["self-test"]:
        return self_test()
    return run(parse_args(effective))


if __name__ == "__main__":
    raise SystemExit(main())
