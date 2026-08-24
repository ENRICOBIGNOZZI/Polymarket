#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

import external_intelligence as ext

SCHEMA = "polymarket_lf_external_validation_v1"


def _mean(values: Iterable[float]) -> float:
    xs = [float(value) for value in values]
    return statistics.fmean(xs) if xs else 0.0


def _finite(value: Any, default: float = math.nan) -> float:
    return ext.finite(value, default)


def _cluster_key(row: dict[str, Any], cluster_seconds: int) -> tuple[int, str]:
    ts = ext.integer(row.get("observed_ts"))
    bucket = ts // max(1, cluster_seconds)
    event = str(row.get("event_id") or row.get("market_id") or "unknown")
    return bucket, event


def _cluster_sums(
    rows: Sequence[dict[str, Any]], values: Sequence[float], cluster_seconds: int
) -> list[tuple[tuple[int, str], float]]:
    grouped: dict[tuple[int, str], float] = defaultdict(float)
    for row, value in zip(rows, values):
        grouped[_cluster_key(row, cluster_seconds)] += float(value)
    return sorted(grouped.items(), key=lambda item: item[0])


def _positive_fold_fraction(values: Sequence[float], folds: int) -> tuple[float, list[float]]:
    if not values:
        return 0.0, []
    folds = max(2, min(int(folds), len(values)))
    fold_sums: list[float] = []
    for fold in range(folds):
        lo = math.floor(len(values) * fold / folds)
        hi = math.floor(len(values) * (fold + 1) / folds)
        if hi > lo:
            fold_sums.append(sum(values[lo:hi]))
    fraction = sum(value > 0.0 for value in fold_sums) / len(fold_sums) if fold_sums else 0.0
    return fraction, fold_sums


