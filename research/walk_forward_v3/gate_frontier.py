"""Leakage-safe policy-gate selection for Direct Action.

The learned cash-PnL model is fit once per fold.  Age, regime-support,
side-support and epistemic-support gates are policy decisions applied after fit.
They are chosen only on an inner historical validation fold and then frozen for
outer OOS evaluation.
"""
from __future__ import annotations

from contextlib import contextmanager
import math

from research.walk_forward_v2.core import SAFETY, folds
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    evaluate_direct_action_policy,
    summarize_direct_action,
)
from research.walk_forward_v3.risk_frontier import scenario_risk_metrics


SCHEMA = "polymarket_direct_action_gate_frontier_v1"

GATE_POLICIES = (
    {
        "policy_id": "OPEN_DIAGNOSTIC",
        "maximum_effective_action_age_ms": None,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "AGE100",
        "maximum_effective_action_age_ms": 100.0,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "AGE250",
        "maximum_effective_action_age_ms": 250.0,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "ROBUST_SUPPORT",
        "maximum_effective_action_age_ms": None,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "ROBUST_WORST_CASE",
    },
    {
        "policy_id": "REGIME8",
        "maximum_effective_action_age_ms": None,
        "minimum_regime_action_targets": 8,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "REGIME32",
        "maximum_effective_action_age_ms": None,
        "minimum_regime_action_targets": 32,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "SIDE2",
        "maximum_effective_action_age_ms": None,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 2,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "SIDE5",
        "maximum_effective_action_age_ms": None,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 5,
        "support_policy_mode": "DIAGNOSTIC",
    },
    {
        "policy_id": "AGE100_ROBUST",
        "maximum_effective_action_age_ms": 100.0,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "ROBUST_WORST_CASE",
    },
    {
        "policy_id": "AGE250_ROBUST",
        "maximum_effective_action_age_ms": 250.0,
        "minimum_regime_action_targets": 0,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "ROBUST_WORST_CASE",
    },
    {
        "policy_id": "AGE250_REGIME8_ROBUST",
        "maximum_effective_action_age_ms": 250.0,
        "minimum_regime_action_targets": 8,
        "minimum_side_regime_action_targets": 0,
        "support_policy_mode": "ROBUST_WORST_CASE",
    },
    {
        "policy_id": "ALL_GATES_BALANCED",
        "maximum_effective_action_age_ms": 250.0,
        "minimum_regime_action_targets": 8,
        "minimum_side_regime_action_targets": 2,
        "support_policy_mode": "ROBUST_WORST_CASE",
    },
)


@contextmanager
def gate_policy(model, spec):
    names = (
        "maximum_effective_action_age_ms",
        "minimum_regime_action_targets",
        "minimum_side_regime_action_targets",
        "support_policy_mode",
    )
    original = {name: getattr(model, name) for name in names}
    try:
        for name in names:
            setattr(model, name, spec[name])
        yield
    finally:
        for name, value in original.items():
            setattr(model, name, value)


def evaluate_gate(model, rows, spec, *, latency_ms=50,
                  capital_budget=10_000.0):
    with gate_policy(model, spec):
        outcomes = evaluate_direct_action_policy(
            model,
            rows,
            latency_ms=latency_ms,
            capital_budget=capital_budget,
            one_entry_per_market=True,
            live_geometry=True,
        )
    risk = scenario_risk_metrics(outcomes)
    robust = risk.get("censored_worst_case") or {}
    return {
        "policy_id": spec["policy_id"],
        "policy": dict(spec),
        "economics": summarize_direct_action(outcomes),
        "risk": risk,
        "robust_total_pnl_lower_bound": (
            robust.get("total_net_pnl_lower_bound")
            if robust.get("state") == "READY" else None
        ),
    }


def select_gate_from_validation(entries):
    """Select max causal lower-bound PnL, with NO_TRADE baseline = 0."""
    usable = [
        entry for entry in entries
        if entry.get("robust_total_pnl_lower_bound") is not None
        and math.isfinite(float(entry["robust_total_pnl_lower_bound"]))
    ]
    if not usable:
        return {
            "state": "NO_VALIDATION_POLICY_WITH_CAUSAL_LOWER_BOUND",
            "policy_id": None,
            "selected_validation_lower_bound": 0.0,
        }
    usable.sort(
        key=lambda entry: (
            float(entry["robust_total_pnl_lower_bound"]),
            float(entry["risk"].get("total_observed_net_pnl") or 0.0),
            -int(entry["risk"].get("selected_trades") or 0),
            str(entry["policy_id"]),
        ),
        reverse=True,
    )
    best = usable[0]
    if float(best["robust_total_pnl_lower_bound"]) <= 0.0:
        return {
            "state": "NO_TRADE_DOMINATES_VALIDATION_LOWER_BOUND",
            "policy_id": None,
            "selected_validation_lower_bound": 0.0,
            "best_candidate_policy_id": best["policy_id"],
            "best_candidate_lower_bound": float(
                best["robust_total_pnl_lower_bound"]),
        }
    return {
        "state": "VALIDATION_GATE_POLICY_SELECTED",
        "policy_id": best["policy_id"],
        "selected_validation_lower_bound": float(
            best["robust_total_pnl_lower_bound"]),
        "selected_validation_observed_pnl": (
            best["risk"].get("total_observed_net_pnl")
        ),
    }


