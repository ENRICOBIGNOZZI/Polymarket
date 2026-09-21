"""Latency and effective-signal-age frontier for Direct Action PAPER research."""
from __future__ import annotations

from collections import defaultdict

from research.walk_forward_v2.core import SAFETY, folds
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    configured_operational_latency_floor_ms,
    evaluate_direct_action_policy,
    summarize_direct_action,
)

SCHEMA = "polymarket_direct_action_latency_age_frontier_v1"
DEFAULT_LATENCIES_MS = (25, 50, 100, 250)
DEFAULT_EFFECTIVE_AGE_GATES_MS = (None, 100.0, 150.0, 250.0, 500.0)


def operational_support_summary(outcomes, rows_by_decision):
    trades = [row for row in outcomes if row.get("action") == "TRADE"]
    known = supported = 0
    gaps = []
    effective_ages = []
    for outcome in trades:
        source = rows_by_decision.get(int(outcome["decision_ns"]))
        if source is None:
            continue
        floor = configured_operational_latency_floor_ms(source)
        if floor is not None:
            known += 1
            gap = float(outcome["latency_ms"]) - float(floor)
            gaps.append(gap)
            if gap >= -1e-12:
                supported += 1
        age = outcome.get("effective_signal_age_ms")
        if isinstance(age, (int, float)):
            effective_ages.append(float(age))
    return {
        "selected_trades": len(trades),
        "operational_floor_known_trades": known,
        "operationally_supported_trades": supported,
        "operational_support_fraction_when_known": (
            supported / known if known else None
        ),
        "mean_research_minus_operational_floor_ms": (
            sum(gaps) / len(gaps) if gaps else None
        ),
        "max_effective_signal_age_ms": (
            max(effective_ages) if effective_ages else None
        ),
        "mean_effective_signal_age_ms": (
            sum(effective_ages) / len(effective_ages)
            if effective_ages else None
        ),
    }


def latency_age_frontier(
    records,
    *,
    desired_folds=3,
    latencies_ms=DEFAULT_LATENCIES_MS,
    effective_age_gates_ms=DEFAULT_EFFECTIVE_AGE_GATES_MS,
    capital_budget=10_000.0,
    model_kwargs=None,
):
    latencies = tuple(sorted({int(value) for value in latencies_ms}))
    if not latencies or any(value < 0 for value in latencies):
        raise ValueError("nonnegative latency grid required")
    gates = []
    for value in effective_age_gates_ms:
        if value is None:
            gates.append(None)
        else:
            value = float(value)
            if value <= 0:
                raise ValueError("positive effective-age gate required")
            gates.append(value)

    found, receipt = folds(records, desired_folds=desired_folds)
    result = {
        "schema": SCHEMA,
        **SAFETY,
        "state": receipt.get("state"),
        "fold_receipt": receipt,
        "latencies_ms": list(latencies),
        "effective_age_gates_ms": gates,
        "capital_budget": float(capital_budget),
        "selection": "NONE_RESEARCH_FRONTIER_ONLY",
        "mean_covariance_estimation": False,
        "folds": [],
    }
    if not found:
        return result

    for fold in found:
        kwargs = dict(model_kwargs or {})
        kwargs["train_latencies_ms"] = latencies
        kwargs["maximum_effective_signal_age_ms"] = None
        model = DirectActionValueModel(**kwargs).fit(
            fold["train_repricing"])
        rows_by_decision = {
            int(row["decision_ns"]): row for row in fold["test"]
        }
        cells = []
        for latency in latencies:
            for gate in gates:
                model.maximum_effective_signal_age_ms = gate
                outcomes = evaluate_direct_action_policy(
                    model,
                    fold["test"],
                    latency_ms=latency,
                    capital_budget=capital_budget,
                    one_entry_per_market=True,
                    live_geometry=True,
                )
                cells.append({
                    "latency_ms": latency,
                    "maximum_effective_signal_age_ms": gate,
                    "economics": summarize_direct_action(outcomes),
                    "operational_support": operational_support_summary(
                        outcomes, rows_by_decision),
                })
        model.maximum_effective_signal_age_ms = None
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