def nested_short_horizon_ablation(
    rows: Sequence[dict[str, Any]],
    config: dict[str, Any],
    source: str,
    feature_name: str,
    horizon_seconds: int,
) -> dict[str, Any]:
    """Test whether an external feature adds value beyond a purged no-external baseline.

    The baseline is the expanding purged training mean of the future Polymarket
    price change. The challenger uses the exact same training rows plus the
    external feature. Every statistic is therefore incremental on a common
    chronological sample; it cannot pass merely because unconditional drift is
    non-zero.

    Inference aggregates incremental PnL by simultaneous event clusters before
    moving-block bootstrap and fold-stability checks. This prevents many
    correlated markets in one event/timestamp from being counted as independent
    evidence.
    """

    backtest = config.get("backtest") or {}
    gates = config.get("gates") or {}
    min_train = max(2, ext.integer(backtest.get("min_train_observations"), 30))
    ridge = max(1e-12, _finite(backtest.get("ridge"), 1e-5))
    base_extra_cost = max(0.0, _finite(backtest.get("extra_cost_bps"), 20.0) / 10000.0)
    multipliers = [float(value) for value in backtest.get("cost_stress_multipliers", [1.0, 1.5, 2.0])]
    cluster_seconds = max(60, ext.integer(backtest.get("dependence_cluster_seconds"), 3600))
    direct_probability = feature_name == "external_probability"

    used_rows: list[dict[str, Any]] = []
    ext_predictions: list[float] = []
    base_predictions: list[float] = []
    targets: list[float] = []
    ext_sides: list[int] = []
    incremental_pnl: dict[str, list[float]] = {str(value): [] for value in multipliers}
    external_pnl: dict[str, list[float]] = {str(value): [] for value in multipliers}
    baseline_pnl: dict[str, list[float]] = {str(value): [] for value in multipliers}

    for index, row in enumerate(rows):
        train = ext.purged_training_rows(rows, index)
        if len(train) < min_train:
            continue
        target_train = [_finite(candidate.get("target_delta")) for candidate in train]
        target_train = [value for value in target_train if math.isfinite(value)]
        if len(target_train) < min_train:
            continue
        baseline_prediction = statistics.fmean(target_train)
        intercept, slope, center = ext.fit_signal(train, direct_probability, ridge)
        x = (
            _finite(row.get("q_external"), _finite(row.get("pm_mid"))) - _finite(row.get("pm_mid"))
            if direct_probability
            else _finite(row.get("feature_value"))
        )
        if not math.isfinite(x):
            continue
        external_prediction = intercept + slope * (x - center)
        target = _finite(row.get("target_delta"))
        if not math.isfinite(target):
            continue

        used_rows.append(dict(row))
        ext_predictions.append(external_prediction)
        base_predictions.append(baseline_prediction)
        targets.append(target)
        normal_side = 0
        for multiplier in multipliers:
            key = str(multiplier)
            cost = base_extra_cost * multiplier
            ext_value, ext_side = ext.trade_pnl(row, external_prediction, cost)
            base_value, _ = ext.trade_pnl(row, baseline_prediction, cost)
            external_pnl[key].append(ext_value)
            baseline_pnl[key].append(base_value)
            incremental_pnl[key].append(ext_value - base_value)
            if multiplier == 1.0:
                normal_side = ext_side
        ext_sides.append(normal_side)

    external_mse = (
        statistics.fmean((prediction - target) ** 2 for prediction, target in zip(ext_predictions, targets))
        if targets
        else 0.0
    )
    baseline_mse = (
        statistics.fmean((prediction - target) ** 2 for prediction, target in zip(base_predictions, targets))
        if targets
        else 0.0
    )
    mse_improvement = baseline_mse - external_mse

    normal_incremental = incremental_pnl.get("1.0", [])
    cluster_pairs = _cluster_sums(used_rows, normal_incremental, cluster_seconds)
    cluster_values = [value for _, value in cluster_pairs]
    fold_fraction, fold_sums = _positive_fold_fraction(
        cluster_values, max(2, ext.integer(backtest.get("folds"), 4))
    )
    pvalue = ext.bootstrap_pvalue(
        cluster_values,
        max(1, ext.integer(backtest.get("bootstrap_block"), 5)),
        max(100, ext.integer(backtest.get("bootstrap_reps"), 1000)),
        20260824 + sum(ord(char) for char in source + feature_name) + int(horizon_seconds),
    )

    reasons: list[str] = []
    if len(used_rows) < ext.integer(gates.get("min_oos_predictions"), 40):
        reasons.append("insufficient_oos_predictions")
    if sum(side != 0 for side in ext_sides) < ext.integer(gates.get("min_trades"), 20):
        reasons.append("insufficient_external_trades")
    min_clusters = max(4, ext.integer(gates.get("min_dependence_clusters"), 20))
    if len(cluster_values) < min_clusters:
        reasons.append("insufficient_dependence_clusters")
    if mse_improvement <= 0.0:
        reasons.append("no_incremental_mse_improvement_vs_no_external")
    for multiplier in (1.0, 1.5, 2.0):
        values = incremental_pnl.get(str(multiplier), [])
        if sum(values) <= 0.0:
            reasons.append(f"nonpositive_incremental_{multiplier:g}x_pnl_vs_no_external")
    if pvalue > _finite(gates.get("max_bootstrap_pvalue"), 0.10):
        reasons.append("cluster_bootstrap_gate")
    if fold_fraction < _finite(gates.get("min_positive_fold_fraction"), 0.50):
        reasons.append("cluster_fold_stability_gate")

    return {
        "schema": SCHEMA,
        "kind": "short_horizon_nested_external_ablation",
        "candidate_id": f"external:{source}:{feature_name}:{horizon_seconds}s",
        "source": source,
        "feature_name": feature_name,
        "horizon_seconds": int(horizon_seconds),
        "oos_predictions": len(used_rows),
        "external_trades": sum(side != 0 for side in ext_sides),
        "dependence_clusters": len(cluster_values),
        "dependence_cluster_seconds": cluster_seconds,
        "external_prediction_mse": external_mse,
        "no_external_baseline_mse": baseline_mse,
        "incremental_mse_improvement": mse_improvement,
        "external_cost_stress_pnl_per_share": {key: sum(values) for key, values in external_pnl.items()},
        "baseline_cost_stress_pnl_per_share": {key: sum(values) for key, values in baseline_pnl.items()},
        "incremental_cost_stress_pnl_per_share": {key: sum(values) for key, values in incremental_pnl.items()},
        "cluster_bootstrap_pvalue": pvalue,
        "cluster_positive_fold_fraction": fold_fraction,
        "cluster_fold_sums": fold_sums,
        "gate_pass": not reasons,
        "reasons": reasons,
        "baseline": "purged expanding no-external mean-delta predictor",
        "requires_incumbent_champion_ablation_before_integration": True,
        "production_change": False,
    }


