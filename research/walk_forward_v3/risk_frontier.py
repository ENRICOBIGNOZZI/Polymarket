"""Empirical portfolio tail-risk diagnostics for Direct Action PAPER research.

No mean vector, covariance matrix, Gaussian approximation, Sharpe objective, or
automatic live promotion is used here.  Risk is measured from the realized OOS
cash-PnL path produced by the same executable/censored Direct Action replay.

The intended use is a frontier: maximize net dollars subject to a human-chosen
tail-risk/drawdown budget.  The module deliberately does not invent that budget.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import argparse
import json
import math
from pathlib import Path

from research.walk_forward_v2.core import SAFETY, atomic_json, folds
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    FrictionPolicy,
    evaluate_direct_action_policy,
    summarize_direct_action,
)

SCHEMA = "polymarket_direct_action_empirical_risk_v1"


def _quantile(values, probability):
    values = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(float(probability) * len(values)) - 1))
    return values[index]


def empirical_var_cvar_from_losses(losses, alpha):
    """Positive loss convention; empirical upper-tail VaR/CVaR."""
    values = sorted(float(value) for value in losses if math.isfinite(float(value)))
    if not values:
        return {"var": None, "cvar": None, "tail_count": 0}
    var = _quantile(values, alpha)
    tail = [value for value in values if value >= var - 1e-15]
    return {
        "var": float(var),
        "cvar": float(sum(tail) / len(tail)),
        "tail_count": len(tail),
    }


def _max_drawdown(exit_events):
    """Dollar drawdown on PnL booked at the modeled exit time."""
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    trough_event = None
    for event in sorted(exit_events, key=lambda item: (item["exit_ns"], item["market_id"])):
        equity += float(event["pnl"])
        peak = max(peak, equity)
        drawdown = peak - equity
        if drawdown > maximum:
            maximum = drawdown
            trough_event = {
                "exit_ns": int(event["exit_ns"]),
                "market_id": str(event["market_id"]),
                "equity": float(equity),
                "peak": float(peak),
            }
    return float(maximum), trough_event


def scenario_risk_metrics(outcomes, *, block_ms=None, alpha_levels=(0.95, 0.99)):
    """Measure OOS tail risk with empirical overlapping-risk scenarios.

    A scenario block defaults to the largest modeled holding horizon among
    selected trades, so positions that can overlap economically are clustered
    instead of treated as independent rows.
    """
    trades = [row for row in outcomes if row.get("action") == "TRADE"]
    censored = [row for row in trades if row.get("realized_pnl") is None]
    observed = [row for row in trades if row.get("realized_pnl") is not None]

    horizons = [
        int(row.get("exit_horizon_ms") or 0)
        for row in trades
        if int(row.get("exit_horizon_ms") or 0) > 0
    ]
    if block_ms is None:
        block_ms = max(horizons, default=2000)
    block_ms = int(block_ms)
    if block_ms <= 0:
        raise ValueError("positive risk scenario block required")
    block_ns = block_ms * 1_000_000

    exit_events = []
    block_pnl = defaultdict(float)
    block_trade_count = defaultdict(int)
    by_asset_pnl = defaultdict(float)
    by_side_pnl = defaultdict(float)
    by_asset_notional = defaultdict(float)
    by_side_notional = defaultdict(float)

    for row in observed:
        horizon_ms = int(row.get("exit_horizon_ms") or 0)
        decision_ns = int(row.get("decision_ns") or 0)
        if decision_ns <= 0 or horizon_ms <= 0:
            continue
        pnl = float(row["realized_pnl"])
        exit_ns = decision_ns + horizon_ms * 1_000_000
        block = exit_ns // block_ns
        exit_events.append({
            "exit_ns": exit_ns,
            "market_id": str(row.get("market_id") or ""),
            "pnl": pnl,
        })
        block_pnl[block] += pnl
        block_trade_count[block] += 1
        asset = str(row.get("asset") or "UNKNOWN")
        side = str(row.get("side") or "SELECTED")
        notional = float(row.get("notional") or 0.0)
        by_asset_pnl[asset] += pnl
        by_side_pnl[side] += pnl
        by_asset_notional[asset] += notional
        by_side_notional[side] += notional

    scenarios = [float(value) for _, value in sorted(block_pnl.items())]
    losses = [-value for value in scenarios]
    drawdown, trough = _max_drawdown(exit_events)
    total_pnl = sum(float(row["realized_pnl"]) for row in observed)
    positive_blocks = sum(value > 0 for value in scenarios)
    negative_blocks = sum(value < 0 for value in scenarios)

    tails = {
        str(alpha): empirical_var_cvar_from_losses(losses, alpha)
        for alpha in alpha_levels
    }

    state = (
        "NO_TRADES"
        if not trades
        else "ALL_SELECTED_TRADES_OBSERVED"
        if not censored
        else "PARTIAL_CENSORED_NO_PROMOTION_CLAIM"
    )
    return {
        "schema": SCHEMA + "_metrics",
        **SAFETY,
        "state": state,
        "block_ms": block_ms,
        "scenario_semantics": "NONOVERLAPPING_EXIT_PNL_BLOCKS_AT_MAX_SELECTED_HOLDING_HORIZON",
        "selected_trades": len(trades),
        "observed_selected_trades": len(observed),
        "censored_selected_trades": len(censored),
        "scenario_blocks": len(scenarios),
        "positive_scenario_blocks": positive_blocks,
        "negative_scenario_blocks": negative_blocks,
        "total_observed_net_pnl": float(total_pnl) if observed else None,
        "mean_scenario_pnl": (
            float(sum(scenarios) / len(scenarios)) if scenarios else None
        ),
        "worst_scenario_pnl": min(scenarios) if scenarios else None,
        "best_scenario_pnl": max(scenarios) if scenarios else None,
        "max_drawdown": drawdown if exit_events else None,
        "max_drawdown_trough": trough,
        "tail_loss": tails,
        "max_active_positions": max(
            (int(row.get("replay_max_active_positions") or 0) for row in outcomes),
            default=0,
        ),
        "max_gross_notional": max(
            (float(row.get("replay_max_gross_notional") or 0.0) for row in outcomes),
            default=0.0,
        ),
        "by_asset_pnl": dict(sorted(by_asset_pnl.items())),
        "by_side_pnl": dict(sorted(by_side_pnl.items())),
        "by_asset_selected_notional": dict(sorted(by_asset_notional.items())),
        "by_side_selected_notional": dict(sorted(by_side_notional.items())),
    }


def _dominates(left, right):
    """Pareto dominance: more dollars, no worse CVaR95 and drawdown."""
    lm, rm = left["risk"], right["risk"]
    if (
        lm["total_observed_net_pnl"] is None
        or rm["total_observed_net_pnl"] is None
        or lm["max_drawdown"] is None
        or rm["max_drawdown"] is None
    ):
        return False
    lc = lm["tail_loss"]["0.95"]["cvar"]
    rc = rm["tail_loss"]["0.95"]["cvar"]
    if lc is None or rc is None:
        return False
    better_or_equal = (
        lm["total_observed_net_pnl"] >= rm["total_observed_net_pnl"]
        and lc <= rc
        and lm["max_drawdown"] <= rm["max_drawdown"]
    )
    strictly_better = (
        lm["total_observed_net_pnl"] > rm["total_observed_net_pnl"]
        or lc < rc
        or lm["max_drawdown"] < rm["max_drawdown"]
    )
    return better_or_equal and strictly_better


def pareto_frontier(entries):
    """Return policy IDs that are not empirically dominated."""
    admissible = [
        entry for entry in entries
        if entry.get("risk", {}).get("state") == "ALL_SELECTED_TRADES_OBSERVED"
    ]
    output = []
    for candidate in admissible:
        if any(
            other is not candidate and _dominates(other, candidate)
            for other in admissible
        ):
            continue
        output.append(str(candidate["policy_id"]))
    return output



def select_policy_under_risk_budget(
    frontier,
    *,
    max_cvar95=None,
    max_drawdown=None,
):
    """Choose max net dollars only after a risk budget has been supplied.

    Intended for train/validation data only.  With no explicit budget, no
    selection is made: human agency over risk appetite is preserved.
    """
    if max_cvar95 is None and max_drawdown is None:
        return {
            "state": "NO_RISK_BUDGET_NO_AUTOMATIC_SELECTION",
            "policy_id": None,
        }
    if max_cvar95 is not None and (
        not math.isfinite(float(max_cvar95)) or float(max_cvar95) < 0
    ):
        raise ValueError("nonnegative finite max_cvar95 required")
    if max_drawdown is not None and (
        not math.isfinite(float(max_drawdown)) or float(max_drawdown) < 0
    ):
        raise ValueError("nonnegative finite max_drawdown required")

    admissible = []
    for entry in frontier.get("entries") or []:
        risk = entry.get("risk") or {}
        if risk.get("state") != "ALL_SELECTED_TRADES_OBSERVED":
            continue
        pnl = risk.get("total_observed_net_pnl")
        drawdown = risk.get("max_drawdown")
        cvar = ((risk.get("tail_loss") or {}).get("0.95") or {}).get("cvar")
        if pnl is None or drawdown is None or cvar is None:
            continue
        if max_cvar95 is not None and float(cvar) > float(max_cvar95) + 1e-15:
            continue
        if max_drawdown is not None and float(drawdown) > float(max_drawdown) + 1e-15:
            continue
        admissible.append(entry)

    if not admissible:
        return {
            "state": "NO_POLICY_MEETS_VALIDATION_RISK_BUDGET",
            "policy_id": None,
        }
    admissible.sort(
        key=lambda entry: (
            float(entry["risk"]["total_observed_net_pnl"]),
            -float(entry["risk"]["tail_loss"]["0.95"]["cvar"]),
            -float(entry["risk"]["max_drawdown"]),
            str(entry["policy_id"]),
        ),
        reverse=True,
    )
    chosen = admissible[0]
    return {
        "state": "VALIDATION_POLICY_SELECTED",
        "policy_id": str(chosen["policy_id"]),
        "validation_net_pnl": float(chosen["risk"]["total_observed_net_pnl"]),
        "validation_cvar95": float(chosen["risk"]["tail_loss"]["0.95"]["cvar"]),
        "validation_max_drawdown": float(chosen["risk"]["max_drawdown"]),
        "max_cvar95": None if max_cvar95 is None else float(max_cvar95),
        "max_drawdown": None if max_drawdown is None else float(max_drawdown),
    }


def default_policy_grid():
    """Predeclared exploration grid; it is a frontier, not a risk preference."""
    grid = []
    for uncertainty in (1.0, 1.5, 2.0):
        for concentration in (0.0, 0.001, 0.01, 0.1):
            grid.append({
                "policy_id": f"u{uncertainty:g}_c{concentration:g}",
                "friction_policy": FrictionPolicy(
                    uncertainty_aversion=uncertainty,
                    asset_concentration_lambda=concentration,
                    common_factor_concentration_lambda=concentration,
                ),
            })
    return grid


def evaluate_empirical_risk_frontier(
    model,
    rows,
    *,
    policy_grid=None,
    latency_ms=50,
    capital_budget=10_000.0,
    one_entry_per_market=True,
    live_geometry=True,
):
    """Evaluate predeclared risk policies on one fixed OOS set.

    This function never chooses a human risk tolerance.  It exposes the
    dollars/tail-risk frontier for downstream train-only policy selection.
    """
    policies = list(policy_grid or default_policy_grid())
    original_policy = model.friction_policy
    entries = []
    try:
        for spec in policies:
            policy = spec["friction_policy"]
            if not isinstance(policy, FrictionPolicy):
                raise TypeError("FrictionPolicy required")
            model.friction_policy = policy.validated()
            outcomes = evaluate_direct_action_policy(
                model,
                rows,
                latency_ms=latency_ms,
                capital_budget=capital_budget,
                one_entry_per_market=one_entry_per_market,
                live_geometry=live_geometry,
            )
            entries.append({
                "policy_id": str(spec["policy_id"]),
                "friction_policy": asdict(policy),
                "economics": summarize_direct_action(outcomes),
                "risk": scenario_risk_metrics(outcomes),
            })
    finally:
        model.friction_policy = original_policy

    return {
        "schema": SCHEMA + "_frontier",
        **SAFETY,
        "state": "READY",
        "selection": "NONE_HUMAN_RISK_BUDGET_REQUIRED",
        "mean_covariance_estimation": False,
        "gaussian_risk_assumption": False,
        "policy_count": len(entries),
        "pareto_policy_ids": pareto_frontier(entries),
        "entries": entries,
    }



_NO_ACTION_SUPPORT_ERRORS = {
    "direct action training rows required",
    "no admissible direct-action training markets",
    "streaming ridge received zero rows",
}


def _fit_direct_action_with_support(rows, model_kwargs):
    try:
        return DirectActionValueModel(**(model_kwargs or {})).fit(rows), None
    except ValueError as exc:
        if str(exc) not in _NO_ACTION_SUPPORT_ERRORS:
            raise
        return None, str(exc)


def nested_walk_forward_risk_frontier(
    records,
    *,
    desired_folds=3,
    inner_desired_folds=2,
    policy_grid=None,
    latency_ms=50,
    capital_budget=10_000.0,
    model_kwargs=None,
    max_cvar95=None,
    max_drawdown=None,
):
    """Nested, leakage-safe risk-policy evaluation.

    Inner historical validation decides either:
    - one policy satisfying an explicit CVaR/drawdown budget, or
    - a Pareto survivor set when no human risk budget is supplied.

    Only those validation-qualified policies reach the outer OOS fold.
    """
    policies = list(policy_grid or default_policy_grid())
    policy_by_id = {str(spec["policy_id"]): spec for spec in policies}
    outer_folds, outer_receipt = folds(records, desired_folds=desired_folds)
    result = {
        "schema": SCHEMA + "_nested_walk_forward",
        **SAFETY,
        "state": outer_receipt.get("state"),
        "mean_covariance_estimation": False,
        "gaussian_risk_assumption": False,
        "outer_fold_receipt": outer_receipt,
        "max_cvar95": max_cvar95,
        "max_drawdown": max_drawdown,
        "folds": [],
    }
    if not outer_folds:
        return result

    for outer in outer_folds:
        outer_train = list(outer["train_repricing"])
        outer_test = list(outer["test"])
        inner_folds, inner_receipt = folds(
            outer_train, desired_folds=inner_desired_folds)

        fold_result = {
            "fold": outer["fold"],
            "cutoff_ns": outer["cutoff_ns"],
            "outer_train_markets": len(outer["train_markets"]),
            "outer_test_markets": len(outer["test_markets"]),
            "inner_receipt": inner_receipt,
            "validation": None,
            "selection": None,
            "outer_oos": None,
        }
        if not inner_folds:
            fold_result["selection"] = {
                "state": "INSUFFICIENT_INNER_FOLDS_NO_POLICY_SELECTION",
                "policy_id": None,
            }
            result["folds"].append(fold_result)
            continue

        # Prefer the latest chronological inner fold, but causal evidence can
        # be sparse enough that a nonempty state block still yields zero
        # executable action targets. Walk backward until the latest fit-able
        # validation split; never convert missing support into zero PnL.
        inner = None
        inner_model = None
        inner_fit_failures = []
        for candidate in reversed(inner_folds):
            candidate_model, support_error = _fit_direct_action_with_support(
                candidate["train_repricing"], model_kwargs)
            if candidate_model is not None:
                inner = candidate
                inner_model = candidate_model
                break
            inner_fit_failures.append({
                "fold": candidate["fold"],
                "reason": support_error,
                "train_markets": len(candidate["train_markets"]),
                "test_markets": len(candidate["test_markets"]),
            })
        if inner_model is None:
            fold_result["selection"] = {
                "state": "INSUFFICIENT_INNER_ACTION_TARGETS_NO_POLICY_SELECTION",
                "policy_id": None,
                "qualification": "NO_EXECUTABLE_ACTION_TARGETS_IN_ANY_INNER_TRAIN_BLOCK",
                "qualified_policy_ids": [],
            }
            fold_result["inner_fit_failures"] = inner_fit_failures
            fold_result["outer_oos"] = {
                "schema": SCHEMA + "_frontier",
                **SAFETY,
                "state": "NOT_EVALUATED_NO_VALIDATION_MODEL",
                "selection": "NONE",
                "policy_count": 0,
                "pareto_policy_ids": [],
                "entries": [],
            }
            result["folds"].append(fold_result)
            continue

        fold_result["inner_fit_failures"] = inner_fit_failures
        fold_result["inner_selected_fold"] = inner["fold"]
        validation_frontier = evaluate_empirical_risk_frontier(
            inner_model,
            inner["test"],
            policy_grid=policies,
            latency_ms=latency_ms,
            capital_budget=capital_budget,
            one_entry_per_market=True,
            live_geometry=True,
        )
        selection = select_policy_under_risk_budget(
            validation_frontier,
            max_cvar95=max_cvar95,
            max_drawdown=max_drawdown,
        )

        if selection["policy_id"] is not None:
            qualified_ids = [str(selection["policy_id"])]
            qualification = "EXPLICIT_VALIDATION_RISK_BUDGET"
        elif selection["state"] == "NO_RISK_BUDGET_NO_AUTOMATIC_SELECTION":
            qualified_ids = list(validation_frontier["pareto_policy_ids"])
            qualification = "VALIDATION_PARETO_SET_NO_SINGLE_SELECTION"
        else:
            qualified_ids = []
            qualification = "NO_VALIDATION_POLICY_QUALIFIED"

        qualified_grid = [
            policy_by_id[policy_id]
            for policy_id in qualified_ids
            if policy_id in policy_by_id
        ]

        outer_model, outer_support_error = _fit_direct_action_with_support(
            outer_train, model_kwargs)
        if qualified_grid and outer_model is not None:
            outer_oos = evaluate_empirical_risk_frontier(
                outer_model,
                outer_test,
                policy_grid=qualified_grid,
                latency_ms=latency_ms,
                capital_budget=capital_budget,
                one_entry_per_market=True,
                live_geometry=True,
            )
        elif qualified_grid:
            outer_oos = {
                "schema": SCHEMA + "_frontier",
                **SAFETY,
                "state": "INSUFFICIENT_OUTER_ACTION_TARGETS",
                "support_error": outer_support_error,
                "selection": "NONE",
                "policy_count": 0,
                "pareto_policy_ids": [],
                "entries": [],
            }
        else:
            outer_oos = {
                "schema": SCHEMA + "_frontier",
                **SAFETY,
                "state": "NO_VALIDATION_QUALIFIED_POLICY",
                "selection": "NONE",
                "policy_count": 0,
                "pareto_policy_ids": [],
                "entries": [],
            }

        fold_result["validation"] = validation_frontier
        fold_result["selection"] = {
            **selection,
            "qualification": qualification,
            "qualified_policy_ids": qualified_ids,
        }
        fold_result["outer_oos"] = outer_oos
        result["folds"].append(fold_result)

    result["state"] = "READY"
    result["selection_semantics"] = (
        "INNER_VALIDATION_ONLY_OUTER_OOS_NEVER_USED_TO_CHOOSE_RISK_POLICY"
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outcomes", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--block-ms", type=int, default=None)
    args = parser.parse_args(argv)
    value = json.loads(args.outcomes.read_text(encoding="utf-8"))
    outcomes = value.get("outcomes") if isinstance(value, dict) else value
    if not isinstance(outcomes, list):
        raise ValueError("outcomes list required")
    atomic_json(args.output, scenario_risk_metrics(outcomes, block_ms=args.block_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
