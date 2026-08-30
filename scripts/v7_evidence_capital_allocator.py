#!/usr/bin/env python3
"""Evidence-adjusted exploration/exploitation allocation proposal for V7 PAPER.

The report never mutates active child budgets and never auto-promotes a model.
It reserves a small exploration floor for every execution family, assigns the
remaining proposed exploitation pool only to strategies with sufficient
full-cost terminal evidence, and leaves it unallocated otherwise.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_evidence_capital_allocator_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def propose(allocation: dict[str, Any], economics: dict[str, Any], *,
            exploration_fraction: float = 0.10,
            minimum_terminal_units: int = 12) -> dict[str, Any]:
    if (allocation.get("paper_only") is not True
            or allocation.get("authenticated_execution") is not False
            or allocation.get("real_order_submission") is not False):
        raise ValueError("safe_paper_allocation_required")
    budgets = allocation.get("strategy_budgets") if isinstance(allocation.get("strategy_budgets"), dict) else {}
    if not budgets:
        raise ValueError("strategy_budgets_required")
    account = finite(allocation.get("account_starting_capital"))
    if account <= 0.0:
        raise ValueError("positive_account_capital_required")
    mature = economics.get("strategy_mature_terminal_units") if isinstance(economics.get("strategy_mature_terminal_units"), dict) else {}
    stress = economics.get("strategy_stressed_net_pnl") if isinstance(economics.get("strategy_stressed_net_pnl"), dict) else {}
    hours = economics.get("strategy_capital_hours") if isinstance(economics.get("strategy_capital_hours"), dict) else {}
    exploration_fraction = min(0.25, max(0.01, finite(exploration_fraction, 0.10)))
    rows: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    exploration_total = 0.0
    for strategy, raw_budget in sorted(budgets.items()):
        envelope = max(0.0, finite(raw_budget))
        exploration = envelope * exploration_fraction
        exploration_total += exploration
        units = max(0, int(finite(mature.get(strategy))))
        strategy_stress = stress.get(strategy) if isinstance(stress.get(strategy), dict) else {}
        pnl_2x = finite(strategy_stress.get("2x"))
        capital_hours = max(0.0, finite(hours.get(strategy)))
        eligible = units >= minimum_terminal_units and pnl_2x > 0.0 and capital_hours > 0.0
        score = pnl_2x / max(capital_hours, 1e-9) if eligible else 0.0
        scores[strategy] = score
        rows[strategy] = {
            "current_paper_envelope": envelope,
            "exploration_floor": exploration,
            "mature_terminal_units": units,
            "stressed_net_pnl_2x": pnl_2x if units else None,
            "capital_hours": capital_hours if units else None,
            "exploitation_eligible": eligible,
            "evidence_score_pnl_per_capital_hour_2x": score if eligible else None,
            "proposed_exploitation": 0.0,
            "proposed_total": exploration,
        }
    exploitation_pool = max(0.0, account - exploration_total)
    score_total = sum(scores.values())
    if score_total > 0.0:
        for strategy, score in scores.items():
            share = exploitation_pool * score / score_total
            rows[strategy]["proposed_exploitation"] = share
            rows[strategy]["proposed_total"] += share
    allocated = sum(float(row["proposed_total"]) for row in rows.values())
    return {
        "schema": SCHEMA, "timestamp": int(time.time()),
        "model_sha": economics.get("expected_model_sha"),
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "advisory_only": True, "automatic_transfer": False,
        "automatic_promotion": False,
        "active_paper_envelopes_unchanged": True,
        "exploration_fraction_per_strategy_envelope": exploration_fraction,
        "minimum_terminal_units_for_exploitation": minimum_terminal_units,
        "strategies": rows, "exploration_total": exploration_total,
        "exploitation_pool": exploitation_pool,
        "proposed_allocated_total": allocated,
        "unallocated_exploitation_reserve": max(0.0, account - allocated),
        "state": "EXPLOITATION_PROPOSED" if score_total > 0.0 else "EXPLORATION_ONLY_MORE_TERMINAL_EVIDENCE",
        "reason_codes": [] if score_total > 0.0 else ["NO_STRATEGY_PASSES_FULL_COST_2X_TERMINAL_GATE"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--economics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exploration-fraction", type=float, default=0.10)
    parser.add_argument("--minimum-terminal-units", type=int, default=12)
    args = parser.parse_args()
    report = propose(
        load(args.allocation), load(args.economics),
        exploration_fraction=args.exploration_fraction,
        minimum_terminal_units=max(1, args.minimum_terminal_units),
    )
    atomic_json(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
