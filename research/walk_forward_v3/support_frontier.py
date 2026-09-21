"""OOS support-gating frontier for Direct Action PAPER research."""
from __future__ import annotations

from research.walk_forward_v2.core import SAFETY, folds
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    evaluate_direct_action_policy,
    summarize_direct_action,
)

SCHEMA = "polymarket_direct_action_support_frontier_v1"
DEFAULT_EVIDENCE_THRESHOLDS = (None, 0.50, 0.70, 0.85)
DEFAULT_EXECUTION_THRESHOLDS = (None, 0.25, 0.50, 0.75)


def support_frontier(
    records,
    *,
    desired_folds=3,
    evidence_thresholds=DEFAULT_EVIDENCE_THRESHOLDS,
    execution_thresholds=DEFAULT_EXECUTION_THRESHOLDS,
    latency_ms=50,
    capital_budget=10_000.0,
    model_kwargs=None,
):
    def normalize(values):
        output = []
        for value in values:
            if value is None:
                output.append(None)
                continue
            value = float(value)
            if not 0.0 <= value <= 1.0:
                raise ValueError("support threshold must be in [0,1]")
            output.append(value)
        return tuple(output)

    evidence_grid = normalize(evidence_thresholds)
    execution_grid = normalize(execution_thresholds)
    found, receipt = folds(records, desired_folds=desired_folds)
    result = {
        "schema": SCHEMA,
        **SAFETY,
        "state": receipt.get("state"),
        "fold_receipt": receipt,
        "latency_ms": int(latency_ms),
        "capital_budget": float(capital_budget),
        "evidence_thresholds": list(evidence_grid),
        "execution_thresholds": list(execution_grid),
        "selection": "NONE_RESEARCH_FRONTIER_ONLY",
        "folds": [],
    }
    if not found:
        return result

    for fold in found:
        kwargs = dict(model_kwargs or {})
        kwargs["support_heads_enabled"] = True
        kwargs["minimum_evidence_support_probability"] = None
        kwargs["minimum_full_execution_probability"] = None
        model = DirectActionValueModel(**kwargs).fit(
            fold["train_repricing"])
        cells = []
        for evidence in evidence_grid:
            for execution in execution_grid:
                model.minimum_evidence_support_probability = evidence
                model.minimum_full_execution_probability = execution
                outcomes = evaluate_direct_action_policy(
                    model,
                    fold["test"],
                    latency_ms=latency_ms,
                    capital_budget=capital_budget,
                    one_entry_per_market=True,
                    live_geometry=True,
                )
                summary = summarize_direct_action(outcomes)
                selected = [
                    row for row in outcomes
                    if row.get("action") == "TRADE"
                ]
                evidence_values = [
                    float(row["evidence_support_probability"])
                    for row in selected
                    if isinstance(
                        row.get("evidence_support_probability"),
                        (int, float),
                    )
                ]
                execution_values = [
                    float(row["full_execution_probability"])
                    for row in selected
                    if isinstance(
                        row.get("full_execution_probability"),
                        (int, float),
                    )
                ]
                cells.append({
                    "minimum_evidence_support_probability": evidence,
                    "minimum_full_execution_probability": execution,
                    "economics": summary,
                    "selected_support": {
                        "mean_evidence_support_probability": (
                            sum(evidence_values) / len(evidence_values)
                            if evidence_values else None
                        ),
                        "mean_full_execution_probability": (
                            sum(execution_values) / len(execution_values)
                            if execution_values else None
                        ),
                    },
                })
        model.minimum_evidence_support_probability = None
        model.minimum_full_execution_probability = None
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
