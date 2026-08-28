#!/usr/bin/env python3
"""Fit the V7 maker execution model from the canonical exact-SHA ledger.

The decision-facing fill quantity is the posterior expected *filled fraction per
posted share*, not a Bernoulli indicator that an order received any fill. This
matters for passive market making because a 5% partial fill is economically very
different from a complete fill. Any-fill/full-fill/partial-fill probabilities
remain explicit diagnostics.

Adverse markout is fill-conditioned and filled-size weighted. A single causal
markout horizon is selected for each execution group so multiple horizons from
the same fill are never treated as independent observations of one target.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import statistics
import time
from typing import Any

from v7_execution_ledger import LedgerEvent

STRATEGY = "MICRO_MAKER_PRO"
MARKOUT_HORIZONS = ("1s", "10s", "45s", "60s", "300s")
# 45s is the canonical adverse-selection target requested by the execution
# evidence contract. If it is not observed yet, use exactly one shorter/nearby
# horizon rather than pooling correlated horizons from the same fill.
ADVERSE_HORIZON_PRIORITY = ("45s", "60s", "10s", "1s", "300s")
_EPS = 1e-12


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_records(path: Path, model_sha: str) -> list[LedgerEvent]:
    records: list[LedgerEvent] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = LedgerEvent.from_dict(json.loads(line))
            except Exception:
                continue
            if event.model_sha == model_sha and event.strategy == STRATEGY:
                records.append(event)
    return records


def _key(event: LedgerEvent) -> str:
    meta = event.metadata if isinstance(event.metadata, dict) else {}
    outcome = str(meta.get("outcome") or meta.get("outcome_side") or "UNKNOWN").upper()
    action = str(event.intended_action or meta.get("action") or "UNKNOWN").upper()
    side = str(event.side or meta.get("execution_side") or "UNKNOWN").upper()
    return f"{action}|{outcome}|{side}"


def _event_cluster(event: LedgerEvent) -> str:
    return str(event.event_id or event.market_id or "UNKNOWN")


def _posterior_rate(success_mass: float, observations: int, alpha0: float, beta0: float) -> float:
    return (alpha0 + max(0.0, float(success_mass))) / (alpha0 + beta0 + max(0, observations))


def _order_filled_fraction(order: LedgerEvent, fills: list[LedgerEvent]) -> float:
    intended = max(0.0, finite(order.intended_size, 0.0))
    if intended <= _EPS:
        return 0.0
    filled = sum(max(0.0, finite(fill.filled_size, 0.0)) for fill in fills)
    return min(1.0, filled / intended)


def _weighted_mean(entries: list[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in entries if weight > 0.0)
    if total_weight <= _EPS:
        return math.nan
    return sum(value * weight for value, weight in entries if weight > 0.0) / total_weight


def _weighted_stdev(entries: list[tuple[float, float]], mean: float) -> float:
    total_weight = sum(weight for _, weight in entries if weight > 0.0)
    if total_weight <= _EPS or not math.isfinite(mean):
        return 0.0
    variance = sum(
        weight * (value - mean) ** 2
        for value, weight in entries
        if weight > 0.0
    ) / total_weight
    return math.sqrt(max(0.0, variance))


def _adverse_target(
    markout_values: dict[str, dict[str, tuple[float, float]]],
) -> tuple[str | None, list[tuple[float, float]]]:
    for horizon in ADVERSE_HORIZON_PRIORITY:
        values = list(markout_values.get(horizon, {}).values())
        if values:
            return horizon, values
    return None, []


def fit(records: list[LedgerEvent], *, cold_fill_prior: float = 0.02,
        prior_strength: float = 20.0) -> dict[str, Any]:
    orders = {e.order_id: e for e in records if e.event_type == "ORDER_SUBMITTED" and e.order_id}
    fills_by_order: dict[str, list[LedgerEvent]] = defaultdict(list)
    fill_by_id: dict[str, LedgerEvent] = {}
    markouts_by_fill: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in records:
        if event.event_type == "FILL" and event.order_id and event.fill_id:
            fills_by_order[event.order_id].append(event)
            fill_by_id[event.fill_id] = event
        elif event.event_type == "MARKOUT" and event.fill_id:
            markouts_by_fill[event.fill_id].append(event)

    grouped: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in orders.values():
        grouped[_key(event)].append(event)
    grouped["GLOBAL"] = list(orders.values())

    alpha0 = max(1e-6, cold_fill_prior * prior_strength)
    beta0 = max(1e-6, (1.0 - cold_fill_prior) * prior_strength)
    output: dict[str, Any] = {}
    for key, group in grouped.items():
        fill_fractions: list[float] = []
        event_clusters: set[str] = set()
        for order in group:
            event_clusters.add(_event_cluster(order))
            fill_fractions.append(_order_filled_fraction(order, fills_by_order.get(order.order_id or "", [])))

        n_orders = len(group)
        n_any_fill = sum(fraction > _EPS for fraction in fill_fractions)
        n_full_fill = sum(fraction >= 1.0 - 1e-9 for fraction in fill_fractions)
        n_partial_fill = sum(_EPS < fraction < 1.0 - 1e-9 for fraction in fill_fractions)
        filled_fraction_mass = sum(fill_fractions)

        # Decision-facing quantity: expected fraction of posted shares that fill.
        # With full fills only this reduces to the former Beta-Bernoulli estimate,
        # so existing cold-start behavior remains backward compatible.
        fill_probability = _posterior_rate(filled_fraction_mass, n_orders, alpha0, beta0)
        any_fill_probability = _posterior_rate(n_any_fill, n_orders, alpha0, beta0)
        full_fill_probability = _posterior_rate(n_full_fill, n_orders, alpha0, beta0)
        empirical_mean_filled_fraction = filled_fraction_mass / n_orders if n_orders else 0.0

        # Deduplicate by fill_id within each horizon. Every observation carries
        # its own filled_size weight, so tiny partial fills cannot dominate the
        # adverse-selection estimate as much as economically large fills.
        markout_values: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
        for order in group:
            for fill_event in fills_by_order.get(order.order_id or "", []):
                fill_id = str(fill_event.fill_id or "")
                filled_shares = max(0.0, finite(fill_event.filled_size, 0.0))
                if not fill_id or filled_shares <= _EPS:
                    continue
                for markout in markouts_by_fill.get(fill_id, []):
                    for horizon, value in markout.markouts.items():
                        pnl_per_share = finite(value, math.nan)
                        if horizon in MARKOUT_HORIZONS and math.isfinite(pnl_per_share):
                            markout_values[horizon][fill_id] = (pnl_per_share, filled_shares)

        markout_summary: dict[str, Any] = {}
        for horizon in MARKOUT_HORIZONS:
            entries = list(markout_values.get(horizon, {}).values())
            if entries:
                mean = _weighted_mean(entries)
                raw_values = [value for value, _ in entries]
                median = statistics.median(raw_values)
                stdev = _weighted_stdev(entries, mean)
                filled_shares = sum(weight for _, weight in entries)
                markout_summary[horizon] = {
                    "n": len(entries),
                    "filled_shares": filled_shares,
                    "mean_pnl_per_share": mean,
                    "median_pnl_per_share": median,
                    "stdev": stdev,
                    "weighting": "filled_size",
                    "adverse_cost_per_share": max(0.0, -mean),
                }

        adverse_horizon, adverse_entries = _adverse_target(markout_values)
        adverse_mean = _weighted_mean(adverse_entries) if adverse_entries else math.nan
        adverse = max(0.0, -adverse_mean) if adverse_entries else 0.002
        adverse_filled_shares = sum(weight for _, weight in adverse_entries)
        output[key] = {
            "orders": n_orders,
            # Backward-compatible diagnostic name: any positive operational fill.
            "filled_orders": n_any_fill,
            "any_filled_orders": n_any_fill,
            "fully_filled_orders": n_full_fill,
            "partially_filled_orders": n_partial_fill,
            "empirical_mean_filled_fraction": empirical_mean_filled_fraction,
            "fill_probability": fill_probability,
            "fill_probability_semantics": "posterior_expected_filled_fraction_per_posted_share",
            "any_fill_probability": any_fill_probability,
            "full_fill_probability": full_fill_probability,
            "event_clusters": len(event_clusters),
            "adverse_markout_per_share": adverse,
            "adverse_markout_horizon": adverse_horizon,
            "adverse_markout_n": len(adverse_entries),
            "adverse_markout_filled_shares": adverse_filled_shares,
            "adverse_markout_mean_pnl_per_share": adverse_mean if adverse_entries else None,
            "adverse_markout_weighting": "filled_size",
            "markouts": markout_summary,
            "mature": n_orders >= 50 and n_any_fill >= 20 and len(event_clusters) >= 5,
        }

    return {
        "schema": "polymarket_v7_maker_execution_model_v1",
        "strategy": STRATEGY,
        "paper_only": True,
        "authenticated_execution": False,
        "generated_ts_ms": time.time_ns() // 1_000_000,
        "fill_probability_semantics": "posterior_expected_filled_fraction_per_posted_share",
        "partial_fills_are_fractional_success_mass": True,
        "adverse_target_horizon": "45s",
        "adverse_horizon_fallback_order": list(ADVERSE_HORIZON_PRIORITY),
        "adverse_markout_weighting": "filled_size",
        "correlated_horizons_are_not_pooled": True,
        "groups": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cold-fill-prior", type=float, default=0.02)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact 40-hex model SHA required")
    result = fit(read_records(args.ledger, args.model_sha), cold_fill_prior=args.cold_fill_prior)
    result["model_sha"] = args.model_sha
    atomic_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
