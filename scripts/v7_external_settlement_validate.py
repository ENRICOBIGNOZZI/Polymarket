#!/usr/bin/env python3
"""Validate an immutable settlement model on post-publication forward rows.

Validation is clustered by settlement market/day and evaluates executable
lower-bound actions when authoritative fee schedules and asks were recorded.
It never promotes a model.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import fields
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any

try:
    from v7_external_economic_common import atomic_json, canonical_sha256, finite
    from v7_external_policy_replay import day_block_lcb95
    from v7_external_settlement_model import predict, validate_parameters
    from v7_external_settlement_train import read_dataset
    from v7_fair_value_registry import FairModelArtifact
except ModuleNotFoundError:
    from scripts.v7_external_economic_common import atomic_json, canonical_sha256, finite
    from scripts.v7_external_policy_replay import day_block_lcb95
    from scripts.v7_external_settlement_model import predict, validate_parameters
    from scripts.v7_external_settlement_train import read_dataset
    from scripts.v7_fair_value_registry import FairModelArtifact


SCHEMA = "polymarket_v7_external_settlement_validation_v1"


def load_artifact(path: Path) -> FairModelArtifact:
    raw = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in fields(FairModelArtifact)}
    artifact = FairModelArtifact(**{key: value for key, value in raw.items() if key in allowed})
    artifact.validate()
    validate_parameters(artifact)
    return artifact


def fee_per_share(price: float, schedule: dict[str, Any]) -> float | None:
    rate, exponent = finite(schedule.get("rate")), finite(schedule.get("exponent"))
    if rate is None or exponent is None or rate < 0.0 or exponent < 0.0 or not 0.0 < price < 1.0:
        return None
    return rate * (price * (1.0 - price)) ** exponent


def _entry_policy(tte: float, config: dict[str, Any]) -> tuple[float, float]:
    taker = config.get("taker") if isinstance(config.get("taker"), dict) else {}
    for bucket in taker.get("tte_bucket_policy") if isinstance(taker.get("tte_bucket_policy"), list) else []:
        if not isinstance(bucket, dict):
            continue
        minimum, maximum = finite(bucket.get("minimum_seconds")), finite(bucket.get("maximum_seconds"))
        if None not in (minimum, maximum) and minimum <= tte <= maximum:
            return (
                float(finite(bucket.get("minimum_robust_ev_per_share"), math.inf)),
                float(finite(bucket.get("execution_risk_per_share"), math.inf)),
            )
    return (
        float(finite(taker.get("minimum_robust_ev_per_share"), math.inf)),
        float(finite(taker.get("base_execution_risk_per_share"), math.inf)),
    )


def _cluster_scores(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluations:
        clusters[str(row["market_id"])].append(row)
    if not clusters:
        return {
            "rows": 0, "independent_contracts": 0, "brier": None,
            "log_loss": None, "margin_rmse_bps": None,
        }
    brier = []
    log_loss = []
    margin_mse = []
    for values in clusters.values():
        brier.append(statistics.fmean((row["yes"] - row["actual_yes"]) ** 2 for row in values))
        log_loss.append(statistics.fmean(
            -(row["actual_yes"] * math.log(min(1.0 - 1e-12, max(1e-12, row["yes"])))
              + (1.0 - row["actual_yes"]) * math.log(min(1.0 - 1e-12, max(1e-12, 1.0 - row["yes"]))))
            for row in values
        ))
        margin_mse.append(statistics.fmean(
            (row["predicted_settlement_margin_bps"] - row["target_margin_bps"]) ** 2
            for row in values
        ))
    return {
        "rows": len(evaluations), "independent_contracts": len(clusters),
        "brier": statistics.fmean(brier), "log_loss": statistics.fmean(log_loss),
        "margin_rmse_bps": math.sqrt(statistics.fmean(margin_mse)),
    }


def _calibration_bins(evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in evaluations:
        probability_bin = min(9, max(0, int(row["yes"] * 10)))
        tte = row["tte_seconds"]
        tte_bucket = "0_60" if tte <= 60.0 else "60_180" if tte <= 180.0 else "180_300"
        bins[(probability_bin, tte_bucket)].append(row)
    output: list[dict[str, Any]] = []
    for (probability_bin, tte_bucket), values in sorted(bins.items()):
        by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in values:
            by_market[str(row["market_id"])].append(row)
        market_forecasts = [
            statistics.fmean(row["yes"] for row in rows)
            for rows in by_market.values()
        ]
        market_actuals = [float(rows[0]["actual_yes"]) for rows in by_market.values()]
        forecast = statistics.fmean(market_forecasts)
        actual = statistics.fmean(market_actuals)
        # Wilson uncertainty stays non-zero for all-win/all-loss and tiny bins;
        # the normal approximation incorrectly collapses to zero there.
        count = len(market_actuals)
        z = 1.96
        denominator = 1.0 + z * z / count
        center = (actual + z * z / (2.0 * count)) / denominator
        radius = z * math.sqrt(
            actual * (1.0 - actual) / count + z * z / (4.0 * count * count)
        ) / denominator
        uncertainty = max(abs(actual - (center - radius)), abs(center + radius - actual))
        output.append({
            "probability_bin": probability_bin,
            "tte_bucket": tte_bucket,
            "rows": len(values),
            "contracts": count,
            "mean_forecast": forecast, "observed_rate": actual,
            "conditional_calibration_error": abs(forecast - actual),
            "observed_rate_wilson95_radius": uncertainty,
            "conditional_calibration_uncertainty": abs(forecast - actual) + uncertainty,
        })
    return output


def _economic_actions(
    rows: list[dict[str, Any]], evaluations: list[dict[str, Any]], config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    output: list[dict[str, Any]] = []
    reasons: dict[str, int] = defaultdict(int)
    selected_markets: set[str] = set()
    paired = sorted(
        zip(rows, evaluations),
        key=lambda pair: (int(pair[0]["observed_ms"]), str(pair[1]["market_id"])),
    )
    for source, model in paired:
        market_id = str(model["market_id"])
        if market_id in selected_markets:
            reasons["MARKET_POSITION_ALREADY_SELECTED"] += 1
            continue
        execution = source.get("execution") if isinstance(source.get("execution"), dict) else {}
        schedule = execution.get("fee_schedule") if isinstance(execution.get("fee_schedule"), dict) else {}
        tte = float(source["features"]["tte_seconds"])
        threshold, risk = _entry_policy(tte, config)
        actions: list[tuple[float, str, float, float, float]] = []
        for outcome, probability, ask_key, size_key, minimum_key in (
            ("YES", model["lower"], "yes_best_ask", "yes_best_ask_visible_size", "yes_min_order_size"),
            ("NO", 1.0 - model["upper"], "no_best_ask", "no_best_ask_visible_size", "no_min_order_size"),
        ):
            ask = finite(execution.get(ask_key))
            visible_size = finite(execution.get(size_key))
            minimum_size = finite(execution.get(minimum_key))
            fee = fee_per_share(ask, schedule) if ask is not None else None
            if (ask is None or fee is None or visible_size is None
                    or minimum_size is None or minimum_size <= 0.0
                    or visible_size < minimum_size):
                continue
            actions.append((probability - ask - fee - risk, outcome, ask, fee, minimum_size))
        if not actions:
            reasons["EXECUTABLE_ASK_OR_FEE_MISSING"] += 1
            continue
        edge, outcome, ask, fee, quantity = max(actions)
        if edge <= threshold:
            reasons["ABSTAIN_NONPOSITIVE_LOWER_BOUND"] += 1
            continue
        won = (model["actual_yes"] == 1.0) == (outcome == "YES")
        pnl = quantity * ((1.0 if won else 0.0) - ask - fee)
        selected_markets.add(market_id)
        output.append({
            "market_id": market_id, "timestamp_ms": source["observed_ms"],
            "pnl": pnl, "outcome": outcome, "lower_bound_edge_per_share": edge,
            "claimed_executable_edge_per_share": edge,
            "tte_seconds": tte, "quantity": quantity,
        })
    return output, dict(sorted(reasons.items()))


def validate(
    artifact: FairModelArtifact, rows: list[dict[str, Any]], config: dict[str, Any],
) -> dict[str, Any]:
    forward_start = int(artifact.hyperparameters.get("forward_oos_starts_after_ns") or artifact.training_end_ns)
    forward_rows = [row for row in rows if int(row["observed_ms"]) * 1_000_000 > forward_start]
    evaluations: list[dict[str, Any]] = []
    for row in forward_rows:
        prediction = predict(artifact, row["features"])
        evaluations.append({
            "market_id": str(row["market_id"]), "observed_ms": int(row["observed_ms"]),
            "actual_yes": float(row["actual_yes"]),
            "target_margin_bps": float(row["target_settlement_margin_bps"]),
            "tte_seconds": float(row["features"]["tte_seconds"]), **prediction,
        })
    actions, abstention_reasons = _economic_actions(forward_rows, evaluations, config)
    confidence = day_block_lcb95(actions)
    calibration_bins = _calibration_bins(evaluations)
    days = len({row["observed_day"] for row in forward_rows})
    contracts = len({row["market_id"] for row in forward_rows})
    promotion_config = config.get("promotion") if isinstance(config.get("promotion"), dict) else {}
    minimum_days = int(promotion_config.get("minimum_settlement_labeled_days") or 30)
    minimum_contracts = int(promotion_config.get("minimum_settlement_labeled_contracts") or 2500)
    minimum_trades = int(promotion_config.get("minimum_policy_forward_oos_trades") or 300)
    minimum_bin_contracts = int(
        promotion_config.get("minimum_conditional_calibration_contracts_per_bin") or 30
    )
    calibration_bin_size_pass = bool(calibration_bins) and all(
        row["contracts"] >= minimum_bin_contracts for row in calibration_bins
    )
    local_uncertainty_pass = bool(actions and calibration_bins) and calibration_bin_size_pass and max(
        row["conditional_calibration_uncertainty"] for row in calibration_bins
    ) < min(row["claimed_executable_edge_per_share"] for row in actions)
    gates = {
        "minimum_forward_days": {"required": minimum_days, "observed": days, "pass": days >= minimum_days},
        "minimum_settlement_contracts": {"required": minimum_contracts, "observed": contracts, "pass": contracts >= minimum_contracts},
        "minimum_policy_trades": {"required": minimum_trades, "observed": len(actions), "pass": len(actions) >= minimum_trades},
        "minimum_conditional_calibration_contracts_per_bin": {
            "required": minimum_bin_contracts,
            "observed": min((row["contracts"] for row in calibration_bins), default=0),
            "pass": calibration_bin_size_pass,
        },
        "positive_day_block_lcb95": {"observed": confidence["lcb95"], "pass": confidence["lcb95"] is not None and confidence["lcb95"] > 0.0},
        "local_calibration_uncertainty_below_claimed_edge": {"pass": local_uncertainty_pass},
        "zero_causality_failures": {"observed": sum(row.get("causality_valid") is not True for row in forward_rows), "pass": all(row.get("causality_valid") is True for row in forward_rows)},
    }
    return {
        "schema": SCHEMA, "generated_at_unix_ms": int(time.time() * 1000),
        "model_hash": artifact.model_hash, "model_version": artifact.model_version,
        "forward_oos_starts_after_ns": forward_start,
        "forward_rows": len(forward_rows), "forward_contracts": contracts,
        "forward_days": days, "forecast_scores": _cluster_scores(evaluations),
        "conditional_calibration": calibration_bins,
        "policy": {
            "actions": len(actions), "abstentions": len(forward_rows) - len(actions),
            "abstention_reasons": abstention_reasons,
            "independent_markets": len({row["market_id"] for row in actions}),
            "net_pnl_at_minimum_executable_size": sum(row["pnl"] for row in actions),
            "confidence": confidence,
        },
        "gates": gates,
        "promotion_eligible": bool(gates) and all(value["pass"] for value in gates.values()),
        "automatic_promotion": False,
        "execution_authority": "SHADOW_ZERO_AUTHORITY",
        "evaluation_rows": evaluations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact = load_artifact(args.artifact)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = validate(artifact, read_dataset(args.dataset), config)
    report["content_sha256"] = canonical_sha256(report)
    atomic_json(args.output, report)
    print(json.dumps({
        "forward_contracts": report["forward_contracts"],
        "policy_actions": report["policy"]["actions"],
        "promotion_eligible": report["promotion_eligible"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
