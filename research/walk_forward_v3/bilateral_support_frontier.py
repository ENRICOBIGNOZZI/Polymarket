"""OOS frontier for bilateral opposite-side training support."""
from __future__ import annotations

from research.walk_forward_v2.core import SAFETY, folds
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    evaluate_direct_action_policy,
    summarize_direct_action,
)

SCHEMA = "polymarket_direct_action_bilateral_support_frontier_v1"
DEFAULT_MINIMUM_MARKETS = (0, 10, 25, 50, 100, 250)


def bilateral_support_frontier(
    records,
    *,
    desired_folds=3,
    minimum_market_grid=DEFAULT_MINIMUM_MARKETS,
    latency_ms=50,
    capital_budget=10_000.0,
    model_kwargs=None,
):
    thresholds = tuple(sorted({int(value) for value in minimum_market_grid}))
    if any(value < 0 for value in thresholds):
        raise ValueError("nonnegative bilateral support thresholds required")

    found, receipt = folds(records, desired_folds=desired_folds)
    result = {
        "schema": SCHEMA,
        **SAFETY,
        "state": receipt.get("state"),
        "fold_receipt": receipt,
        "latency_ms": int(latency_ms),
        "capital_budget": float(capital_budget),
        "minimum_market_grid": list(thresholds),
        "selection": "NONE_RESEARCH_FRONTIER_ONLY",
        "folds": [],
    }
    if not found:
        return result

    for fold in found:
        kwargs = dict(model_kwargs or {})
        kwargs["minimum_bilateral_opposite_side_markets"] = 0
        model = DirectActionValueModel(**kwargs).fit(
            fold["train_repricing"])
        cells = []
        for threshold in thresholds:
            model.minimum_bilateral_opposite_side_markets = threshold
            outcomes = evaluate_direct_action_policy(
                model,
                fold["test"],
                latency_ms=latency_ms,
                capital_budget=capital_budget,
                one_entry_per_market=True,
                live_geometry=True,
            )
            summary = summarize_direct_action(outcomes)
            cells.append({
                "minimum_bilateral_opposite_side_markets": threshold,
                "training_bilateral_supported_markets": int(
                    model.bilateral_support_markets),
                "opposite_side_ready": (
                    model.bilateral_support_markets >= threshold),
                "economics": summary,
                "selected_side_counts": {
                    side: int(cell.get("trades") or 0)
                    for side, cell in (summary.get("by_side") or {}).items()
                },
            })
        model.minimum_bilateral_opposite_side_markets = 0
        result["folds"].append({
            "fold": fold["fold"],
            "cutoff_ns": fold["cutoff_ns"],
            "train_markets": len(fold["train_markets"]),
            "test_markets": len(fold["test_markets"]),
            "training": model.training_receipt,
            "cells": cells,
        })

    result["state"] = "READY"
    return result
