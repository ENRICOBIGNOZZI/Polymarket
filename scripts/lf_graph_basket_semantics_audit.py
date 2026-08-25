#!/usr/bin/env python3
"""Research-only audit of V5 graph projection versus executable basket semantics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence


def graph_projection(prices: Sequence[float], variances: Sequence[float]) -> list[float]:
    if len(prices) != len(variances) or not prices:
        raise ValueError("prices and variances must be non-empty and have equal length")
    varsum = sum(variances)
    if varsum <= 0.0:
        raise ValueError("variances must have positive sum")
    total = sum(prices)
    raw = [p + (1.0 - total) * v / varsum for p, v in zip(prices, variances)]
    clipped = [min(0.999, max(0.001, q)) for q in raw]
    qsum = sum(clipped)
    return [q / qsum for q in clipped]


def deterministic_fixture() -> dict[str, object]:
    # Three mutually exclusive and exhaustive outcomes. The midpoint sum error is
    # 0.34, just inside the current V5 graph_max_sum_error=0.35 gate.
    mids = [0.22, 0.22, 0.22]
    asks = [0.23, 0.23, 0.23]
    true_probabilities = [0.10, 0.45, 0.45]
    variances = [1.0, 1.0, 1.0]
    projected = graph_projection(mids, variances)

    graph_single_leg_model_edge = projected[0] - asks[0]
    true_single_leg_ev = true_probabilities[0] - asks[0]
    basket_gross_profit = 1.0 - sum(asks)
    sum_error = abs(sum(mids) - 1.0)
    graph_confidence = 0.85 * max(0.10, min(1.0, 1.0 - sum_error / 0.35))

    return {
        "mids": mids,
        "asks": asks,
        "true_probabilities": true_probabilities,
        "projected_probabilities": projected,
        "sum_error": sum_error,
        "passes_current_graph_sum_gate": sum_error <= 0.35,
        "graph_confidence_before_singleton_normalization": graph_confidence,
        "graph_single_leg_model_edge": graph_single_leg_model_edge,
        "true_single_leg_ev": true_single_leg_ev,
        "all_yes_basket_gross_profit": basket_gross_profit,
        "interpretation": (
            "The sum-to-one relation identifies a basket arbitrage when all YES asks sum below one, "
            "but it does not identify which individual outcome has positive physical expected value."
        ),
    }


def source_contract(root: Path) -> dict[str, object]:
    engine = (root / "src" / "engine.cpp").read_text(encoding="utf-8")
    manager = (root / "scripts" / "multi_strategy_paper.py").read_text(encoding="utf-8")
    config = json.loads((root / "config" / "paper_v5.json").read_text(encoding="utf-8"))
    graph = next(item for item in config["multi_strategy"]["strategies"] if item["name"] == "graph")
    return {
        "graph_child_enabled": graph["enabled"],
        "graph_capital_fraction": graph["capital_fraction"],
        "graph_max_sum_error": graph["overrides"]["graph_max_sum_error"],
        "projection_present": "(1.0 - sum) * g[i].var / varsum" in engine,
        "single_market_candidate_path_present": "candidates.push_back({std::move(s), &m, &book, fd});" in engine,
        "single_market_paper_trade_present": "paper_trade(c.s, *c.m, *c.b, c.fd, c.s.desired_notional);" in engine,
        "singleton_child_weighting_present": "name: (1.0 if name == strategy.expert else 0.0)" in manager,
    }


def audit(root: Path) -> dict[str, object]:
    fixture = deterministic_fixture()
    contract = source_contract(root)
    defect = bool(
        contract["graph_child_enabled"]
        and contract["projection_present"]
        and contract["single_market_candidate_path_present"]
        and contract["single_market_paper_trade_present"]
        and contract["singleton_child_weighting_present"]
        and fixture["passes_current_graph_sum_gate"]
        and fixture["graph_single_leg_model_edge"] > 0.0
        and fixture["true_single_leg_ev"] < 0.0
        and fixture["all_yes_basket_gross_profit"] > 0.0
    )
    return {
        "status": "STRUCTURAL_DEFECT" if defect else "NO_DEFECT_DEMONSTRATED",
        "source_contract": contract,
        "fixture": fixture,
        "research_decision": "MORE_EVIDENCE_REQUIRED",
        "candidate_design": {
            "structural_signal": "keep neg-risk sum constraint at the event/basket level",
            "directional_probability": "do not infer a single-leg physical q from the constraint projection alone",
            "execution": "evaluate complete executable baskets with all legs, fees, slippage, depth and leg-risk",
            "terminal_model": "if single-leg trading is desired, require an independent calibrated probability estimator",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = audit(Path(args.root).resolve())
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["status"] == "STRUCTURAL_DEFECT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
