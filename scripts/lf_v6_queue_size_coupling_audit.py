#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scenario:
    queue_ahead_shares: float
    generator_max_notional: float
    broker_target_shares: float
    admitted: bool


def incumbent_generator_max_notional(
    queue_ahead_shares: float,
    capital_per_unit: float,
    min_order_shares: float,
    max_trade_usd: float,
) -> float:
    """Replicate the same-side touch-size coupling in v6_relation_intents.py."""
    if queue_ahead_shares + 1e-12 < min_order_shares:
        return 0.0
    return min(max_trade_usd, queue_ahead_shares * max(capital_per_unit, 1e-6))


def incumbent_broker_target_shares(
    max_notional: float,
    capital_per_unit: float,
    queue_ahead_shares: float,
    min_order_shares: float,
    weight: float = 1.0,
) -> tuple[float, bool]:
    """Replicate the broker's 25%-of-touch same-side depth cap for one equal-weight leg."""
    if max_notional <= 0.0 or capital_per_unit <= 0.0 or weight <= 0.0:
        return 0.0, False
    units = max_notional / capital_per_unit
    units = min(units, 0.25 * max(1.0, queue_ahead_shares) / weight)
    shares = units * weight
    return shares, shares + 1e-9 >= min_order_shares


def decoupled_reference_target_shares(
    max_trade_usd: float,
    capital_per_unit: float,
    min_order_shares: float,
    executable_unwind_capacity_shares: float,
    unwind_fraction: float = 0.25,
) -> tuple[float, bool]:
    """Research comparator: size from risk budget and independent unwind capacity, not queue ahead."""
    if capital_per_unit <= 0.0:
        return 0.0, False
    shares = min(
        max_trade_usd / capital_per_unit,
        unwind_fraction * max(0.0, executable_unwind_capacity_shares),
    )
    return shares, shares + 1e-9 >= min_order_shares


def run_audit() -> dict[str, object]:
    capital_per_unit = 0.98
    min_order_shares = 5.0
    max_trade_usd = 60.0
    queue_grid = [5.0, 19.0, 20.0, 1000.0]

    scenarios: list[Scenario] = []
    for queue in queue_grid:
        generated = incumbent_generator_max_notional(
            queue, capital_per_unit, min_order_shares, max_trade_usd
        )
        shares, admitted = incumbent_broker_target_shares(
            generated, capital_per_unit, queue, min_order_shares
        )
        scenarios.append(Scenario(queue, generated, shares, admitted))

    reference_shares, reference_admitted = decoupled_reference_target_shares(
        max_trade_usd=max_trade_usd,
        capital_per_unit=capital_per_unit,
        min_order_shares=min_order_shares,
        executable_unwind_capacity_shares=100.0,
    )

    hard_queue_floor = 4.0 * min_order_shares
    return {
        "schema": "lf_v6_queue_size_coupling_audit_v1",
        "fixture": {
            "capital_per_unit": capital_per_unit,
            "min_order_shares": min_order_shares,
            "max_trade_usd": max_trade_usd,
            "reference_unwind_capacity_shares": 100.0,
        },
        "incumbent": {
            "scenarios": [asdict(x) for x in scenarios],
            "implied_same_side_queue_floor_shares": hard_queue_floor,
            "finding": (
                "The relation generator and multi-leg broker both couple target size to same-side "
                "touch depth. With the broker's 25% touch cap, a leg cannot satisfy a 5-share "
                "minimum order unless queue ahead is at least 20 shares. Holding all economics "
                "fixed, increasing queue from 20 to 1000 shares increases admitted target size "
                "from 5 to about 61.22 shares, while smaller queues are rejected."
            ),
        },
        "reference": {
            "target_shares": reference_shares,
            "admitted": reference_admitted,
            "interpretation": (
                "This is not a production prescription. It demonstrates the intended separation: "
                "queue ahead belongs in fill/completion probability, while target capacity should "
                "be bounded by risk and independently measured executable unwind depth."
            ),
        },
        "required_experiment": [
            "On identical chronological Graph/RV candidates, compare incumbent same-side-touch sizing with queue-decoupled sizing.",
            "Snapshot queue ahead separately from cumulative executable unwind depth and verified fees at candidate origin.",
            "Keep the same post-cost edge, drawdown, market/event/gross caps and execution deadline across arms.",
            "Measure all-leg completion, partial-fill states, capital-hours, abort/unwind loss and fill-conditioned PnL under 1x/1.5x/2x costs.",
            "Do not promote a larger target merely because it posts more size; require non-degrading state-conditioned executable EV.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run_audit()
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
