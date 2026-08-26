#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BasketFixture:
    prices: tuple[float, ...]
    max_notional: float
    completion_threshold: float
    max_leg_risk_usd: float
    expected_edge: float


def evaluate_threshold_state(fixture: BasketFixture, excess_leg: int = 0) -> dict[str, object]:
    """Evaluate the first state that can cross the broker's fixed completion gate.

    The fixture is an exhaustive mutually-exclusive YES basket: exactly one leg
    pays one dollar per share at resolution.  Every leg has equal target weight.
    One leg is fully filled while all other legs are filled exactly to the common
    completion threshold.  This is enough to show whether operational completion
    preserves the basket's structural payoff floor.
    """
    if not fixture.prices or not 0 <= excess_leg < len(fixture.prices):
        raise ValueError("invalid basket fixture")
    capital_per_unit = sum(fixture.prices)
    if capital_per_unit <= 0.0:
        raise ValueError("basket cost must be positive")
    target_units = fixture.max_notional / capital_per_unit
    fractions = [fixture.completion_threshold for _ in fixture.prices]
    fractions[excess_leg] = 1.0
    fills = [target_units * fraction for fraction in fractions]
    common = min(fractions)
    entry_cash = sum(fill * price for fill, price in zip(fills, fixture.prices))
    unmatched_entry_risk = sum(
        max(0.0, fill - common * target_units) * price
        for fill, price in zip(fills, fixture.prices)
    )
    terminal_pnl = [fill - entry_cash for fill in fills]
    full_bundle_profit = target_units * (1.0 - capital_per_unit)
    worst_pnl = min(terminal_pnl)
    best_pnl = max(terminal_pnl)
    return {
        "target_units": target_units,
        "fill_fractions": fractions,
        "filled_shares": fills,
        "common_completion": common,
        "entry_cash": entry_cash,
        "unmatched_entry_risk": unmatched_entry_risk,
        "passes_fixed_completion_gate": common + 1e-12 >= fixture.completion_threshold,
        "passes_absolute_unmatched_risk_gate": unmatched_entry_risk <= fixture.max_leg_risk_usd + 1e-12,
        "terminal_pnl_by_winning_leg": terminal_pnl,
        "worst_terminal_pnl": worst_pnl,
        "best_terminal_pnl": best_pnl,
        "full_bundle_guaranteed_profit": full_bundle_profit,
        "worst_loss_to_full_profit_ratio": (
            abs(worst_pnl) / full_bundle_profit if full_bundle_profit > 1e-12 else None
        ),
        "advertised_edge_dollars_at_max_notional": fixture.expected_edge * fixture.max_notional,
    }


def source_contract(repo_root: Path) -> dict[str, bool]:
    broker = (repo_root / "src" / "multileg_paper.cpp").read_text(encoding="utf-8")
    execution = (repo_root / "include" / "pm" / "execution.hpp").read_text(encoding="utf-8")
    loop = (repo_root / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
    compact_broker = "".join(broker.split())
    compact_loop = "".join(loop.split())
    return {
        "completion_is_minimum_leg_fraction": "returnpm::minimum_completion(ft);" in compact_broker,
        "fixed_threshold_marks_bundle_complete": "if(c>=completion_threshold_){b.status=\"COMPLETE\";" in compact_broker,
        "residual_orders_cancelled_after_threshold": "request_cancel(*l,nowm,\"completion_residual\")" in compact_broker,
        "absolute_unmatched_entry_risk_gate_present": "bundle_leg_risk(id)>max_leg_risk_usd_" in compact_broker,
        "unmatched_risk_uses_common_completion": "constdoublecommon=minimum_completion(completion_inputs);" in "".join(execution.split()),
        "v6_completion_threshold_is_075": "--completion-threshold0.75" in compact_loop,
        "v6_max_leg_risk_is_12": "--max-leg-risk-usd12" in compact_loop,
        "economic_completion_recheck_present": any(
            marker in broker
            for marker in (
                "guaranteed_completion_pnl",
                "economic_completion",
                "completion_edge_budget",
                "residual_payoff_floor",
            )
        ),
    }


def run_audit(repo_root: Path) -> dict[str, object]:
    fixture = BasketFixture(
        prices=(0.74, 0.15, 0.09),
        max_notional=60.0,
        completion_threshold=0.75,
        max_leg_risk_usd=12.0,
        expected_edge=0.02,
    )
    state = evaluate_threshold_state(fixture, excess_leg=0)
    return {
        "source_contract": source_contract(repo_root),
        "current_graph_fixture": {
            "prices": list(fixture.prices),
            "max_notional": fixture.max_notional,
            "completion_threshold": fixture.completion_threshold,
            "max_leg_risk_usd": fixture.max_leg_risk_usd,
            "expected_edge": fixture.expected_edge,
            **state,
        },
        "finding": (
            "The V6 broker treats a fixed minimum per-leg fill fraction as COMPLETE and only "
            "checks an absolute unmatched-entry-risk budget.  For a structural Graph/RV basket, "
            "that can destroy the guaranteed payoff floor while still passing both gates.  "
            "Operational completion must therefore be separated from economic hedge completion."
        ),
        "required_successor_contract": [
            "Keep minimum leg fill fraction as an execution-progress statistic, not proof of an economically complete basket.",
            "After residual cancels become effective, recompute matched common units and residual unmatched exposure using actual fills.",
            "For structural baskets, require guaranteed terminal payoff after entry costs, verified fees, slippage and residual unwind to remain positive before HOLD/COMPLETE.",
            "If the residual exposure erases the structural edge, rebalance or unwind rather than holding the basket as COMPLETE.",
            "Report operational completion and economic hedge completion separately in fill-conditioned PnL evidence.",
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