#!/usr/bin/env python3
"""Research-only diagnostic for confidence handling in V5 single-expert books.

V5 deliberately runs each expert in an independent paper book.  The legacy
ensemble, however, uses expert confidence only as a multiplicative mixture
weight.  With exactly one active expert that factor cancels from the weighted
mean and the disagreement variance is identically zero.  This script makes
that algebra explicit and proposes *evaluation targets*, not a production fix.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

LF_EXPERTS = ("pca", "graph", "semantic", "external")


def incumbent_singleton(q_yes: float, confidence: float, spread: float) -> tuple[float, float]:
    """Exact V5 singleton consequence of Engine::ensemble for confidence > 0."""
    if not 0.0 < confidence <= 1.0:
        raise ValueError("confidence must be in (0, 1]")
    if not 0.0 <= q_yes <= 1.0:
        raise ValueError("q_yes must be in [0, 1]")
    if spread < 0.0:
        raise ValueError("spread must be nonnegative")
    # For one active prediction, q = w*q / w and weighted disagreement is zero.
    fair = min(0.999, max(0.001, q_yes))
    uncertainty = min(1.0, max(1e-4, 0.5 * spread))
    return fair, uncertainty


def research_shrink_to_market(mid: float, q_yes: float, confidence: float) -> float:
    """Simple monotone research baseline; not a proposed production formula."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be in [0, 1]")
    return min(0.999, max(0.001, mid + confidence * (q_yes - mid)))


def source_contract(root: Path) -> dict[str, bool]:
    engine = (root / "src/engine.cpp").read_text(encoding="utf-8")
    manager = (root / "scripts/multi_strategy_paper.py").read_text(encoding="utf-8")
    return {
        "confidence_enters_only_mixture_weight": (
            "const double w = base * std::exp(-2.0 * brier) * e.confidence;" in engine
        ),
        "mixture_normalizes_by_weight_sum": "q /= sw;" in engine,
        "singleton_disagreement_variance_formula": (
            "v += w * (e.q_yes - q) * (e.q_yes - q);" in engine and "v /= sw;" in engine
        ),
        "uncertainty_adds_spread_only_after_disagreement": (
            "0.25 * spread * spread" in engine
        ),
        "pca_emits_variable_confidence": (
            "z_conf * sample_conf * factor_conf * reversion_conf" in engine
        ),
        "external_confidence_decays_with_age": (
            "s.confidence *= std::exp(-std::log(2.0) * age / (6.0 * 3600.0));" in engine
        ),
        "v5_child_has_exactly_one_active_expert": (
            "name: (1.0 if name == strategy.expert else 0.0)" in manager
        ),
    }


def lf_strategy_spec(root: Path) -> list[dict[str, Any]]:
    cfg = json.loads((root / "config/paper_v5.json").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for raw in cfg["multi_strategy"]["strategies"]:
        if raw.get("enabled", True) and raw.get("expert") in LF_EXPERTS:
            rows.append(
                {
                    "name": raw["name"],
                    "expert": raw["expert"],
                    "capital_fraction": float(raw["capital_fraction"]),
                    "uncertainty_penalty": float(raw.get("overrides", {}).get("uncertainty_penalty", cfg["uncertainty_penalty"])),
                    "min_net_edge": float(raw.get("overrides", {}).get("min_net_edge", cfg["min_net_edge"])),
                }
            )
    return rows


def build_report(root: Path) -> dict[str, Any]:
    contracts = source_contract(root)
    strategies = lf_strategy_spec(root)
    q = 0.62
    mid = 0.60
    spread = 0.02
    confidence_grid = (1.0, 0.50, 0.10, 0.02)
    rows = []
    for confidence in confidence_grid:
        fair, uncertainty = incumbent_singleton(q, confidence, spread)
        shrunk = research_shrink_to_market(mid, q, confidence)
        rows.append(
            {
                "confidence": confidence,
                "incumbent_fair_yes": fair,
                "incumbent_uncertainty": uncertainty,
                "incumbent_edge_from_mid": fair - mid,
                "research_shrink_edge_from_mid": shrunk - mid,
            }
        )
    invariant = len({(row["incumbent_fair_yes"], row["incumbent_uncertainty"]) for row in rows}) == 1
    return {
        "schema": "polymarket_lf_single_expert_confidence_diagnostic_v1",
        "production_changed": False,
        "lf_v5_strategies": strategies,
        "source_contract": contracts,
        "all_source_contracts_present": all(contracts.values()),
        "fixture": {
            "market_mid": mid,
            "expert_q_yes": q,
            "spread": spread,
            "rows": rows,
            "incumbent_is_confidence_invariant": invariant,
            "low_vs_high_confidence_fair_difference": rows[0]["incumbent_fair_yes"] - rows[-1]["incumbent_fair_yes"],
            "low_vs_high_confidence_uncertainty_difference": rows[0]["incumbent_uncertainty"] - rows[-1]["incumbent_uncertainty"],
        },
        "interpretation": [
            "In a one-expert V5 child, confidence cancels from the normalized fair probability.",
            "The one-expert disagreement variance is zero, so uncertainty falls back to half the spread.",
            "Therefore PCA/graph/external reliability information cannot directly reduce edge or increase uncertainty inside its independent book.",
            "This is a structural diagnostic, not evidence that a particular confidence-aware mapping improves PnL.",
        ],
        "required_common_sample_ablation": [
            "incumbent singleton confidence-invariant decision",
            "monotone confidence shrink-to-market",
            "confidence-dependent uncertainty inflation",
            "calibrated confidence floor/abstention",
        ],
        "required_metrics": [
            "time-to-resolution Brier score",
            "time-to-resolution log loss",
            "reliability/calibration error",
            "executable OOS net PnL",
            "1x/1.5x/2x cost-stressed PnL",
            "turnover and candidate survival",
            "portfolio drawdown and cross-strategy covariance",
        ],
        "evidence_state": "MORE_EVIDENCE_REQUIRED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = build_report(Path(args.root).resolve())
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["all_source_contracts_present"] and report["fixture"]["incumbent_is_confidence_invariant"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
