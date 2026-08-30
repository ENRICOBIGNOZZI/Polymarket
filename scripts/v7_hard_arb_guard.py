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
from typing import Any, Callable, Iterable, Sequence

from v7_market_common import FeeDetails, finite, parse_array, request_json, resolve_fee_details
from v7_shared_market_state import SharedStateError, load_snapshot, synchronized_books

FEE_Q = Decimal("0.00001")
CANDIDATE_FIELDS = [
    "timestamp", "bundle_id", "strategy", "action", "expected_edge", "event_id", "legs", "shares",
    "raw_edge", "net_edge", "capital_used", "total_fees", "slippage_cost", "total_execution_cost",
    "max_leg_age_ms", "cross_leg_skew_ms", "max_exchange_snapshot_age_ms", "exchange_snapshot_skew_ms",
    "fee_sources", "executable", "reason",
]
FILL_FIELDS = [
    "timestamp", "bundle_id", "strategy", "event_id", "action", "token", "leg_index", "leg_count",
    "shares", "price", "capital_used", "fee", "total_fees", "slippage_cost", "total_execution_cost",
    "net_edge", "net_pnl", "detail",
]


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
    def buy_cash(self) -> float:
        return self.stressed_cash + self.fee

    @property
    def sell_cash(self) -> float:
        return self.stressed_cash - self.fee


@dataclass(frozen=True)
class BundlePlan:
    capital_used: float
    raw_cash: float
    total_fees: float
    slippage_cost: float
    fills: tuple[dict[str, Any], ...]

    @property
    def execution_cost(self) -> float:
        return self.total_fees + self.slippage_cost


def normalize_timestamp_ms(value: Any) -> int:
    ts = finite(value, 0.0)
    if ts <= 0.0:
        return 0
    if ts >= 1e14:
        ts /= 1000.0
    elif ts < 1e11:
        ts *= 1000.0
    return int(ts)


def local_book_freshness(
    live: dict[str, dict[str, Any]], tokens: Sequence[str], *, now_ms: int,
    max_leg_age_ms: int, max_cross_leg_skew_ms: int,
) -> tuple[bool, str, int, int]:
    stamps = [int(finite(live.get(t, {}).get("received_ms"), 0.0)) for t in tokens]
    if not tokens or any(ts <= 0 for ts in stamps):
        return False, "missing_receive_timestamp", 0, 0
    age, skew = max(0, now_ms - min(stamps)), max(stamps) - min(stamps)
    if age > max_leg_age_ms:
        return False, "max_leg_age", age, skew
    if skew > max_cross_leg_skew_ms:
        return False, "cross_leg_skew", age, skew
    return True, "ok", age, skew


def exchange_book_freshness(
    live: dict[str, dict[str, Any]], tokens: Sequence[str], *, now_ms: int,
    max_snapshot_age_ms: int, max_snapshot_skew_ms: int,
) -> tuple[bool, str, int, int]:
    stamps = [int(finite(live.get(t, {}).get("exchange_ts_ms"), 0.0)) for t in tokens]
    if not tokens or any(ts <= 0 for ts in stamps):
        return False, "missing_exchange_timestamp", 0, 0
    age, skew = max(0, now_ms - min(stamps)), max(stamps) - min(stamps)
    snapshot_ids = {str(live.get(t, {}).get("bus_snapshot_id") or "") for t in tokens}
    continuous = all(live.get(t, {}).get("lineage_continuous") is True for t in tokens)
    if continuous and len(snapshot_ids) == 1 and "" not in snapshot_ids:
        return True, "ok_shared_ws_lineage", age, skew
    if age > max_snapshot_age_ms:
        return False, "max_exchange_snapshot_age", age, skew
    if skew > max_snapshot_skew_ms:
        return False, "exchange_snapshot_skew", age, skew
    return True, "ok", age, skew


def _levels(rows: Any, *, buy: bool) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            p, q = finite(row.get("price")), finite(row.get("size"), 0.0)
        elif isinstance(row, (tuple, list)) and len(row) >= 2:
            p, q = finite(row[0]), finite(row[1], 0.0)
        else:
            continue
        if math.isfinite(p) and 0.0 < p < 1.0 and q > 0.0:
            out.append((p, q))
    out.sort(key=lambda z: z[0], reverse=not buy)
    return out


def parse_book(raw: dict[str, Any], received_ms: int) -> dict[str, Any] | None:
    token = str(raw.get("asset_id") or raw.get("token_id") or "").strip()
    if not token:
        return None
    bids, asks = _levels(raw.get("bids"), buy=False), _levels(raw.get("asks"), buy=True)
    if not bids and not asks:
        return None
    return {
        "token": token,
        "bids": bids,
        "asks": asks,
        "min_order": max(0.0, finite(raw.get("min_order_size"), 0.0)),
        "received_ms": received_ms,
        "exchange_ts_ms": normalize_timestamp_ms(raw.get("timestamp")),
    }


