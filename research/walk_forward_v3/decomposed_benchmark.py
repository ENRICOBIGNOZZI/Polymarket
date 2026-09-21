"""Chronological PAPER horse race for Direct Action challengers.

This module is research-only. It evaluates the frozen baseline, the current
trade-frequency/sizing challenger and the decomposed fill x conditional-PnL
challenger on exactly the same chronological OOS folds.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from research.walk_forward_v2.core import (
    SAFETY,
    atomic_json,
    build_dataset,
    folds,
)
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    evaluate_direct_action_policy,
    load_trade_frequency_challenger_config,
    summarize_direct_action,
)
from research.walk_forward_v3.decomposed_action import DecomposedActionValueModel
from research.walk_forward_v3.risk_frontier import scenario_risk_metrics


SCHEMA = "polymarket_v7_decomposed_action_horse_race_v1"


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _quantile(values, level):
    values = sorted(float(value) for value in values if _finite(value))
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(math.ceil(level * len(values))) - 1))
    return values[index]


def performance_snapshot(outcomes):
    rows = list(outcomes)
    summary = summarize_direct_action(rows)
    trades = [row for row in rows if row.get("action") == "TRADE"]
    observed = [
        row for row in trades if _finite(row.get("realized_pnl"))
    ]
    fills = [
        row for row in trades
        if str(row.get("target_state") or "") in {
            "OBSERVED_FULL_FILL", "OBSERVED_PARTIAL_FILL"
        }
    ]
    notionals = [
        float(row.get("notional"))
        for row in trades if _finite(row.get("notional"))
    ]
    turnover = sum(notionals)

    decision_times = [
        int(row.get("decision_ns"))
        for row in rows
        if isinstance(row.get("decision_ns"), int)
    ]
    span_days = None
    if len(decision_times) >= 2:
        span_ns = max(decision_times) - min(decision_times)
        if span_ns > 0:
            span_days = span_ns / 86_400_000_000_000

    pnl = summary.get("total_observed_net_pnl")
    pnl_day = (
        float(pnl) / span_days
        if _finite(pnl) and span_days is not None and span_days > 0
        else None
    )
    trades_day = (
        len(trades) / span_days
        if span_days is not None and span_days > 0
        else None
    )
    pnl_turnover = (
        float(pnl) / turnover
        if _finite(pnl) and turnover > 0
        else None
    )
    risk = scenario_risk_metrics(rows)
    tail95 = ((risk.get("tail_loss") or {}).get("0.95") or {})
    tail99 = ((risk.get("tail_loss") or {}).get("0.99") or {})

    return {
        "schema": SCHEMA + "_performance_v1",
        **SAFETY,
        "opportunities": len(rows),
        "selected_trades": len(trades),
        "trade_rate": len(trades) / len(rows) if rows else None,
        "selected_trades_per_day": trades_day,
        "observed_selected_trades": len(observed),
        "censored_selected_trades": len(trades) - len(observed),
        "fill_count": len(fills),
        "fill_rate_given_selected": len(fills) / len(trades) if trades else None,
        "positive_observed_trades": sum(
            float(row["realized_pnl"]) > 0 for row in observed),
        "zero_observed_trades": sum(
            abs(float(row["realized_pnl"])) <= 1e-15 for row in observed),
        "negative_observed_trades": sum(
            float(row["realized_pnl"]) < 0 for row in observed),
        "total_observed_net_pnl": pnl,
        "net_pnl_per_day": pnl_day,
        "mean_observed_net_pnl": summary.get("mean_observed_net_pnl"),
        "turnover_notional": turnover,
        "net_pnl_per_dollar_turnover": pnl_turnover,
        "mean_selected_notional": (
            sum(notionals) / len(notionals) if notionals else None),
        "p50_selected_notional": _quantile(notionals, 0.50),
        "p90_selected_notional": _quantile(notionals, 0.90),
        "max_selected_notional": max(notionals) if notionals else None,
        "max_active_positions": summary.get("max_active_positions"),
        "max_gross_notional": summary.get("max_gross_notional"),
        "max_drawdown": risk.get("max_drawdown"),
        "var95": tail95.get("var"),
        "cvar95": tail95.get("cvar"),
        "var99": tail99.get("var"),
        "cvar99": tail99.get("cvar"),
        "risk": risk,
        "summary": summary,
    }


def _comparison(reference, challenger):
    keys = (
        "selected_trades",
        "selected_trades_per_day",
        "fill_count",
        "total_observed_net_pnl",
        "net_pnl_per_day",
        "net_pnl_per_dollar_turnover",
        "max_drawdown",
        "cvar95",
        "cvar99",
        "turnover_notional",
        "max_gross_notional",
    )
    result = {}
    for key in keys:
        left = reference.get(key)
        right = challenger.get(key)
        result[key + "_baseline"] = left
        result[key + "_challenger"] = right
        result[key + "_delta"] = (
            float(right) - float(left)
            if _finite(left) and _finite(right)
            else None
        )
    return result


def walk_forward_horse_race(
    records,
    *,
    desired_folds=3,
    latency_ms=50,
    capital_budget=10_000.0,
    challenger_config,
):
    found, receipt = folds(records, desired_folds=desired_folds)
    result = {
        "schema": SCHEMA,
        **SAFETY,
        "state": receipt.get("state"),
        "selection": "NONE_RESEARCH_ONLY_NO_RUNTIME_PROMOTION",
        "automatic_promotion": False,
        "latency_ms": int(latency_ms),
        "capital_budget": float(capital_budget),
        "fold_receipt": receipt,
        "policies": {
            policy: {"folds": []}
            for policy in (
                "A0_BASELINE",
                "A1_DIRECT_CHALLENGER",
                "A2_DECOMPOSED",
            )
        },
    }
    if not found:
        return result

    sizing = challenger_config["sizing_policy"].validated()
    challenger_kwargs = dict(challenger_config["model_kwargs"])
    entry_policy = challenger_config["entry_policy"]
    all_outcomes = {key: [] for key in result["policies"]}

    for fold in found:
        train = fold["train_repricing"]
        test = fold["test"]

        baseline = DirectActionValueModel().fit(train)
        baseline_outcomes = evaluate_direct_action_policy(
            baseline,
            test,
            latency_ms=latency_ms,
            capital_budget=capital_budget,
            one_entry_per_market=True,
            live_geometry=True,
        )

        direct = DirectActionValueModel(**challenger_kwargs).fit(train)
        direct_outcomes = evaluate_direct_action_policy(
            direct,
            test,
            latency_ms=latency_ms,
            capital_budget=capital_budget,
            live_geometry=True,
            entry_policy=entry_policy,
            sizing_policy=sizing,
            max_market_exposure=float(capital_budget) / sizing.context_count,
        )

        decomposed = DecomposedActionValueModel(**challenger_kwargs).fit(
            train, fitted_base=direct)
        decomposed_outcomes = evaluate_direct_action_policy(
            decomposed,
            test,
            latency_ms=latency_ms,
            capital_budget=capital_budget,
            live_geometry=True,
            entry_policy=entry_policy,
            sizing_policy=sizing,
            max_market_exposure=float(capital_budget) / sizing.context_count,
        )

        entries = (
            ("A0_BASELINE", baseline, baseline_outcomes),
            ("A1_DIRECT_CHALLENGER", direct, direct_outcomes),
            ("A2_DECOMPOSED", decomposed, decomposed_outcomes),
        )
        for policy_id, model, outcomes in entries:
            all_outcomes[policy_id].extend(outcomes)
            result["policies"][policy_id]["folds"].append({
                "fold": fold["fold"],
                "cutoff_ns": fold["cutoff_ns"],
                "train_markets": len(fold["train_markets"]),
                "test_markets": len(fold["test_markets"]),
                "training": model.training_receipt,
                "oos": performance_snapshot(outcomes),
            })

    for policy_id, outcomes in all_outcomes.items():
        result["policies"][policy_id]["aggregate_oos"] = (
            performance_snapshot(outcomes)
        )

    baseline = result["policies"]["A0_BASELINE"]["aggregate_oos"]
    direct = result["policies"]["A1_DIRECT_CHALLENGER"]["aggregate_oos"]
    decomposed = result["policies"]["A2_DECOMPOSED"]["aggregate_oos"]
    result["comparisons"] = {
        "A1_vs_A0": _comparison(baseline, direct),
        "A2_vs_A0": _comparison(baseline, decomposed),
        "A2_vs_A1": _comparison(direct, decomposed),
    }
    result["state"] = "READY"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--latency-ms", type=int, default=50)
    parser.add_argument("--capital-budget", type=float, default=10_000.0)
    parser.add_argument("--minimum-wall-ns", type=int, default=None)
    parser.add_argument(
        "--challenger-config",
        type=Path,
        default=Path("config/v7_trade_frequency_sizing_challenger.json"),
    )
    args = parser.parse_args(argv)

    config = load_trade_frequency_challenger_config(args.challenger_config)
    data = build_dataset(
        args.root,
        **(
            {"minimum_wall_ns": args.minimum_wall_ns}
            if args.minimum_wall_ns is not None
            else {}
        ),
    )
    if data.get("input_state") != "READY":
        result = {
            "schema": SCHEMA,
            **SAFETY,
            "state": data.get("input_state"),
            "automatic_promotion": False,
            "selection": "NONE_RESEARCH_ONLY_NO_RUNTIME_PROMOTION",
            "data_sha256": data.get("data_sha256"),
        }
    else:
        result = walk_forward_horse_race(
            data["decisions"],
            desired_folds=args.folds,
            latency_ms=args.latency_ms,
            capital_budget=args.capital_budget,
            challenger_config=config,
        )
        result["data_sha256"] = data.get("data_sha256")
    atomic_json(args.output, result)
    return 0 if result.get("state") == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
