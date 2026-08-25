#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

STRESS_MULTIPLIERS = (1.0, 1.5, 2.0)
ONE_SIDED_95_Z = 1.6448536269514722


@dataclass(frozen=True)
class Observation:
    decision_ts: float
    outcome_ts: float
    predicted_net_edge: float
    executable_price: float
    future_side_mid: float
    fee_per_share: float
    slippage_per_share: float
    quantity: float = 1.0
    market_id: str = ""

    def stressed_markout(self, multiplier: float) -> float:
        costs = max(0.0, self.fee_per_share) + max(0.0, self.slippage_per_share)
        return self.future_side_mid - self.executable_price - multiplier * costs


def _float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def load_observations(path: Path) -> list[Observation]:
    rows: list[Observation] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                Observation(
                    decision_ts=_float(row, "decision_ts"),
                    outcome_ts=_float(row, "outcome_ts"),
                    predicted_net_edge=_float(row, "predicted_net_edge"),
                    executable_price=_float(row, "executable_price"),
                    future_side_mid=_float(row, "future_side_mid"),
                    fee_per_share=_float(row, "fee_per_share"),
                    slippage_per_share=_float(row, "slippage_per_share"),
                    quantity=max(0.0, _float(row, "quantity", 1.0)),
                    market_id=str(row.get("market_id", "")),
                )
            )
    return rows


def one_sided_lower_bound(values: Iterable[float]) -> float:
    values = list(values)
    if len(values) < 2:
        return float("-inf")
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    return mean - ONE_SIDED_95_Z * sd / math.sqrt(len(values))


def route_observations(
    rows: Iterable[Observation],
    *,
    min_history: int = 12,
    require_stress_multiplier: float = 2.0,
) -> list[dict[str, object]]:
    ordered = sorted(rows, key=lambda x: (x.decision_ts, x.market_id, x.outcome_ts))
    completed: list[Observation] = []
    output: list[dict[str, object]] = []

    for obs in ordered:
        # Fail closed on timestamp ties: only outcomes completed strictly before
        # the current decision are eligible. This prevents future-price,
        # overlapping-horizon, and same-timestamp ordering leakage.
        eligible = [x for x in completed if x.outcome_ts < obs.decision_ts]
        stress_values = [x.stressed_markout(require_stress_multiplier) for x in eligible]
        lcb = one_sided_lower_bound(stress_values)
        mean = statistics.fmean(stress_values) if stress_values else float("-inf")

        if obs.predicted_net_edge <= 0.0:
            route = "SKIP"
            reason = "nonpositive_predicted_edge"
        elif len(eligible) < min_history:
            route = "MAKER_SHADOW"
            reason = "insufficient_causal_forward_history"
        elif lcb <= 0.0:
            route = "MAKER_SHADOW"
            reason = "stressed_forward_markout_not_positive"
        else:
            route = "TAKER_PAPER"
            reason = "causal_stressed_forward_markout_positive"

        output.append(
            {
                "market_id": obs.market_id,
                "decision_ts": obs.decision_ts,
                "outcome_ts": obs.outcome_ts,
                "predicted_net_edge": obs.predicted_net_edge,
                "route": route,
                "reason": reason,
                "causal_history_count": len(eligible),
                "causal_stressed_mean": mean,
                "causal_stressed_lcb95": lcb,
            }
        )
        completed.append(obs)
    return output


def summarize(rows: Iterable[Observation], decisions: Iterable[dict[str, object]]) -> dict[str, object]:
    rows = list(rows)
    decisions = list(decisions)
    stressed: dict[str, object] = {}
    for multiplier in STRESS_MULTIPLIERS:
        per_share = [x.stressed_markout(multiplier) for x in rows]
        pnl = sum(x.stressed_markout(multiplier) * x.quantity for x in rows)
        stressed[f"{multiplier:g}x"] = {
            "mean_per_share": statistics.fmean(per_share) if per_share else 0.0,
            "pnl": pnl,
            "positive_rows": sum(v > 0.0 for v in per_share),
            "rows": len(per_share),
        }
    counts: dict[str, int] = {}
    for item in decisions:
        route = str(item["route"])
        counts[route] = counts.get(route, 0) + 1
    return {
        "schema_version": 1,
        "rows": len(rows),
        "route_counts": counts,
        "cost_stress": stressed,
        "policy": {
            "taker_requires_positive_predicted_edge": True,
            "taker_requires_strictly_prior_causal_forward_history": True,
            "taker_requires_positive_one_sided_95pct_lcb_at_2x_cost": True,
            "fallback_for_unproven_positive_edge": "MAKER_SHADOW",
            "maker_shadow_is_not_a_fill_claim": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route paper HF signals using only causally completed forward-markout evidence."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-history", type=int, default=12)
    args = parser.parse_args()

    rows = load_observations(args.input)
    decisions = route_observations(rows, min_history=max(2, args.min_history))
    payload = summarize(rows, decisions)
    payload["decisions"] = decisions
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload["route_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
