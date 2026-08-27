#!/usr/bin/env python3
"""Fit the V7 maker execution model from the canonical exact-SHA ledger.

The model is deliberately small and robust: Beta-Binomial fill estimates plus
fill-conditioned executable markout summaries by action/outcome/side.  It is a
causal execution model, not a directional probability model.  It can be
replaced by richer logistic/GBM models later without changing the ledger or
quote-control contract.
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
        filled_orders = [order for order in group if fills_by_order.get(order.order_id or "")]
        n_orders = len(group)
        n_filled = len(filled_orders)
        fill_probability = (alpha0 + n_filled) / (alpha0 + beta0 + n_orders)
        markout_values: dict[str, list[float]] = defaultdict(list)
        event_clusters: set[str] = set()
        for order in group:
            event_clusters.add(_event_cluster(order))
            for fill_event in fills_by_order.get(order.order_id or "", []):
                for markout in markouts_by_fill.get(fill_event.fill_id or "", []):
                    for horizon, value in markout.markouts.items():
                        pnl_per_share = finite(value, math.nan)
                        if math.isfinite(pnl_per_share):
                            markout_values[horizon].append(pnl_per_share)
        markout_summary = {}
        preferred = []
        for horizon in ("1s", "10s", "45s", "60s", "300s"):
            values = markout_values.get(horizon, [])
            if values:
                mean = statistics.fmean(values)
                median = statistics.median(values)
                stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
                markout_summary[horizon] = {
                    "n": len(values), "mean_pnl_per_share": mean,
                    "median_pnl_per_share": median, "stdev": stdev,
                    "adverse_cost_per_share": max(0.0, -mean),
                }
                if horizon in {"45s", "60s"}:
                    preferred.extend(values)
        if not preferred:
            for values in markout_values.values():
                preferred.extend(values)
        adverse = max(0.0, -statistics.fmean(preferred)) if preferred else 0.002
        output[key] = {
            "orders": n_orders,
            "filled_orders": n_filled,
            "fill_probability": fill_probability,
            "event_clusters": len(event_clusters),
            "adverse_markout_per_share": adverse,
            "markouts": markout_summary,
            "mature": n_orders >= 50 and n_filled >= 20 and len(event_clusters) >= 5,
        }

    return {
        "schema": "polymarket_v7_maker_execution_model_v1",
        "strategy": STRATEGY,
        "paper_only": True,
        "authenticated_execution": False,
        "generated_ts_ms": time.time_ns() // 1_000_000,
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
