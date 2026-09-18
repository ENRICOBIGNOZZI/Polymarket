"""Chronological OOS evaluation of the price-aware settlement EV gate."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from models import fit_settlement_residual, predict

DATASET_SCHEMA = "polymarket_v7_native_ev_dataset_v1"
REPORT_SCHEMA = "polymarket_v7_ev_gate_report_v1"
PRIMARY_BUFFER = 0.01
VALIDATION_BUFFERS = (0.0, 0.005, 0.01, 0.02)
FEATURE_NAMES = [
    "signal_return_bp",
    "confirmation_return_bp",
    "abs_signal_return_bp",
    "aligned_signal_return_bp",
    "aligned_confirmation_return_bp",
    "tte_seconds",
    "signal_age_ms",
    "spread",
    "bid_depth_shares",
    "ask_depth_shares",
    "depth_imbalance",
    "confirmed_non_opposing",
    "asset_BTC", "asset_ETH", "asset_SOL", "asset_XRP", "asset_DOGE", "asset_BNB",
    "horizon_M5", "horizon_M15", "horizon_H1", "horizon_H4", "horizon_D1",
]


def _clip(p: float) -> float:
    return min(1.0 - 1e-9, max(1e-9, float(p)))


def _brier(rows: list[dict[str, Any]], probabilities: list[float]) -> float | None:
    if not rows:
        return None
    return mean((float(r["outcome"]) - p) ** 2 for r, p in zip(rows, probabilities))


def _log_loss(rows: list[dict[str, Any]], probabilities: list[float]) -> float | None:
    if not rows:
        return None
    values = []
    for row, p in zip(rows, probabilities):
        p = _clip(p)
        y = float(row["outcome"])
        values.append(-y * math.log(p) - (1.0 - y) * math.log(1.0 - p))
    return mean(values)


def _max_drawdown(pnl: list[float]) -> float:
    peak = 0.0
    wealth = 0.0
    drawdown = 0.0
    for value in pnl:
        wealth += value
        peak = max(peak, wealth)
        drawdown = max(drawdown, peak - wealth)
    return drawdown


def policy_metrics(
    rows: list[dict[str, Any]], probabilities: list[float] | None, *, buffer: float,
    trade_all: bool = False,
) -> dict[str, Any]:
    selected = []
    for index, row in enumerate(rows):
        cost = float(row["executable_ask"]) + float(row["fee_per_share"])
        probability = None if probabilities is None else float(probabilities[index])
        predicted_edge = None if probability is None else probability - cost
        trade = trade_all or (predicted_edge is not None and predicted_edge > buffer)
        if not trade:
            continue
        realized = float(row["outcome"]) - cost
        selected.append((row, probability, predicted_edge, realized))
    pnl = [item[3] for item in selected]
    return {
        "markets": len(rows),
        "trades": len(selected),
        "trade_rate": len(selected) / len(rows) if rows else None,
        "net_pnl_per_share_total": sum(pnl),
        "net_pnl_per_share_mean": mean(pnl) if pnl else None,
        "win_rate": (
            sum(float(item[0]["outcome"]) == 1.0 for item in selected) / len(selected)
            if selected else None
        ),
        "mean_predicted_edge": (
            mean(item[2] for item in selected if item[2] is not None)
            if selected and probabilities is not None else None
        ),
        "mean_realized_edge": mean(pnl) if pnl else None,
        "max_drawdown_per_share": _max_drawdown(pnl),
        "buffer": buffer,
    }


def _group_gate(
    rows: list[dict[str, Any]], probabilities: list[float], *, buffer: float, key: str
) -> dict[str, Any]:
    groups: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for row, probability in zip(rows, probabilities):
        groups[str(row[key])].append((row, probability))
    return {
        label: policy_metrics(
            [r for r, _ in values], [p for _, p in values], buffer=buffer
        )
        for label, values in sorted(groups.items())
    }


def calibration_bins(
    rows: list[dict[str, Any]], probabilities: list[float], bins: int = 5
) -> list[dict[str, Any]]:
    result = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        selected = [
            (r, p) for r, p in zip(rows, probabilities)
            if low <= p < high or (index == bins - 1 and p == 1.0)
        ]
        result.append({
            "low": low,
            "high": high,
            "count": len(selected),
            "mean_probability": mean(p for _, p in selected) if selected else None,
            "outcome_rate": mean(float(r["outcome"]) for r, _ in selected) if selected else None,
        })
    return result


def analyze(
    dataset: dict[str, Any], *, minimum_training_markets: int = 100,
    minimum_validation_markets: int = 20, minimum_test_markets: int = 20,
    primary_buffer: float = PRIMARY_BUFFER,
) -> dict[str, Any]:
    if dataset.get("schema") != DATASET_SCHEMA:
        raise ValueError("dataset schema")
    rows = dataset.get("rows")
    if not isinstance(rows, list):
        raise ValueError("dataset rows")
    rows = sorted(rows, key=lambda r: (int(r["decision_ns"]), str(r["market"])))
    if len({str(r["market"]) for r in rows}) != len(rows):
        raise ValueError("duplicate market")
    base = {
        "schema": REPORT_SCHEMA,
        "paper_only": True,
        "execution_authority": False,
        "automatic_promotion": False,
        "real_money_authorized": False,
        "primary_buffer_per_share": primary_buffer,
        "dataset_sha256": dataset.get("dataset_sha256"),
        "total_markets": len(rows),
    }
    needed = minimum_training_markets + minimum_validation_markets + minimum_test_markets
    if len(rows) < needed:
        return {**base, "state": "INSUFFICIENT_EVIDENCE", "required_markets": needed}

    train_index = max(minimum_training_markets, int(len(rows) * 0.60))
    test_index = max(train_index + minimum_validation_markets, int(len(rows) * 0.80))
    if len(rows) - test_index < minimum_test_markets:
        test_index = len(rows) - minimum_test_markets
    train_end_ns = int(rows[train_index]["decision_ns"])
    training = [
        row for row in rows[:train_index]
        if int(row["label_observed_ns"]) < train_end_ns
    ]
    validation = rows[train_index:test_index]
    test = rows[test_index:]
    if (
        len(training) < minimum_training_markets
        or len(validation) < minimum_validation_markets
        or len(test) < minimum_test_markets
    ):
        return {
            **base,
            "state": "INSUFFICIENT_EVIDENCE_AFTER_PURGE",
            "training_markets": len(training),
            "validation_markets": len(validation),
            "test_markets": len(test),
        }

    model = fit_settlement_residual(
        training,
        feature_names=FEATURE_NAMES,
        train_end_ns=train_end_ns,
        dataset_sha256=str(dataset["dataset_sha256"]),
        ridge=1.0,
        minimum_markets=minimum_training_markets,
    )
    validation_probability = predict(model, validation)
    test_probability = predict(model, test)
    validation_pm = [float(row["pm_probability"]) for row in validation]
    test_pm = [float(row["pm_probability"]) for row in test]

    validation_grid = {
        str(buffer): policy_metrics(validation, validation_probability, buffer=buffer)
        for buffer in VALIDATION_BUFFERS
    }
    current = policy_metrics(test, None, buffer=0.0, trade_all=True)
    pm_gate = policy_metrics(test, test_pm, buffer=primary_buffer)
    model_gate = policy_metrics(test, test_probability, buffer=primary_buffer)
    return {
        **base,
        "state": "HELDOUT_EVALUATED_RESEARCH_ONLY",
        "training_cutoff_ns": train_end_ns,
        "training_markets": len(training),
        "validation_markets": len(validation),
        "test_markets": len(test),
        "model_sha256": model["model_sha256"],
        "model_feature_names": FEATURE_NAMES,
        "validation_buffer_diagnostics_no_selection": validation_grid,
        "validation": {
            "pm_brier": _brier(validation, validation_pm),
            "model_brier": _brier(validation, validation_probability),
            "pm_log_loss": _log_loss(validation, validation_pm),
            "model_log_loss": _log_loss(validation, validation_probability),
        },
        "test": {
            "pm_brier": _brier(test, test_pm),
            "model_brier": _brier(test, test_probability),
            "brier_improvement_over_pm": (
                (_brier(test, test_pm) or 0.0) - (_brier(test, test_probability) or 0.0)
            ),
            "pm_log_loss": _log_loss(test, test_pm),
            "model_log_loss": _log_loss(test, test_probability),
            "current_directional_policy": current,
            "pm_prior_ev_gate": pm_gate,
            "model_ev_gate": model_gate,
            "model_gate_by_asset": _group_gate(
                test, test_probability, buffer=primary_buffer, key="asset"
            ),
            "model_gate_by_horizon": _group_gate(
                test, test_probability, buffer=primary_buffer, key="horizon"
            ),
            "pm_calibration": calibration_bins(test, test_pm),
            "model_calibration": calibration_bins(test, test_probability),
        },
        "limitations": [
            "The primary EV buffer is fixed before test evaluation.",
            "Validation buffer diagnostics do not select the test threshold.",
            "One accepted taker decision per market is the statistical unit.",
            "This report cannot authorize production or real-money execution.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-training-markets", type=int, default=100)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output exists")
    report = analyze(
        json.loads(args.dataset.read_text()),
        minimum_training_markets=args.minimum_training_markets,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"state": report["state"], "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
