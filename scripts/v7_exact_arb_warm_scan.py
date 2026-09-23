#!/usr/bin/env python3
"""Bounded exchange-wide warm screen for compiled exact-arbitrage relations.

This process is discovery/ranking only.  Public /books batches are not atomic
and therefore can NEVER create an actionable opportunity.  Apparent edges are
used solely to select relations for causal hot observation.

No credentials, signing, OMS, ledger or execution dependency exists here.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import time
from typing import Any

from v7_clob_public_batch import fetch_books, full_book
from v7_unified_exact_arb_graph import (
    SAFETY, GraphError, fee_per_share, frac, fstr, load, round_fee, validate_graph,
)

SCHEMA = "polymarket_v7_exact_arb_warm_screen_status_v1"
HOTSET_SCHEMA = "polymarket_v7_exact_arb_hotset_v1"
EVIDENCE = "NONATOMIC_PUBLIC_REST_SCREEN_ONLY"


def atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def quantiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    def at(p: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, int(math.floor((len(ordered) - 1) * p))))]
    return {
        "min": ordered[0],
        "p0_1": at(.001),
        "p1": at(.01),
        "p5": at(.05),
        "p10": at(.10),
        "p25": at(.25),
        "p50": at(.50),
        "p90": at(.90),
        "p99": at(.99),
    }


def _levels(book: dict[str, Any], side: str) -> list[tuple[Fraction, Fraction]]:
    rows = book.get("asks" if side == "BUY" else "bids")
    if not isinstance(rows, list) or not rows:
        return []
    result = []
    for price, size in rows:
        try:
            p, q = frac(price), frac(size)
        except (GraphError, TypeError, ValueError):
            return []
        if not 0 < p < 1 or q <= 0:
            return []
        result.append((p, q))
    result.sort(key=lambda row: row[0], reverse=side == "SELL")
    return result


def _fee_terms(leg: dict[str, Any]) -> tuple[Fraction, int, Fraction | None, str] | None:
    try:
        rate = frac(leg["fee_rate"])
        exponent = frac(leg["fee_exponent"])
        increment = frac(leg["fee_rounding_increment"]) if leg.get("fee_rounding_increment") is not None else None
        mode = str(leg.get("fee_rounding_mode") or "EXACT")
    except (KeyError, TypeError, ValueError, GraphError):
        return None
    if not 0 <= rate <= 1 or exponent < 0 or exponent.denominator != 1:
        return None
    try:
        round_fee(Fraction(0), increment, mode)
    except GraphError:
        return None
    return rate, int(exponent), increment, mode


def screen_relation(relation: dict[str, Any], books: dict[str, dict[str, Any]], maximum_units: Fraction) -> dict[str, Any]:
    """Non-causal economic screen.

    It intentionally omits freshness, lineage and inter-leg arrival claims.
    Any apparent edge requires a later causal observer confirmation.
    """
    if relation.get("enabled") is not True or relation.get("relation_type") != "CONSTANT_PAYOUT_EQUALITY":
        return {"screened": False, "reason": "nonactionable_relation"}
    legs = relation.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= 16:
        return {"screened": False, "reason": "invalid_legs"}
    try:
        guarantee = frac(relation["guaranteed_payout"])
        reserve = frac(relation.get("reserve_per_unit", 0))
    except (KeyError, GraphError):
        return {"screened": False, "reason": "invalid_economics"}
    prepared = []
    for leg in legs:
        token = str(leg.get("token_id") or "")
        book = books.get(token)
        terms = _fee_terms(leg)
        try:
            coefficient = frac(leg["coefficient"])
            minimum = frac(leg.get("minimum_order", 0))
        except (KeyError, GraphError):
            return {"screened": False, "reason": "invalid_leg"}
        if not token or book is None or terms is None or coefficient <= 0:
            return {"screened": False, "reason": "missing_book_or_fee"}
        prepared.append((leg, book, coefficient, minimum, terms))
    out: dict[str, Any] = {
        "screened": True,
        "relation_id": relation.get("relation_id"),
        "relation_family": relation.get("relation_family"),
        "evidence_quality": EVIDENCE,
        "requires_causal_confirmation": True,
        "actionable": False,
        "directions": {},
    }
    allowed = set(relation.get("directions") or ["BUY_BASKET"])
    for side in ("BUY", "SELL"):
        if side == "BUY":
            permitted = bool(allowed.intersection({"BUY_BASKET", "BUY_COMPLETE_SET"}))
        else:
            permitted = bool(allowed.intersection({"SELL_INVENTORY_BASKET", "SELL_COMPLETE_SET"}))
        if not permitted:
            continue
        depth = [_levels(book, side) for _, book, _, _, _ in prepared]
        if not all(depth):
            out["directions"][side] = {"screened": False, "reason": "missing_depth"}
            continue
        raw_unit = Fraction(0)
        fee_unit = Fraction(0)
        tick_values = []
        for (leg, _, coefficient, _, (rate, exponent, increment, mode)), levels in zip(prepared, depth):
            price = levels[0][0]
            raw_unit += coefficient * price
            fee_unit += round_fee(coefficient * fee_per_share(price, rate, exponent), increment, mode)
            try:
                tick = frac(leg.get("tick_size"))
                if tick > 0:
                    tick_values.append(tick)
            except (GraphError, TypeError):
                pass
        raw_distance = raw_unit - guarantee if side == "BUY" else guarantee - raw_unit
        after_fee = raw_distance + fee_unit
        after_reserve = after_fee + reserve
        minimum = max((m / c for _, _, c, m, _ in prepared), default=Fraction(0))
        capacity = min(
            [maximum_units]
            + [
                sum((size for _, size in levels), Fraction(0)) / prepared[i][2]
                for i, levels in enumerate(depth)
            ]
        )
        # Synchronized depth breakpoint walk.  This is still only a screen
        # because the REST legs are not an atomic causal observation.
        indices = [0] * len(prepared)
        remaining = [levels[0][1] for levels in depth]
        quantity = Fraction(0)
        pnl = Fraction(0)
        fees = Fraction(0)
        notionals = Fraction(0)
        denominator = 1
        for _, _, coefficient, _, _ in prepared:
            denominator = denominator * coefficient.denominator // math.gcd(denominator, coefficient.denominator)
        quantum = Fraction(denominator, 1_000_000)
        while quantity < capacity:
            for i, (_, _, coefficient, _, _) in enumerate(prepared):
                while indices[i] < len(depth[i]) and remaining[i] < coefficient * quantum:
                    indices[i] += 1
                    if indices[i] < len(depth[i]):
                        remaining[i] = depth[i][indices[i]][1]
            if any(indices[i] >= len(depth[i]) for i in range(len(prepared))):
                break
            step = min([capacity - quantity] + [
                remaining[i] / prepared[i][2] for i in range(len(prepared))
            ])
            step = (step // quantum) * quantum
            if step <= 0:
                break
            raw = Fraction(0)
            fee = Fraction(0)
            for i, (_, _, coefficient, _, (rate, exponent, increment, mode)) in enumerate(prepared):
                price = depth[i][indices[i]][0]
                shares = step * coefficient
                raw += shares * price
                fee += round_fee(shares * fee_per_share(price, rate, exponent), increment, mode)
            edge = (step * guarantee - raw if side == "BUY" else raw - step * guarantee) - fee - step * reserve
            if edge <= 0:
                break
            quantity += step
            pnl += edge
            notionals += raw
            fees += fee
            for i, (_, _, coefficient, _, _) in enumerate(prepared):
                remaining[i] -= step * coefficient
                if remaining[i] == 0:
                    indices[i] += 1
                    if indices[i] < len(depth[i]):
                        remaining[i] = depth[i][indices[i]][1]
        common_tick = min(tick_values) if tick_values else None
        row = {
            "screened": True,
            "distance_to_raw_arbitrage": fstr(raw_distance),
            "distance_to_after_fee_arbitrage": fstr(after_fee),
            "distance_to_after_reserve_arbitrage": fstr(after_reserve),
            "quantity_apparent": fstr(quantity),
            "net_locked_pnl_apparent": fstr(pnl),
            "fee_drag_apparent": fstr(fees),
            "notional_apparent": fstr(notionals),
            "minimum_relation_units": fstr(minimum),
            "inventory_required": side == "SELL",
            "requires_causal_confirmation": True,
            "actionable": False,
        }
        if common_tick is not None:
            row["distance_after_reserve_ticks"] = float(after_reserve / common_tick)
        out["directions"][side] = row
    return out


def relation_order(relations: list[dict[str, Any]], cursor: int) -> list[int]:
    priority = []
    ordinary = []
    for i, relation in enumerate(relations):
        if relation.get("enabled") is not True:
            continue
        if relation.get("relation_family") == "SAME_MARKET_BINARY_COMPLETE_SET":
            ordinary.append(i)
        else:
            priority.append(i)
    if ordinary:
        cursor %= len(ordinary)
        ordinary = ordinary[cursor:] + ordinary[:cursor]
    return priority + ordinary


def select_relations(relations: list[dict[str, Any]], cursor: int, max_tokens: int) -> tuple[list[int], list[str]]:
    selected = []
    tokens: set[str] = set()
    for index in relation_order(relations, cursor):
        relation_tokens = {str(leg.get("token_id") or "") for leg in relations[index].get("legs") or []}
        relation_tokens.discard("")
        if not relation_tokens:
            continue
        if selected and len(tokens | relation_tokens) > max_tokens:
            continue
        if len(relation_tokens) > max_tokens:
            continue
        selected.append(index)
        tokens.update(relation_tokens)
        if len(tokens) >= max_tokens:
            break
    return selected, sorted(tokens)


def scan_once(args: argparse.Namespace, cursor: int) -> tuple[dict[str, Any], dict[str, Any], int]:
    graph = load(args.graph)
    validate_graph(graph, args.model_sha)
    relations = graph.get("relations") or []
    selected, tokens = select_relations(relations, cursor, args.max_tokens_per_cycle)
    started = time.monotonic_ns()
    raw = fetch_books(
        args.clob_url, tokens, args.timeout_seconds,
        chunk_size=args.chunk_size,
        user_agent="polymarket-v7-exact-arb-warm-screen",
    )
    books = {token: full_book(raw.get(token)) for token in tokens}
    missing = sum(book is None for book in books.values())
    family = Counter()
    rejected = Counter()
    distances: dict[str, list[float]] = defaultdict(list)
    ranked = []
    raw_positive = after_fee_positive = after_reserve_positive = 0
    screened = 0
    for index in selected:
        relation = relations[index]
        result = screen_relation(relation, books, frac(args.maximum_relation_units))
        family[str(relation.get("relation_family") or "UNKNOWN")] += 1
        if not result.get("screened"):
            rejected[str(result.get("reason") or "unknown")] += 1
            continue
        screened += 1
        best = None
        for side, row in result["directions"].items():
            if not row.get("screened"):
                continue
            try:
                d0 = float(frac(row["distance_to_raw_arbitrage"]))
                d1 = float(frac(row["distance_to_after_fee_arbitrage"]))
                d2 = float(frac(row["distance_to_after_reserve_arbitrage"]))
            except (GraphError, ValueError, TypeError):
                continue
            distances["raw"].append(d0)
            distances["after_fee"].append(d1)
            distances["after_reserve"].append(d2)
            raw_positive += d0 < 0
            after_fee_positive += d1 < 0
            after_reserve_positive += d2 < 0
            score = d2
            candidate = {
                "relation_id": relation.get("relation_id"),
                "relation_family": relation.get("relation_family"),
                "direction": side,
                "distance_to_after_reserve_arbitrage": row["distance_to_after_reserve_arbitrage"],
                "distance_after_reserve_ticks": row.get("distance_after_reserve_ticks"),
                "quantity_apparent": row["quantity_apparent"],
                "net_locked_pnl_apparent": row["net_locked_pnl_apparent"],
                "requires_causal_confirmation": True,
                "actionable": False,
                "tokens": sorted({str(leg.get("token_id")) for leg in relation.get("legs") or []}),
            }
            if best is None or score < best[0]:
                best = (score, candidate)
        if best is not None:
            ranked.append(best)
    ranked.sort(key=lambda row: row[0])
    hot_rows = [row for _, row in ranked[:args.hotset_relations]]
    now = time.time_ns() // 1_000_000
    hot_tokens = sorted({token for row in hot_rows for token in row["tokens"]})
    hotset = {
        "schema": HOTSET_SCHEMA,
        **SAFETY,
        "execution_authority": False,
        "model_sha": args.model_sha,
        "graph_generation": graph["graph_generation"],
        "timestamp_ms": now,
        "evidence_quality": EVIDENCE,
        "actionable": False,
        "selection_purpose": "CAUSAL_HOT_OBSERVATION_PRIORITY_ONLY",
        "relations": hot_rows,
        "tokens": hot_tokens,
    }
    status = {
        "schema": SCHEMA,
        **SAFETY,
        "execution_authority": False,
        "model_sha": args.model_sha,
        "state": "SCREENING",
        "timestamp_ms": now,
        "graph_generation": graph["graph_generation"],
        "evidence_quality": EVIDENCE,
        "actionable_candidates": 0,
        "relations_selected": len(selected),
        "relations_screened": screened,
        "relations_by_family": dict(family),
        "tokens_requested": len(tokens),
        "books_missing": missing,
        "raw_positive_screen_only": raw_positive,
        "after_fee_positive_screen_only": after_fee_positive,
        "after_reserve_positive_screen_only": after_reserve_positive,
        "near_arbitrage": {name: quantiles(values) for name, values in distances.items()},
        "rejection_reasons": dict(rejected),
        "hotset_relations": len(hot_rows),
        "hotset_tokens": len(hot_tokens),
        "scan_duration_ms": (time.monotonic_ns() - started) / 1_000_000.0,
    }
    next_cursor = cursor + max(1, sum(
        1 for i in selected if relations[i].get("relation_family") == "SAME_MARKET_BINARY_COMPLETE_SET"
    ))
    return status, hotset, next_cursor


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--hotset", type=Path, required=True)
    ap.add_argument("--model-sha", required=True)
    ap.add_argument("--clob-url", default="https://clob.polymarket.com")
    ap.add_argument("--timeout-seconds", type=float, default=2.0)
    ap.add_argument("--interval-seconds", type=float, default=2.0)
    ap.add_argument("--chunk-size", type=int, default=50)
    ap.add_argument("--max-tokens-per-cycle", type=int, default=500)
    ap.add_argument("--hotset-relations", type=int, default=64)
    ap.add_argument("--maximum-relation-units", default="1000")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("invalid model sha")
    if not (.1 <= args.timeout_seconds <= 10 and .25 <= args.interval_seconds <= 60
            and 1 <= args.chunk_size <= 50 and 2 <= args.max_tokens_per_cycle <= 5000
            and 1 <= args.hotset_relations <= 1000 and frac(args.maximum_relation_units) > 0):
        raise SystemExit("invalid bounds")
    cursor = 0
    while True:
        try:
            status, hotset, cursor = scan_once(args, cursor)
            atomic(args.status, status)
            atomic(args.hotset, hotset)
            print(json.dumps(status, sort_keys=True), flush=True)
            if args.once:
                return 0
        except Exception as exc:
            failure = {
                "schema": SCHEMA,
                **SAFETY,
                "execution_authority": False,
                "model_sha": args.model_sha,
                "state": "SCREEN_ERROR",
                "error": type(exc).__name__,
                "timestamp_ms": time.time_ns() // 1_000_000,
                "evidence_quality": EVIDENCE,
                "actionable_candidates": 0,
            }
            atomic(args.status, failure)
            print(json.dumps(failure, sort_keys=True), flush=True)
            if args.once:
                return 2
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
