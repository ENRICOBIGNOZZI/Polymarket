#!/usr/bin/env python3
"""Read-only LEAD_LAG_TAKER_V1 PnL attribution by signal age."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

THRESHOLDS_MS = (100, 250, 500, 1000, 2000, 5000)
FAMILIES = {"lead_lag_taker_v1", "crypto_informed_taker"}
AUTH = {
    "paper_only": True,
    "authenticated_execution": False,
    "real_order_submission": False,
    "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def family(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return str(metadata.get("model_family") or metadata.get("component") or "")


def event_fill_ages(events: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in events:
        if row.get("event") != "FILLED":
            continue
        market = str(row.get("market_id") or "")
        age = finite(row.get("signal_age_ms"))
        if market and age is not None and age >= 0:
            result.setdefault(market, age)
    return result


def ledger_fill_ages(rows: list[dict[str, Any]]) -> tuple[dict[str, float], set[str]]:
    observed: dict[str, list[float]] = {}
    for row in rows:
        if row.get("event_type") != "FILL" or family(row) not in FAMILIES:
            continue
        order = str(row.get("order_id") or "")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        age = finite(metadata.get("signal_age_ms_at_fill", metadata.get("signal_age_ms")))
        if order and age is not None and age >= 0:
            observed.setdefault(order, []).append(age)
    result: dict[str, float] = {}
    conflicts: set[str] = set()
    for order, ages in observed.items():
        if max(ages) - min(ages) > 1e-6:
            conflicts.add(order)
        else:
            result[order] = ages[0]
    return result, conflicts


def describe(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [float(row["final_pnl"]) for row in rows]
    wins = sum(value > 0 for value in pnl)
    losses = sum(value < 0 for value in pnl)
    total = sum(pnl)
    return {
        "settled_markets": len(rows),
        "wins": wins,
        "losses": losses,
        "flat": len(rows) - wins - losses,
        "win_rate": wins / len(rows) if rows else None,
        "total_pnl": total,
        "mean_pnl": total / len(rows) if rows else None,
    }


def analyze(ledger: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    by_order, conflicts = ledger_fill_ages(ledger)
    by_market = event_fill_ages(events)
    settled: list[dict[str, Any]] = []
    missing_age = 0
    for row in ledger:
        if row.get("event_type") != "FINAL" or family(row) not in FAMILIES:
            continue
        pnl = finite(row.get("final_pnl"))
        if pnl is None:
            continue
        order = str(row.get("order_id") or "")
        market = str(row.get("market_id") or "")
        age = None if order in conflicts else by_order.get(order, by_market.get(market))
        if age is None:
            missing_age += 1
        settled.append({
            "order_id": order,
            "market_id": market,
            "signal_age_ms": age,
            "final_pnl": pnl,
        })
    observed = [row for row in settled if row["signal_age_ms"] is not None]
    cumulative = []
    for cap in THRESHOLDS_MS:
        cohort = [row for row in observed if float(row["signal_age_ms"]) <= cap]
        cumulative.append({"maximum_signal_age_ms": cap, **describe(cohort)})
    buckets = []
    lower = 0
    for upper in THRESHOLDS_MS:
        cohort = [
            row for row in observed
            if lower < float(row["signal_age_ms"]) <= upper
        ]
        buckets.append({"minimum_exclusive_ms": lower, "maximum_inclusive_ms": upper,
                        **describe(cohort)})
        lower = upper
    return {
        "schema": "polymarket_v7_lead_lag_signal_age_report_v1",
        **AUTH,
        "strategy_id": "LEAD_LAG_TAKER_V1",
        "frozen_v1_modified": False,
        "threshold_selection_authorized": False,
        "automatic_promotion": False,
        "settled": describe(settled),
        "settled_with_observed_signal_age": len(observed),
        "settled_missing_signal_age": missing_age,
        "conflicting_fill_age_orders": sorted(conflicts),
        "cumulative_thresholds": cumulative,
        "disjoint_age_buckets": buckets,
        "rows": settled,
        "interpretation_limit": (
            "Retrospective age cohorts are descriptive and confounded by market, price, "
            "direction and regime. They do not authorize tuning the frozen V1 protocol."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(load_jsonl(args.ledger), load_jsonl(args.events))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