def fetch_books(clob: str, tokens: Sequence[str], stats: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    receipts: list[int] = []
    tokens = list(dict.fromkeys(str(t) for t in tokens if str(t)))
    for i in range(0, len(tokens), 80):
        raw = request_json(clob.rstrip("/") + "/books", [{"token_id": t} for t in tokens[i:i + 80]])
        received_ms = int(time.time() * 1000)
        receipts.append(received_ms)
        if stats is not None:
            stats["book_batches"] = int(stats.get("book_batches", 0)) + 1
        for item in raw if isinstance(raw, list) else []:
            book = parse_book(item, received_ms) if isinstance(item, dict) else None
            if book is not None:
                output[book["token"]] = book
    if stats is not None:
        stats["book_calls"] = int(stats.get("book_calls", 0)) + 1
        if receipts:
            stats["max_observed_batch_receive_span_ms"] = max(
                int(stats.get("max_observed_batch_receive_span_ms", 0)), max(receipts) - min(receipts)
            )
    return output


def execution_books(args: argparse.Namespace, clob: str, tokens: Sequence[str],
                    stats: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if args.shared_state is None:
        stats["rest_execution_book_reads"] = int(stats.get("rest_execution_book_reads", 0)) + 1
        return fetch_books(clob, tokens, stats)
    try:
        snapshot = load_snapshot(
            args.shared_state, expected_sha=args.model_sha,
            max_publish_age_ms=args.max_shared_publish_age_ms,
        )
        selected = synchronized_books(snapshot, tokens, require_continuous=True)
        stats["shared_state_reads"] = int(stats.get("shared_state_reads", 0)) + 1
        stats["shared_state_generation"] = int(snapshot["generation"])
        stats["shared_state_snapshot_id"] = str(snapshot["snapshot_id"])
        return selected
    except SharedStateError as exc:
        stats["shared_state_rejections"] = int(stats.get("shared_state_rejections", 0)) + 1
        stats["last_shared_state_rejection"] = str(exc)
        return {}


def round_fee_usdc(value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        return 0.0
    value = float(Decimal(str(value)).quantize(FEE_Q, rounding=ROUND_HALF_UP))
    return value if value >= float(FEE_Q) else 0.0


def fee_amount(shares: float, price: float, details: FeeDetails) -> float:
    if shares <= 0.0 or not details.verified or details.rate <= 0.0 or not 0.0 < price < 1.0:
        return 0.0
    return round_fee_usdc(shares * details.rate * (price * (1.0 - price)) ** max(0.0, details.exponent))


def walk_book_for_shares(
    levels: Iterable[tuple[float, float]], shares: float, details: FeeDetails, *, buy: bool,
    slippage_bps: float = 0.0, require_full: bool = True,
) -> BookFill | None:
    target = max(0.0, finite(shares, 0.0))
    if target <= 0.0 or not details.verified:
        return None
    remaining, filled, raw, stressed, fees = target, 0.0, 0.0, 0.0, 0.0
    stress = max(0.0, finite(slippage_bps, 0.0)) / 1e4
    for price, size in sorted(_levels(list(levels), buy=buy), key=lambda z: z[0], reverse=not buy):
        take = min(remaining, size)
        if take <= 0.0:
            continue
        px = min(0.999999, price * (1.0 + stress)) if buy else max(0.000001, price * (1.0 - stress))
        raw += take * price
        stressed += take * px
        fees += fee_amount(take, px, details)
        filled += take
        remaining -= take
        if remaining <= 1e-10:
            break
    complete = remaining <= max(1e-9, 1e-8 * target)
    if filled <= 1e-12 or (require_full and not complete):
        return None
    return BookFill(
        target, filled, raw, stressed, fees, raw / filled, stressed / filled, abs(stressed - raw), complete
    )


def plan_bundle(
    live: dict[str, dict[str, Any]], tokens: Sequence[str], fees: dict[str, FeeDetails], shares: float,
    slippage_bps: float,
) -> BundlePlan | None:
    capital = raw = total_fees = slippage = 0.0
    fills: list[dict[str, Any]] = []
    for token in tokens:
        details, book = fees.get(token), live.get(token)
        if details is None or not details.verified or book is None:
            return None
        fill = walk_book_for_shares(book.get("asks", []), shares, details, buy=True,
                                    slippage_bps=slippage_bps, require_full=True)
        if fill is None:
            return None
        capital += fill.buy_cash
        raw += fill.raw_cash
        total_fees += fill.fee
        slippage += fill.slippage_cost
        fills.append({
            "token": token, "shares": fill.filled_shares, "price": fill.stressed_vwap,
            "raw_vwap": fill.raw_vwap, "fee": fill.fee, "capital_used": fill.buy_cash,
            "slippage_cost": fill.slippage_cost, "total_execution_cost": fill.fee + fill.slippage_cost,
        })
    return BundlePlan(capital, raw, total_fees, slippage, tuple(fills))


def max_bundle_for_cash(
    live: dict[str, dict[str, Any]], tokens: Sequence[str], fees: dict[str, FeeDetails], cash_cap: float,
    slippage_bps: float, *, minimum_shares: float, max_leg_cash: float = math.inf,
) -> tuple[float, BundlePlan] | None:
    cap = max(0.0, finite(cash_cap, 0.0))
    if cap <= 0.0 or not tokens:
        return None
    depth = min((sum(q for _, q in live.get(t, {}).get("asks", [])) for t in tokens), default=0.0)
    if depth + 1e-12 < minimum_shares:
        return None
    lo, hi, best = 0.0, depth, None
    for _ in range(42):
        mid = (lo + hi) / 2.0
        p = plan_bundle(live, tokens, fees, mid, slippage_bps) if mid > 1e-12 else None
        within_leg_cap = p is not None and all(float(x["capital_used"]) <= max_leg_cash + 1e-9 for x in p.fills)
        if p is not None and p.capital_used <= cap + 1e-9 and within_leg_cap:
            best, lo = (mid, p), mid
        else:
            hi = mid
    return best if best is not None and best[0] + 1e-12 >= minimum_shares else None


def _yes_token(raw: dict[str, Any]) -> str:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().lower() for x in parse_array(raw.get("outcomes"))]
    if not ids:
        return ""
    idx = next((i for i, outcome in enumerate(outcomes[:len(ids)]) if outcome == "yes"), 0)
    return ids[idx] if idx < len(ids) else ""


def _event_id(raw: dict[str, Any]) -> str:
    value = str(raw.get("eventId") or "").strip()
    events = raw.get("events")
    if not value and isinstance(events, list) and events and isinstance(events[0], dict):
        value = str(events[0].get("id") or "").strip()
    return value


def discover_event_ids(gamma: str, limit: int, min_liquidity: float, max_events: int) -> list[str]:
    """Discover all eligible events when limit=0; max_events is only a scan budget."""
    del max_events
    out: list[str] = []
    offset = 0
    for _ in range(200):
        if limit > 0 and offset >= limit:
            break
        page = 500 if limit <= 0 else min(500, max(1, limit - offset))
        query = urllib.parse.urlencode({
            "active": "true", "closed": "false", "limit": page, "offset": offset,
            "order": "liquidityNum", "ascending": "false",
        })
        payload = request_json(f"{gamma.rstrip('/')}/markets?{query}")
        batch = payload if isinstance(payload, list) else payload.get("markets", []) if isinstance(payload, dict) else []
        if not batch:
            break
        for raw in batch:
            if not isinstance(raw, dict) or not bool(raw.get("negRisk", False)):
                continue
            liq = max(0.0, finite(raw.get("liquidityNum"), finite(raw.get("liquidity"), 0.0)))
            eid = _event_id(raw)
            if liq >= min_liquidity and eid and eid not in out:
                out.append(eid)
        if len(batch) < page:
            break
        offset += len(batch)
    else:
        raise RuntimeError("Gamma event discovery pagination guard reached before exhaustion")
    return out


def rotating_window(values: Sequence[str], cursor: int, budget: int) -> tuple[list[str], int]:
    if not values:
        return [], 0
    start = max(0, int(cursor)) % len(values)
    count = min(len(values), max(1, int(budget)))
    selected = [values[(start + index) % len(values)] for index in range(count)]
    return selected, (start + count) % len(values)


def event_spec(gamma: str, event_id: str) -> list[dict[str, Any]] | None:
    event = request_json(f"{gamma.rstrip('/')}/events/{event_id}")
    if not isinstance(event, dict) or event.get("closed") or not event.get("negRisk") or event.get("negRiskAugmented"):
        return None
    rows = event.get("markets")
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    legs = []
    for raw in rows:
        if not isinstance(raw, dict):
            return None
        market_id, condition, token = str(raw.get("id") or ""), str(raw.get("conditionId") or ""), _yes_token(raw)
        if not market_id or not condition or not token:
            return None
        legs.append({"market_id": market_id, "condition_id": condition, "token": token, "raw": raw})
    return legs


def resolve_fees(legs: Sequence[dict[str, Any]], clob: str, stats: dict[str, Any], sources: Counter[str]) -> dict[str, FeeDetails] | None:
    fees: dict[str, FeeDetails] = {}
    for leg in legs:
        d = resolve_fee_details(leg["raw"], clob, leg["condition_id"], leg["token"])
        sources[d.source] += 1
        if not d.verified or d.rate < 0.0:
            stats["unverified_fee_rejections"] = int(stats.get("unverified_fee_rejections", 0)) + 1
            return None
        fees[leg["token"]] = d
    return fees


def record_freshness(live: dict[str, dict[str, Any]], tokens: Sequence[str], args: argparse.Namespace,
                     stats: dict[str, Any]) -> tuple[bool, str, int, int, int, int]:
    now_ms = int(time.time() * 1000)
    ok, reason, age, skew = local_book_freshness(
        live, tokens, now_ms=now_ms, max_leg_age_ms=args.max_leg_age_ms,
        max_cross_leg_skew_ms=args.max_cross_leg_skew_ms,
    )
    stats["freshness_checks"] = int(stats.get("freshness_checks", 0)) + 1
    stats["max_observed_leg_age_ms"] = max(int(stats.get("max_observed_leg_age_ms", 0)), age)
    stats["max_observed_cross_leg_skew_ms"] = max(int(stats.get("max_observed_cross_leg_skew_ms", 0)), skew)
    if not ok:
        stats["receive_rejections"] = int(stats.get("receive_rejections", 0)) + 1
        return False, reason, age, skew, 0, 0
    xok, xreason, xage, xskew = exchange_book_freshness(
        live, tokens, now_ms=now_ms, max_snapshot_age_ms=args.max_exchange_snapshot_age_ms,
        max_snapshot_skew_ms=args.max_exchange_snapshot_skew_ms,
    )
    stats["max_observed_exchange_snapshot_age_ms"] = max(int(stats.get("max_observed_exchange_snapshot_age_ms", 0)), xage)
    stats["max_observed_exchange_snapshot_skew_ms"] = max(int(stats.get("max_observed_exchange_snapshot_skew_ms", 0)), xskew)
    if not xok:
        stats["exchange_rejections"] = int(stats.get("exchange_rejections", 0)) + 1
        return False, xreason, age, skew, xage, xskew
    return True, "ok", age, skew, xage, xskew


def append_csv(path: Path, fields: Sequence[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({field: row.get(field, "") for field in fields})


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def normalize_state(state: dict[str, Any], start: float) -> dict[str, Any]:
    raw_open = state.get("open_bundles") if isinstance(state.get("open_bundles"), dict) else state.get("bundles", {})
    open_bundles: dict[str, dict[str, Any]] = {}
    for key, raw in raw_open.items() if isinstance(raw_open, dict) else []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["event_id"] = str(item.get("event_id") or key)
        item["capital_used"] = max(0.0, finite(item.get("capital_used"), finite(item.get("cost"), 0.0)))
        item["leg_count"] = int(finite(item.get("leg_count"), finite(item.get("legs"), 0.0)))
        open_bundles[str(key)] = item
    aborting: dict[str, dict[str, Any]] = {}
    for key, raw in state.get("aborting", {}).items() if isinstance(state.get("aborting"), dict) else []:
        if not isinstance(raw, dict):
            continue
        item, legs = dict(raw), []
        for old in item.get("legs", []) if isinstance(item.get("legs"), list) else []:
            if not isinstance(old, dict):
                continue
            leg = dict(old)
            leg["capital_used"] = max(0.0, finite(leg.get("capital_used"), finite(leg.get("cost"), 0.0)))
            if "raw_market" not in leg and isinstance(leg.get("market"), dict):
                leg["raw_market"] = leg["market"]
            leg["total_execution_cost"] = max(0.0, finite(
                leg.get("total_execution_cost"), finite(leg.get("fee"), 0.0) + finite(leg.get("slippage_cost"), 0.0)
            ))
            legs.append(leg)
        item["event_id"] = str(item.get("event_id") or key)
        item["legs"] = legs
        item["capital_used"] = sum(max(0.0, finite(x.get("capital_used"), 0.0)) for x in legs)
        aborting[str(key)] = item
    return {
        "cash": max(0.0, finite(state.get("cash"), start)),
        "peak": max(start, finite(state.get("peak"), start)),
        "killed": bool(state.get("killed", False)),
        "open_bundles": open_bundles,
        "aborting": aborting,
        "realized_pnl_total": finite(state.get("realized_pnl_total"), finite(state.get("realized_pnl"), 0.0)),
        "scan_cursor": max(0, int(finite(state.get("scan_cursor"), 0.0))),
    }


def executable_abort_mark(clob: str, aborting: dict[str, Any], slippage_bps: float,
                          stats: dict[str, Any],
                          book_source: Callable[[Sequence[str]], dict[str, dict[str, Any]]] | None = None) -> float:
    """Full-depth, fee-net mark; unmarkable residual legs are worth zero."""
    value = 0.0
    for bundle in aborting.values():
        for leg in bundle.get("legs", []) if isinstance(bundle, dict) else []:
            token, shares = str(leg.get("token") or ""), max(0.0, finite(leg.get("shares"), 0.0))
            raw = leg.get("raw_market") if isinstance(leg, dict) else None
            if not token or shares <= 0.0 or not isinstance(raw, dict):
                continue
            d = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
            if not d.verified:
                stats["unverified_fee_rejections"] = int(stats.get("unverified_fee_rejections", 0)) + 1
                continue
            try:
                book = (book_source([token]) if book_source else fetch_books(clob, [token], stats)).get(token)
            except Exception:
                book = None
            fill = walk_book_for_shares(book.get("bids", []), shares, d, buy=False,
                                        slippage_bps=slippage_bps, require_full=True) if book else None
            if fill is not None and fill.complete:
                value += max(0.0, fill.sell_cash)
    return value


def unwind_bundle(clob: str, bundle_id: str, event_id: str, legs: list[dict[str, Any]],
                  fees: dict[str, FeeDetails], slippage_bps: float, run_dir: Path,
                  stats: dict[str, Any],
                  book_source: Callable[[Sequence[str]], dict[str, dict[str, Any]]] | None = None,
                  ) -> tuple[list[dict[str, Any]], float, float]:
    residual, cash, realized = [], 0.0, 0.0
    for index, leg in enumerate(legs):
        token = str(leg.get("token") or "")
        shares, basis = max(0.0, finite(leg.get("shares"), 0.0)), max(0.0, finite(leg.get("capital_used"), 0.0))
        try:
            book = (book_source([token]) if book_source else fetch_books(clob, [token], stats)).get(token)
        except Exception:
            book = None
        d = fees.get(token)
        fill = walk_book_for_shares(book.get("bids", []), shares, d, buy=False,
                                    slippage_bps=slippage_bps, require_full=False) if book and d and d.verified else None
        if fill is None or fill.filled_shares <= 1e-12:
            residual.append(dict(leg))
            continue
        fraction = min(1.0, fill.filled_shares / max(shares, 1e-12))
        allocated_basis = basis * fraction
        proceeds, pnl = fill.sell_cash, fill.sell_cash - allocated_basis
        entry_cost = max(0.0, finite(leg.get("total_execution_cost"), 0.0)) * fraction
        row_cost = entry_cost + fill.fee + fill.slippage_cost
        cash += proceeds
        realized += pnl
        append_csv(run_dir / "fills.csv", FILL_FIELDS, {
            "timestamp": int(time.time()), "bundle_id": bundle_id, "strategy": "HARD_ARB", "event_id": event_id,
            "action": "UNWIND", "token": token, "leg_index": index + 1, "leg_count": len(legs),
            "shares": fill.filled_shares, "price": fill.stressed_vwap, "capital_used": allocated_basis,
            "fee": fill.fee, "total_fees": fill.fee, "slippage_cost": fill.slippage_cost,
            "total_execution_cost": row_cost, "net_pnl": pnl, "detail": "forced_partial_bundle_unwind",
        })
        remaining = max(0.0, shares - fill.filled_shares)
        if remaining > 1e-9:
            left = dict(leg)
            left["shares"] = remaining
            left["capital_used"] = max(0.0, basis - allocated_basis)
            left["total_execution_cost"] = max(0.0, finite(leg.get("total_execution_cost"), 0.0) - entry_cost)
            residual.append(left)
    return residual, cash, realized


def _stats() -> dict[str, Any]:
    return {key: 0 for key in (
        "book_calls", "book_batches", "freshness_checks", "receive_rejections", "exchange_rejections",
        "unverified_fee_rejections", "max_observed_leg_age_ms", "max_observed_cross_leg_skew_ms",
        "max_observed_exchange_snapshot_age_ms", "max_observed_exchange_snapshot_skew_ms",
        "max_observed_batch_receive_span_ms", "shared_state_reads",
        "shared_state_rejections", "rest_execution_book_reads",
    )}


def self_test() -> int:
    live = {
        "a": {"received_ms": 10_000, "exchange_ts_ms": 9_990, "asks": [(0.40, 20)], "bids": [(0.39, 20)], "min_order": 1},
        "b": {"received_ms": 10_040, "exchange_ts_ms": 10_010, "asks": [(0.50, 20)], "bids": [(0.49, 20)], "min_order": 1},
    }
    assert local_book_freshness(live, ["a", "b"], now_ms=10_100, max_leg_age_ms=200, max_cross_leg_skew_ms=100) == (True, "ok", 100, 40)
    assert exchange_book_freshness(live, ["a", "b"], now_ms=10_100, max_snapshot_age_ms=200, max_snapshot_skew_ms=100) == (True, "ok", 110, 20)
    free = FeeDetails(0.0, 1.0, True, True, "test:free")
    plan = plan_bundle(live, ["a", "b"], {"a": free, "b": free}, 10.0, 0.0)
    assert plan is not None and abs(plan.capital_used - 9.0) < 1e-9
    assert plan_bundle(live, ["a", "b"], {"a": free, "b": free}, 21.0, 0.0) is None
    migrated = normalize_state({
        "cash": 90, "peak": 100, "realized_pnl": 1.25,
        "bundles": {"e1": {"shares": 10, "cost": 9, "legs": 2}},
        "aborting": {"e2": {"legs": [{"token": "a", "shares": 2, "cost": .8, "market": {"conditionId": "c"}}]}},
    }, 100)
    assert migrated["open_bundles"]["e1"]["capital_used"] == 9
    assert migrated["aborting"]["e2"]["legs"][0]["raw_market"]["conditionId"] == "c"
    assert migrated["realized_pnl_total"] == 1.25
    assert normalize_timestamp_ms(1_787_700_000) == 1_787_700_000_000
    print("v7_hard_arb_guard_self_test=ok")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="V7 native PAPER structural complete-set execution")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--markets", type=int, default=1000)
    p.add_argument("--min-liquidity", type=float, default=2.0)
    p.add_argument("--max-events", type=int, default=80)
    p.add_argument("--min-edge", type=float, default=0.00005)
    p.add_argument("--max-trade-usd", type=float, default=1e100)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--leg-latency-ms", type=int, default=100)
    p.add_argument("--max-leg-age-ms", type=int, default=2000)
    p.add_argument("--max-cross-leg-skew-ms", type=int, default=1000)
    p.add_argument("--max-exchange-snapshot-age-ms", type=int, default=5000)
    p.add_argument("--max-exchange-snapshot-skew-ms", type=int, default=1000)
    p.add_argument("--shared-state", type=Path)
    p.add_argument("--model-sha", default="")
    p.add_argument("--max-shared-publish-age-ms", type=int, default=2500)
    return p.parse_args(argv)


def _candidate(run_dir: Path, **row: Any) -> None:
    append_csv(run_dir / "candidates.csv", CANDIDATE_FIELDS, row)


def run(args: argparse.Namespace) -> int:
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if not cfg.get("paper_only", True):
        raise RuntimeError("v7_hard_arb_requires_paper_only_config")
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    start = max(0.0, finite(cfg.get("starting_capital"), 0.0))
    maxdd = max(0.0, min(1.0, finite(cfg.get("max_drawdown"), .15)))
    maxgross = max(0.0, finite(cfg.get("max_gross_fraction"), 1.0))
    maxevent = max(0.0, finite(cfg.get("max_event_fraction"), 1.0))
    maxmarket = max(0.0, finite(cfg.get("max_market_fraction"), 1.0))
    maxtrade = max(0.0, finite(cfg.get("max_trade_fraction"), 1.0))
    args.run_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.run_dir / "state.json"
    try:
        raw_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_state = {}
    state = normalize_state(raw_state if isinstance(raw_state, dict) else {}, start)
    cash, peak, killed = state["cash"], state["peak"], state["killed"]
    openb, aborting = state["open_bundles"], state["aborting"]
    realized_total, realized_tick = state["realized_pnl_total"], 0.0
    scan_cursor = state["scan_cursor"]
    failures, stats, sources = [], _stats(), Counter()
    now = int(time.time())
    if args.shared_state is not None and (
        len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha)
    ):
        raise RuntimeError("shared_state_requires_exact_model_sha")
    book_source = lambda tokens: execution_books(args, clob, tokens, stats)

    for bid, bundle in list(openb.items()):
        try:
            eid = str(bundle.get("event_id") or "")
            event = request_json(f"{gamma.rstrip('/')}/events/{eid}")
            if isinstance(event, dict) and event.get("closed"):
                shares = max(0.0, finite(bundle.get("shares"), 0.0))
                capital = max(0.0, finite(bundle.get("capital_used"), 0.0))
                pnl = shares - capital
                cash += shares
                realized_tick += pnl
                append_csv(args.run_dir / "fills.csv", FILL_FIELDS, {
                    "timestamp": now, "bundle_id": bid, "strategy": "HARD_ARB", "event_id": eid,
                    "action": "CLOSE_STRUCTURAL_PAYOUT", "leg_count": bundle.get("leg_count", 0), "shares": shares,
                    "capital_used": capital, "total_fees": bundle.get("total_fees", 0.0),
                    "slippage_cost": bundle.get("slippage_cost", 0.0),
                    "total_execution_cost": bundle.get("total_execution_cost", 0.0),
                    "net_edge": bundle.get("net_edge", 0.0), "net_pnl": pnl,
                    "detail": "neg_risk_complete_set_terminal_payout",
                })
                del openb[bid]
        except Exception as exc:
            if len(failures) < 20:
                failures.append(f"settle:{bid}:{type(exc).__name__}:{exc}")

    for bid, bundle in list(aborting.items()):
        legs = bundle.get("legs", []) if isinstance(bundle.get("legs"), list) else []
        fee_map: dict[str, FeeDetails] = {}
        for leg in legs:
            raw = leg.get("raw_market") if isinstance(leg, dict) else None
            token = str(leg.get("token") or "") if isinstance(leg, dict) else ""
            if isinstance(raw, dict) and token:
                d = resolve_fee_details(raw, clob, str(raw.get("conditionId") or ""), token)
                sources[d.source] += 1
                if d.verified:
                    fee_map[token] = d
        residual, received, pnl = unwind_bundle(
            clob, bid, str(bundle.get("event_id") or ""), legs, fee_map, args.slippage_bps,
            args.run_dir, stats, book_source
        )
        cash += received
        realized_tick += pnl
        if residual:
            bundle["legs"] = residual
            bundle["capital_used"] = sum(max(0.0, finite(x.get("capital_used"), 0.0)) for x in residual)
        else:
            del aborting[bid]

    locked = sum(max(0.0, finite(x.get("capital_used"), 0.0)) for x in openb.values())
    abort_value = executable_abort_mark(
        clob, aborting, args.slippage_bps, stats, book_source) if aborting else 0.0
    equity = cash + locked + abort_value
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = bool(killed) or drawdown >= maxdd
    scanned = positive = entered = seq_aborts = candidate_rows = 0
    best_edge = 0.0

    try:
        discovered_event_ids = [] if killed or aborting else discover_event_ids(gamma, args.markets, args.min_liquidity, args.max_events)
        event_ids, scan_cursor = rotating_window(discovered_event_ids, scan_cursor, args.max_events)
    except Exception as exc:
        discovered_event_ids = []
        event_ids = []
        failures.append(f"discover:{type(exc).__name__}:{exc}")

    for eid in event_ids:
        if any(str(x.get("event_id") or "") == eid for x in openb.values()):
            continue
        try:
            legs = event_spec(gamma, eid)
            if not legs:
                continue
            tokens = [x["token"] for x in legs]
            fees = resolve_fees(legs, clob, stats, sources)
            if fees is None:
                continue
            live = execution_books(args, clob, tokens, stats)
            scanned += 1
            fresh = record_freshness(live, tokens, args, stats)
            ok, reason, age, skew, xage, xskew = fresh
            minimum = max([max(0.0, finite(live.get(t, {}).get("min_order"), 0.0)) for t in tokens] + [1.0])
            locked = sum(max(0.0, finite(x.get("capital_used"), 0.0)) for x in openb.values())
            equity = cash + locked + (executable_abort_mark(
                clob, aborting, args.slippage_bps, stats, book_source) if aborting else 0.0)
            cap = min(
                max(0.0, args.max_trade_usd), cash,
                max(0.0, maxtrade * equity), max(0.0, maxevent * equity),
                max(0.0, maxgross * equity - locked),
            )
            sized = max_bundle_for_cash(
                live, tokens, fees, cap, args.slippage_bps, minimum_shares=minimum,
                max_leg_cash=max(0.0, maxmarket * equity),
            ) if ok else None
            bundle_id = f"HARD-{eid}-{int(time.time() * 1000)}"
            if sized is None:
                candidate_rows += 1
                _candidate(args.run_dir, timestamp=int(time.time()), bundle_id=bundle_id, strategy="HARD_ARB",
                           action="REJECT", event_id=eid, legs=len(tokens), max_leg_age_ms=age,
                           cross_leg_skew_ms=skew, max_exchange_snapshot_age_ms=xage,
                           exchange_snapshot_skew_ms=xskew, fee_sources=json.dumps(dict(sources), sort_keys=True),
                           executable=0, reason=reason if not ok else "insufficient_depth_or_risk_capacity")
                continue
            shares, plan = sized
            raw_edge, net_edge = 1.0 - plan.raw_cash / shares, 1.0 - plan.capital_used / shares
            best_edge = max(best_edge, net_edge)
            executable = net_edge > args.min_edge
            candidate_rows += 1
            _candidate(args.run_dir, timestamp=int(time.time()), bundle_id=bundle_id, strategy="HARD_ARB",
                       action="SUBMIT" if executable else "REJECT", expected_edge=net_edge if executable else "",
                       event_id=eid, legs=len(tokens), shares=shares, raw_edge=raw_edge, net_edge=net_edge,
                       capital_used=plan.capital_used, total_fees=plan.total_fees, slippage_cost=plan.slippage_cost,
                       total_execution_cost=plan.execution_cost, max_leg_age_ms=age, cross_leg_skew_ms=skew,
                       max_exchange_snapshot_age_ms=xage, exchange_snapshot_skew_ms=xskew,
                       fee_sources=json.dumps(dict(sources), sort_keys=True), executable=int(executable),
                       reason="positive_post_cost_edge" if executable else "net_edge_gate")
            if not executable:
                continue
            positive += 1

            depth = {t: sum(q for _, q in live.get(t, {}).get("asks", [])) for t in tokens}
            order = sorted(tokens, key=lambda t: (depth.get(t, math.inf), t))
            by_token = {x["token"]: x for x in legs}
            execution_cost = raw_execution = total_fees = total_slippage = 0.0
            filled: list[dict[str, Any]] = []
            fail = ""
            for i, token in enumerate(order):
                remaining = order[i:]
                refreshed = execution_books(args, clob, remaining, stats)
                rfresh = record_freshness(refreshed, remaining, args, stats)
                if not rfresh[0]:
                    fail = f"freshness:{rfresh[1]}"
                    break
                rplan = plan_bundle(refreshed, remaining, fees, shares, args.slippage_bps)
                if rplan is None:
                    fail = "remaining_depth"
                    break
                guarantee = 1.0 - (execution_cost + rplan.capital_used) / shares
                if guarantee <= args.min_edge:
                    fail = "edge_revalidation"
                    break
                current = next((x for x in rplan.fills if x["token"] == token), None)
                if current is None or float(current["capital_used"]) > cash + 1e-9:
                    fail = "capital_or_current_leg"
                    break
                leg_cost = float(current["capital_used"])
                cash -= leg_cost
                execution_cost += leg_cost
                raw_execution += float(current["raw_vwap"]) * shares
                total_fees += float(current["fee"])
                total_slippage += float(current["slippage_cost"])
                leg = {**current, "raw_market": by_token[token]["raw"]}
                filled.append(leg)
                append_csv(args.run_dir / "leg_fills.csv", FILL_FIELDS, {
                    "timestamp": int(time.time()), "bundle_id": bundle_id, "strategy": "HARD_ARB", "event_id": eid,
                    "action": "BUY_LEG_FOK", "token": token, "leg_index": i + 1, "leg_count": len(order),
                    "shares": shares, "price": current["price"], "capital_used": leg_cost, "fee": current["fee"],
                    "total_fees": current["fee"], "slippage_cost": current["slippage_cost"],
                    "total_execution_cost": current["total_execution_cost"], "net_edge": guarantee,
                    "detail": f"leg={i + 1}/{len(order)} sequential_revalidation",
                })
                if i + 1 < len(order) and args.leg_latency_ms > 0:
                    time.sleep(args.leg_latency_ms / 1000.0)

            final_edge = 1.0 - execution_cost / shares if shares > 0 else -math.inf
            if fail or final_edge <= args.min_edge:
                seq_aborts += 1
                residual, received, pnl = unwind_bundle(
                    clob, bundle_id, eid, filled, fees, args.slippage_bps,
                    args.run_dir, stats, book_source
                )
                cash += received
                realized_tick += pnl
                if residual:
                    aborting[bundle_id] = {
                        "event_id": eid, "legs": residual,
                        "capital_used": sum(max(0.0, finite(x.get("capital_used"), 0.0)) for x in residual),
                        "reason": fail or "post_execution_edge", "opened_ts": now,
                    }
                continue

            openb[bundle_id] = {
                "event_id": eid, "shares": shares, "capital_used": execution_cost,
                "raw_edge": 1.0 - raw_execution / shares, "net_edge": final_edge,
                "total_fees": total_fees, "slippage_cost": total_slippage,
                "total_execution_cost": total_fees + total_slippage, "leg_count": len(order), "opened_ts": now,
            }
            entered += 1
            append_csv(args.run_dir / "fills.csv", FILL_FIELDS, {
                "timestamp": int(time.time()), "bundle_id": bundle_id, "strategy": "HARD_ARB", "event_id": eid,
                "action": "BUY_COMPLETE_YES_SET_SEQUENTIAL", "leg_count": len(order), "shares": shares,
                "capital_used": execution_cost, "net_edge": final_edge,
                "detail": "neg_risk_complete_set;paper_only;all_legs_fok",
            })
        except Exception as exc:
            if len(failures) < 20:
                failures.append(f"event:{eid}:{type(exc).__name__}:{exc}")

    realized_total += realized_tick
    locked = sum(max(0.0, finite(x.get("capital_used"), 0.0)) for x in openb.values())
    abort_value = executable_abort_mark(
        clob, aborting, args.slippage_bps, stats, book_source) if aborting else 0.0
    equity = cash + locked + abort_value
    peak = max(peak, equity)
    drawdown = max(0.0, 1.0 - equity / peak) if peak else 0.0
    killed = bool(killed) or drawdown >= maxdd
    status = {
        "schema": "polymarket_v7_hard_arb_status_v2", "timestamp": int(time.time()), "paper_only": True,
        "authenticated_execution": False, "cash": cash, "equity_cost_basis": equity, "peak": peak,
        "drawdown": drawdown, "killed": killed, "open_bundles": openb, "aborting": aborting,
        "aborting_bundles": len(aborting), "realized_pnl_last_tick": realized_tick,
        "realized_pnl_total": realized_total,
        "locked_expected_profit": sum(max(0.0, finite(x.get("shares"), 0.0) - finite(x.get("capital_used"), 0.0)) for x in openb.values()),
        "scanned_events": scanned, "positive_candidates": positive, "candidate_rows": candidate_rows,
        "discovered_events": len(discovered_event_ids), "scan_budget_events": max(1, args.max_events),
        "scan_cursor": scan_cursor,
        "full_cycle_fraction": (len(event_ids) / len(discovered_event_ids)) if discovered_event_ids else 0.0,
        "discovery_exhaustive": args.markets <= 0,
        "entered": entered, "sequential_aborts": seq_aborts, "best_edge": best_edge,
        "fee_sources_last_tick": dict(sources), "failures": failures,
        "atomic_snapshot_assumption": args.shared_state is not None,
        "market_state_source": "SHARED_CPP_WEBSOCKET" if args.shared_state is not None else "REST_COMPATIBILITY",
        "per_token_receive_timestamps": True, "exchange_snapshot_timestamps": True, "multi_level_depth": True,
        "verified_fees_required": True, "sequential_leg_revalidation": True, "unwind_on_leg_failure": True,
        "aborting_mark": "full_depth_executable_liquidation_net_exit_fee_fail_closed",
        "leg_latency_ms": max(0, args.leg_latency_ms), "max_leg_age_ms": max(0, args.max_leg_age_ms),
        "max_cross_leg_skew_ms": max(0, args.max_cross_leg_skew_ms),
        "max_exchange_snapshot_age_ms": max(0, args.max_exchange_snapshot_age_ms),
        "max_exchange_snapshot_skew_ms": max(0, args.max_exchange_snapshot_skew_ms),
        "freshness_guard": stats,
        "execution_model": "v7_native_depth_fee_freshness_sequential_fok_unwind",
    }
    atomic_json(state_path, status)
    atomic_json(args.run_dir / "status.json", status)
    print(f"v7_hard_arb scanned={scanned} positive={positive} entered={entered} sequential_aborts={seq_aborts} "
          f"aborting={len(aborting)} best_edge={best_edge:.8f} realized={realized_tick:.6f}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv) if argv is not None else None
    if (values == ["self-test"]) or (values is None and len(os.sys.argv) > 1 and os.sys.argv[1] == "self-test"):
        return self_test()
    return run(parse_args(values))


if __name__ == "__main__":
    raise SystemExit(main())