def _log_loss(probability: float, outcome: int) -> float:
    probability = min(1.0 - 1e-9, max(1e-9, probability))
    return -(outcome * math.log(probability) + (1 - outcome) * math.log(1.0 - probability))


def _resolution_outcomes(prices: Sequence[dict[str, Any]]) -> dict[str, int]:
    outcomes: dict[str, tuple[int, int]] = {}
    for row in prices:
        outcome_raw = row.get("resolved_outcome")
        if outcome_raw is None:
            continue
        try:
            outcome = int(outcome_raw)
        except (TypeError, ValueError):
            continue
        if outcome not in (0, 1):
            continue
        market_id = str(row.get("market_id") or "")
        if not market_id:
            continue
        ts = ext.integer(row.get("observed_ts"))
        previous = outcomes.get(market_id)
        if previous is None or ts >= previous[0]:
            outcomes[market_id] = (ts, outcome)
    return {market_id: value[1] for market_id, value in outcomes.items()}


def _horizon_bucket(seconds: int) -> str:
    if seconds <= 86400:
        return "0-1d"
    if seconds <= 7 * 86400:
        return "1-7d"
    if seconds <= 30 * 86400:
        return "7-30d"
    return "30d+"


def terminal_probability_ablation(
    observations: Sequence[dict[str, Any]], prices: Sequence[dict[str, Any]], source: str = "kalshi"
) -> dict[str, Any]:
    """Evaluate terminal probabilities on resolved outcomes without pseudo-replication.

    At most one (latest) observation per market and time-to-resolution bucket is
    retained. Brier/log-loss improvements are measured against the contemporaneous
    Polymarket probability on exactly the same market observations. Event-level
    score differences are reported so downstream inference can cluster by event.
    """

    outcomes = _resolution_outcomes(prices)
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in observations:
        if str(row.get("source") or "") != source or str(row.get("feature_name") or "") != "external_probability":
            continue
        market_id = str(row.get("market_id") or "")
        if market_id not in outcomes:
            continue
        q_external = _finite(row.get("q_external"))
        q_pm = _finite(row.get("pm_mid"))
        if not (math.isfinite(q_external) and math.isfinite(q_pm) and 0.0 < q_external < 1.0 and 0.0 < q_pm < 1.0):
            continue
        observed_ts = ext.integer(row.get("observed_ts"))
        end_ts = ext.integer(row.get("end_ts"))
        if not observed_ts or not end_ts or observed_ts >= end_ts:
            continue
        bucket = _horizon_bucket(max(0, end_ts - observed_ts))
        key = (market_id, bucket)
        previous = selected.get(key)
        if previous is None or observed_ts > ext.integer(previous.get("observed_ts")):
            selected[key] = dict(row)

    rows = sorted(selected.values(), key=lambda row: (ext.integer(row.get("observed_ts")), str(row.get("market_id"))))
    scored: list[dict[str, Any]] = []
    for row in rows:
        market_id = str(row.get("market_id"))
        outcome = outcomes[market_id]
        q_external = _finite(row.get("q_external"))
        q_pm = _finite(row.get("pm_mid"))
        end_ts = ext.integer(row.get("end_ts"))
        observed_ts = ext.integer(row.get("observed_ts"))
        scored.append({
            "market_id": market_id,
            "event_id": str(row.get("event_id") or market_id),
            "bucket": _horizon_bucket(max(0, end_ts - observed_ts)),
            "brier_improvement": (q_pm - outcome) ** 2 - (q_external - outcome) ** 2,
            "log_loss_improvement": _log_loss(q_pm, outcome) - _log_loss(q_external, outcome),
        })

    def summarize(subset: Sequence[dict[str, Any]]) -> dict[str, Any]:
        event_brier: dict[str, list[float]] = defaultdict(list)
        event_log: dict[str, list[float]] = defaultdict(list)
        for row in subset:
            event = str(row["event_id"])
            event_brier[event].append(float(row["brier_improvement"]))
            event_log[event].append(float(row["log_loss_improvement"]))
        cluster_brier = [_mean(values) for _, values in sorted(event_brier.items())]
        cluster_log = [_mean(values) for _, values in sorted(event_log.items())]
        return {
            "market_bucket_observations": len(subset),
            "unique_markets": len({str(row["market_id"]) for row in subset}),
            "event_clusters": len(event_brier),
            "brier_improvement_external_vs_polymarket": _mean(row["brier_improvement"] for row in subset),
            "log_loss_improvement_external_vs_polymarket": _mean(row["log_loss_improvement"] for row in subset),
            "event_mean_brier_improvement": _mean(cluster_brier),
            "event_mean_log_loss_improvement": _mean(cluster_log),
        }

    by_bucket = {
        bucket: summarize([row for row in scored if row["bucket"] == bucket])
        for bucket in ("0-1d", "1-7d", "7-30d", "30d+")
        if any(row["bucket"] == bucket for row in scored)
    }
    overall = summarize(scored)
    return {
        "schema": SCHEMA,
        "kind": "terminal_probability_external_vs_polymarket",
        "source": source,
        "overall": overall,
        "by_time_to_resolution": by_bucket,
        "selection": "latest point-in-time observation per market and time-to-resolution bucket",
        "independence_guard": "report event-clustered score improvements; repeated same-market observations do not count independently",
        "production_change": False,
    }