_NO_SUPPORT = {
    "direct action training rows required",
    "no admissible direct-action training markets",
    "streaming ridge received zero rows",
}


def fit_model(rows, kwargs):
    try:
        return DirectActionValueModel(**(kwargs or {})).fit(rows), None
    except ValueError as exc:
        if str(exc) not in _NO_SUPPORT:
            raise
        return None, str(exc)


def nested_gate_frontier(
    records,
    *,
    outer_folds=3,
    inner_folds=2,
    latency_ms=50,
    capital_budget=10_000.0,
    model_kwargs=None,
    gate_policies=None,
):
    policies = tuple(gate_policies or GATE_POLICIES)
    outer, receipt = folds(records, desired_folds=outer_folds)
    result = {
        "schema": SCHEMA,
        **SAFETY,
        "state": receipt.get("state"),
        "selection_semantics": (
            "INNER_VALIDATION_ONLY;OUTER_OOS_NEVER_USED_TO_CHOOSE_GATE"
        ),
        "validation_objective": (
            "MAX_CAUSAL_CENSORED_WORST_CASE_TOTAL_PNL_LOWER_BOUND;"
            "NO_TRADE_BASELINE_ZERO"
        ),
        "policy_grid": [dict(spec) for spec in policies],
        "folds": [],
    }
    if not outer:
        return result

    for outer_fold in outer:
        inner, inner_receipt = folds(
            outer_fold["train_repricing"],
            desired_folds=inner_folds,
        )
        chosen_inner = None
        inner_failures = []
        model = None

        # Most recent estimable inner fold is the validation cut.
        for candidate in reversed(inner):
            model, failure = fit_model(
                candidate["train_repricing"], model_kwargs)
            if model is not None:
                chosen_inner = candidate
                break
            inner_failures.append({
                "fold": candidate["fold"],
                "reason": failure,
            })

        validation_entries = []
        selection = {
            "state": "INSUFFICIENT_INNER_ACTION_SUPPORT",
            "policy_id": None,
            "selected_validation_lower_bound": 0.0,
        }
        if chosen_inner is not None and model is not None:
            validation_entries = [
                evaluate_gate(
                    model,
                    chosen_inner["test"],
                    spec,
                    latency_ms=latency_ms,
                    capital_budget=capital_budget,
                )
                for spec in policies
            ]
            selection = select_gate_from_validation(validation_entries)

        outer_model, outer_failure = fit_model(
            outer_fold["train_repricing"], model_kwargs)
        outer_evaluation = {
            "state": "NOT_EVALUATED_NO_SELECTED_GATE",
            "policy_id": selection.get("policy_id"),
        }
        if outer_model is None:
            outer_evaluation = {
                "state": "OUTER_MODEL_UNAVAILABLE",
                "reason": outer_failure,
                "policy_id": selection.get("policy_id"),
            }
        elif selection.get("policy_id") is None:
            # Explicit no-trade choice selected by validation.
            outer_evaluation = {
                "state": "NO_TRADE_SELECTED_BY_VALIDATION",
                "policy_id": None,
                "selected_trades": 0,
                "observed_pnl": 0.0,
                "robust_total_pnl_lower_bound": 0.0,
            }
        else:
            spec = next(
                item for item in policies
                if item["policy_id"] == selection["policy_id"]
            )
            outer_entry = evaluate_gate(
                outer_model,
                outer_fold["test"],
                spec,
                latency_ms=latency_ms,
                capital_budget=capital_budget,
            )
            outer_evaluation = {
                "state": "READY",
                **outer_entry,
            }

        result["folds"].append({
            "fold": outer_fold["fold"],
            "cutoff_ns": outer_fold["cutoff_ns"],
            "outer_train_markets": len(outer_fold["train_markets"]),
            "outer_test_markets": len(outer_fold["test_markets"]),
            "inner_fold_receipt": inner_receipt,
            "inner_selected_fold": (
                chosen_inner["fold"] if chosen_inner else None),
            "inner_fit_failures": inner_failures,
            "validation_entries": validation_entries,
            "selection": selection,
            "outer_oos": outer_evaluation,
        })

    result["state"] = "READY"
    return result
