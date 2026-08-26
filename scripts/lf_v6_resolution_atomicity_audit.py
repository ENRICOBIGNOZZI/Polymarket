#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExhaustiveBasket:
    prices: tuple[float, ...]
    max_notional: float


def loser_first_transition(
    basket: ExhaustiveBasket,
    first_closed_leg: int,
    remaining_unwind_prices: tuple[float, ...] | None = None,
) -> dict[str, object]:
    """Value the current broker transition when one losing leg closes first.

    The basket is an exhaustive mutually-exclusive YES basket with equal filled
    units on every leg, so holding every matched unit to terminal resolution pays
    exactly one dollar per unit.  The current broker settles a closed leg and then
    aborts the whole live bundle; still-tradable legs are sold at their executable
    bids.  This fixture isolates the economic consequence of that transition.
    """
    if len(basket.prices) < 2 or not 0 <= first_closed_leg < len(basket.prices):
        raise ValueError("invalid exhaustive basket")
    total_cost = sum(basket.prices)
    if total_cost <= 0.0 or total_cost >= 1.0:
        raise ValueError("fixture must be a positive-edge exhaustive basket")
    units = basket.max_notional / total_cost
    if remaining_unwind_prices is None:
        remaining_unwind_prices = tuple(
            px for i, px in enumerate(basket.prices) if i != first_closed_leg
        )
    if len(remaining_unwind_prices) != len(basket.prices) - 1:
        raise ValueError("wrong unwind-price count")

    entry_cash = units * total_cost
    guaranteed_terminal_pnl = units - entry_cash
    unwind_cash = units * sum(remaining_unwind_prices)
    transition_pnl = unwind_cash - entry_cash
    return {
        "first_closed_leg": first_closed_leg,
        "first_closed_price": basket.prices[first_closed_leg],
        "target_units": units,
        "entry_cash": entry_cash,
        "guaranteed_terminal_pnl": guaranteed_terminal_pnl,
        "remaining_unwind_prices": list(remaining_unwind_prices),
        "transition_exit_cash": unwind_cash,
        "transition_pnl_before_exit_fees_slippage": transition_pnl,
        "turns_positive_structural_edge_negative": guaranteed_terminal_pnl > 0.0 and transition_pnl < 0.0,
        "loss_to_guaranteed_profit_ratio": (
            abs(transition_pnl) / guaranteed_terminal_pnl
            if transition_pnl < 0.0 and guaranteed_terminal_pnl > 0.0
            else 0.0
        ),
    }


def source_contract(repo_root: Path) -> dict[str, bool]:
    broker = (repo_root / "src" / "multileg_paper.cpp").read_text(encoding="utf-8")
    relation = (repo_root / "scripts" / "v6_relation_intents.py").read_text(encoding="utf-8")
    compact_broker = "".join(broker.split())
    compact_relation = "".join(relation.split())
    return {
        "closed_legs_are_settled_individually": (
            "if(mi==markets.end()||!mi->second.closed)continue;" in compact_broker
            and "cash_+=payout;" in compact_broker
            and "append_event(\"SETTLE\"" in compact_broker
        ),
        "any_closed_leg_aborts_resting_or_complete_bundle": (
            "if(handle_closed_legs(id,markets)&&(b.status==\"RESTING\"||b.status==\"COMPLETE\")){abort_bundle(id,\"market_closed\");}" in compact_broker
        ),
        "abort_path_sells_still_tradable_filled_legs": (
            "if(b.status==\"ABORTING\")" in compact_broker
            and "exit_bundle(id,books,markets,\"UNWOUND\")" in compact_broker
            and "auto r=sell_all(bk->second,l->filled_shares,cfg_.slippage_bps,fee_for(mi->second));" in compact_broker
        ),
        "graph_hold_deadline_can_extend_past_market_end": (
            "hold=max(now+3600,end_ts+3600ifend_tselsenow+7*86400)" in compact_relation
        ),
        "bundle_level_settling_state_present": any(
            marker in broker
            for marker in (
                'status="SETTLING"',
                'status == "SETTLING"',
                'status=="SETTLING"',
                "event_atomic_settlement",
                "bundle_settlement_barrier",
            )
        ),
    }


def run_audit(repo_root: Path) -> dict[str, object]:
    basket = ExhaustiveBasket(prices=(0.74, 0.15, 0.09), max_notional=60.0)
    transitions = [loser_first_transition(basket, i) for i in range(len(basket.prices))]
    full_profit = transitions[0]["guaranteed_terminal_pnl"]
    return {
        "source_contract": source_contract(repo_root),
        "current_graph_fixture": {
            "prices": list(basket.prices),
            "sum_prices": sum(basket.prices),
            "max_notional": basket.max_notional,
            "guaranteed_terminal_profit": full_profit,
            "loser_first_transitions_at_unchanged_remaining_bids": transitions,
            "least_bad_transition_pnl": max(
                float(x["transition_pnl_before_exit_fees_slippage"]) for x in transitions
            ),
            "worst_transition_pnl": min(
                float(x["transition_pnl_before_exit_fees_slippage"]) for x in transitions
            ),
        },
        "finding": (
            "The V6 multi-leg broker treats the first individually closed leg as an abort trigger for an otherwise live "
            "Graph/RV bundle.  It settles that closed leg and sends every still-tradable filled leg through taker unwind. "
            "For a fully matched exhaustive basket this can destroy a guaranteed terminal payoff solely because market "
            "closure/resolution metadata arrives asynchronously across legs.  A closed-but-not-yet-resolved leg also "
            "triggers the same abort path."
        ),
        "required_successor_contract": [
            "Separate SETTLING from ABORTING for structurally matched Graph/RV units.",
            "Do not unwind already matched structural units merely because one leg reports closed before its siblings.",
            "Use an authoritative event-level terminal barrier or wait until every matched leg has a terminal outcome before final settlement accounting.",
            "If closure metadata is partial or a closed leg lacks a resolved outcome, preserve the matched hedge fail-closed and only cancel/unwind genuinely residual unmatched exposure.",
            "Keep operational fill completion, economic hedge completion, and terminal settlement as separate ledger states.",
            "Do not rely on a hold deadline after market end as an executable-book exit; terminal Graph/RV economics must have an explicit settlement path.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_audit(args.repo_root.resolve())
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
