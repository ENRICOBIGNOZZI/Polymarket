#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Iterable


def incumbent_full_completion_pnl(units: float, expected_edge: float) -> float:
    """Replicate the V7 forward guard full-completion shortcut."""
    return float(units) * float(expected_edge)


def executed_complete_set_pnl(
    *,
    units: float,
    payout_floor_per_unit: float,
    weights: Iterable[float],
    limit_prices: Iterable[float],
    entry_fee_per_share: Iterable[float],
    cost_multiplier: float,
) -> float:
    weights = [float(x) for x in weights]
    prices = [float(x) for x in limit_prices]
    fees = [float(x) for x in entry_fee_per_share]
    if not (len(weights) == len(prices) == len(fees)) or not weights:
        raise ValueError("weights/prices/fees must be non-empty and aligned")
    if units <= 0.0 or payout_floor_per_unit < 0.0 or cost_multiplier < 0.0:
        raise ValueError("invalid economic inputs")
    entry_cost_per_unit = sum(
        weight * (price + cost_multiplier * fee)
        for weight, price, fee in zip(weights, prices, fees)
    )
    return float(units) * (float(payout_floor_per_unit) - entry_cost_per_unit)


def deterministic_counterexample() -> dict[str, object]:
    # A complete-set basket pays one dollar per matched unit. The quoted prices
    # sum to 0.98, so the relation-intent gross edge is 0.02. A verified fee
    # schedule with maker fees of 0.006/share per leg is sufficient to make the
    # true 2x-cost PnL negative even though the incumbent full-completion path
    # reports the same +0.02 at every stress level.
    units = 1.0
    expected_edge = 0.02
    weights = [1.0, 1.0]
    prices = [0.49, 0.49]
    fees = [0.006, 0.006]
    incumbent = {
        key: incumbent_full_completion_pnl(units, expected_edge)
        for key in ("1x", "1.5x", "2x")
    }
    executed = {
        "1x": executed_complete_set_pnl(
            units=units,
            payout_floor_per_unit=1.0,
            weights=weights,
            limit_prices=prices,
            entry_fee_per_share=fees,
            cost_multiplier=1.0,
        ),
        "1.5x": executed_complete_set_pnl(
            units=units,
            payout_floor_per_unit=1.0,
            weights=weights,
            limit_prices=prices,
            entry_fee_per_share=fees,
            cost_multiplier=1.5,
        ),
        "2x": executed_complete_set_pnl(
            units=units,
            payout_floor_per_unit=1.0,
            weights=weights,
            limit_prices=prices,
            entry_fee_per_share=fees,
            cost_multiplier=2.0,
        ),
    }
    return {
        "gross_quote_edge": expected_edge,
        "incumbent_stress_pnl": incumbent,
        "executed_entry_cost_stress_pnl": executed,
        "incumbent_2x_positive": incumbent["2x"] > 0.0,
        "executed_2x_negative": executed["2x"] < 0.0,
        "finding": (
            "full-completion stress_pnl is not executable cost-stressed PnL when it reuses gross expected_edge "
            "instead of recomputing payout minus executed entry costs and verified fees"
        ),
    }


if __name__ == "__main__":
    print(json.dumps(deterministic_counterexample(), indent=2, sort_keys=True))
