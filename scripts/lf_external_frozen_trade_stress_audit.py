#!/usr/bin/env python3
"""Research-only audit for external-intelligence cost-stress semantics.

The current external backtester re-runs admission independently at every cost
multiplier. That produces a useful re-optimized admission frontier, but it is not
cost stress of the trade set selected at 1x costs. A losing marginal trade can be
removed when costs rise, so reported aggregate PnL can improve at 1.5x or 2x.

This audit keeps those two questions separate:
  * reselected frontier: re-run admission after changing costs;
  * frozen-trade stress: keep the 1x side/trade set fixed and only worsen costs.

The second quantity is the appropriate robustness check before interpreting a
candidate as surviving execution-cost stress. This file is research-only and
cannot submit orders or mutate the live champion.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class Row:
    name: str
    bid: float
    ask: float
    future_bid: float
    future_ask: float
    predicted_delta: float


def admission_side(row: Row, extra_cost: float) -> int:
    threshold = 0.5 * max(0.0, row.ask - row.bid) + extra_cost
    if row.predicted_delta > threshold:
        return 1
    if row.predicted_delta < -threshold:
        return -1
    return 0


def executable_pnl_for_side(row: Row, side: int, extra_cost: float) -> float:
    if side > 0:
        return row.future_bid - row.ask - extra_cost
    if side < 0:
        return row.bid - row.future_ask - extra_cost
    return 0.0


def reselected_frontier(rows: list[Row], base_cost: float, multipliers: list[float]) -> dict[str, dict[str, float | int]]:
    output: dict[str, dict[str, float | int]] = {}
    for multiplier in multipliers:
        cost = base_cost * multiplier
        sides = [admission_side(row, cost) for row in rows]
        output[str(multiplier)] = {
            "trades": sum(side != 0 for side in sides),
            "pnl": sum(executable_pnl_for_side(row, side, cost) for row, side in zip(rows, sides)),
        }
    return output


def frozen_trade_stress(rows: list[Row], base_cost: float, multipliers: list[float]) -> dict[str, dict[str, float | int]]:
    base_sides = [admission_side(row, base_cost) for row in rows]
    output: dict[str, dict[str, float | int]] = {}
    for multiplier in multipliers:
        cost = base_cost * multiplier
        output[str(multiplier)] = {
            "trades": sum(side != 0 for side in base_sides),
            "pnl": sum(executable_pnl_for_side(row, side, cost) for row, side in zip(rows, base_sides)),
        }
    return output


def counterexample() -> dict:
    rows = [
        Row(
            name="strong_winner",
            bid=0.49,
            ask=0.51,
            future_bid=0.55,
            future_ask=0.57,
            predicted_delta=0.030,
        ),
        Row(
            name="marginal_loser_filtered_by_higher_cost",
            bid=0.49,
            ask=0.51,
            future_bid=0.49,
            future_ask=0.51,
            predicted_delta=0.013,
        ),
    ]
    base_cost = 0.002
    multipliers = [1.0, 1.5, 2.0]
    return {
        "base_cost": base_cost,
        "multipliers": multipliers,
        "rows": [asdict(row) for row in rows],
        "reselected_frontier": reselected_frontier(rows, base_cost, multipliers),
        "frozen_trade_stress": frozen_trade_stress(rows, base_cost, multipliers),
    }


def evaluate() -> dict:
    example = counterexample()
    reselected = example["reselected_frontier"]
    frozen = example["frozen_trade_stress"]
    return {
        "schema": "lf_external_frozen_trade_stress_audit_v1",
        "status": "BLOCKING_EVIDENCE_DEFECT",
        "interpretation": (
            "A cost-stress robustness metric must hold the 1x trade set fixed. "
            "Re-running the admission hurdle at each cost multiplier is a separate "
            "re-optimized frontier and can mechanically improve aggregate PnL by "
            "dropping marginal losing trades."
        ),
        "counterexample": example,
        "checks": {
            "reselected_pnl_can_increase_with_cost": (
                float(reselected["1.5"]["pnl"]) > float(reselected["1.0"]["pnl"])
            ),
            "reselected_trade_count_changes": (
                int(reselected["1.5"]["trades"]) < int(reselected["1.0"]["trades"])
            ),
            "frozen_trade_count_constant": len({int(row["trades"]) for row in frozen.values()}) == 1,
            "frozen_pnl_nonincreasing": (
                float(frozen["1.0"]["pnl"]) >= float(frozen["1.5"]["pnl"]) >= float(frozen["2.0"]["pnl"])
            ),
        },
    }


def main() -> int:
    print(json.dumps(evaluate(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