def read_jsonl_gz(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def analyze(observations: Sequence[dict[str, Any]], prices: Sequence[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        source = str(row.get("source") or "")
        feature = str(row.get("feature_name") or "")
        if source and feature:
            groups[(source, feature)].append(dict(row))

    short_horizon: list[dict[str, Any]] = []
    backtest = config.get("backtest") or {}
    for horizon in [ext.integer(value) for value in backtest.get("horizons_seconds", [3600, 21600, 86400])]:
        tolerance = max(60, ext.integer(backtest.get("future_price_tolerance_seconds"), horizon // 2))
        for (source, feature), rows in sorted(groups.items()):
            labeled = ext.label_observations(rows, prices, horizon, tolerance)
            if labeled:
                short_horizon.append(nested_short_horizon_ablation(labeled, config, source, feature, horizon))
    short_horizon.sort(key=lambda row: (0 if row.get("gate_pass") else 1, row.get("cluster_bootstrap_pvalue", 1.0), row.get("candidate_id", "")))

    return {
        "schema": SCHEMA,
        "short_horizon": short_horizon,
        "terminal_probability": terminal_probability_ablation(observations, prices),
        "interpretation": {
            "short_horizon_baseline_is_not_live_champion": True,
            "terminal_score_is_against_contemporaneous_polymarket_probability": True,
            "incumbent_champion_ablation_still_required_before_integration": True,
            "production_change": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only LF validation for external Polymarket information")
    parser.add_argument("--config", type=Path, default=Path("config/external_intelligence.json"))
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    ext.validate_config(config)
    report = analyze(read_jsonl_gz(args.observations), read_jsonl_gz(args.prices), config)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
