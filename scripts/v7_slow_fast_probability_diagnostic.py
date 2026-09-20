#!/usr/bin/env python3
"""OOS diagnostic for adding causal slow context to the frozen fast probability model.

This script never emits a loadable runtime artifact. It compares the existing
fast feature family with a ridge-regularized augmentation of slow values plus
explicit missingness indicators using training-only normalization.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from v7_fit_probability_candidate import (
    ASSETS, HORIZONS, feature_vector, fit, metrics, scales_for, sigmoid, weights,
)
from v7_slow_fast_probability_dataset import SLOW_FIELDS

SCHEMA = "polymarket_v7_slow_fast_probability_diagnostic_v1"


def _legacy_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "asset": row["asset"],
        "horizon": row["horizon"],
        "market_id": row["market_id"],
        "close_ts_ms": row["close_ts_ms"],
        "decision_ts_ms": row["decision_ts_ms"],
        "selected_outcome": row["selected_outcome"],
        "features": row["fast_features"],
    }


def _slow_stats(rows: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    stats: dict[str, tuple[float, float]] = {}
    for name in SLOW_FIELDS:
        values = [
            float(r["slow_context"]["values"][name])
            for r in rows
            if r["slow_context"]["values"].get(name) is not None
        ]
        if not values:
            stats[name] = (0.0, 1.0)
            continue
        mu = float(np.mean(values))
        sd = float(np.std(values))
        stats[name] = (mu, sd if sd > 1e-12 else 1.0)
    return stats


def slow_vector(row: dict[str, Any], stats: dict[str, tuple[float, float]]) -> np.ndarray:
    values: list[float] = []
    slow = row["slow_context"]["values"]
    for name in SLOW_FIELDS:
        raw = slow.get(name)
        if raw is None:
            values.extend((0.0, 0.0))
        else:
            x = float(raw)
            if not math.isfinite(x):
                raise ValueError("nonfinite_slow_value")
            mu, sd = stats[name]
            values.extend(((x - mu) / sd, 1.0))
    return np.asarray(values, dtype=float)


def coverage(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    n = len(rows)
    return {
        name: (sum(r["slow_context"]["values"].get(name) is not None for r in rows) / n if n else None)
        for name in SLOW_FIELDS
    }


def compare(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [r for r in rows if r.get("selected_outcome") in (0.0, 1.0)]
    usable.sort(key=lambda r: (r["close_ts_ms"], r["market_id"], r["decision_ts_ms"]))
    blocks = sorted({int(r["close_ts_ms"]) // 900_000 for r in usable})
    if len(blocks) < 4:
        raise ValueError("insufficient_15_minute_blocks")
    split = blocks[max(1, int(.7 * len(blocks)))]
    train = [r for r in usable if int(r["close_ts_ms"]) // 900_000 < split]
    test = [r for r in usable if int(r["close_ts_ms"]) // 900_000 >= split]
    if not train or not test:
        raise ValueError("empty_chronological_split")
    first_test_decision = min(int(r["decision_ts_ms"]) for r in test)
    train = [r for r in train if int(r["close_ts_ms"]) < first_test_decision]
    if not train:
        raise ValueError("purge_removed_training")

    legacy_train = [_legacy_row(r) for r in train]
    legacy_test = [_legacy_row(r) for r in test]
    shock_scales = scales_for(legacy_train)
    x_train = np.vstack([feature_vector(r, shock_scales) for r in legacy_train])
    x_test = np.vstack([feature_vector(r, shock_scales) for r in legacy_test])
    y_train = np.asarray([r["selected_outcome"] for r in train], dtype=float)
    y_test = np.asarray([r["selected_outcome"] for r in test], dtype=float)
    w_train = weights(legacy_train)
    w_test = weights(legacy_test)

    base_penalty = np.asarray([3., 8., 8., 8., 8., 8., 8., 8.] + [12.] * 6 + [12.] * 4)
    beta_base = fit(x_train, y_train, w_train, base_penalty)
    baseline = metrics(sigmoid(x_test @ beta_base), y_test, w_test)

    stats = _slow_stats(train)
    z_train = np.vstack([slow_vector(r, stats) for r in train])
    z_test = np.vstack([slow_vector(r, stats) for r in test])
    aug_train = np.hstack([x_train, z_train])
    aug_test = np.hstack([x_test, z_test])
    # Slow directions are deliberately shrunk harder than the established fast
    # family. This is a diagnostic, not a claim that 20 is an optimal penalty.
    aug_penalty = np.concatenate([base_penalty, np.full(z_train.shape[1], 20.0)])
    beta_aug = fit(aug_train, y_train, w_train, aug_penalty)
    augmented = metrics(sigmoid(aug_test @ beta_aug), y_test, w_test)

    return {
        "schema": SCHEMA,
        "paper_only": True,
        "execution_authority": False,
        "runtime_artifact_emitted": False,
        "automatic_promotion": False,
        "train_rows": len(train),
        "test_rows": len(test),
        "train_markets": len({r["market_id"] for r in train}),
        "test_markets": len({r["market_id"] for r in test}),
        "baseline_fast": baseline,
        "augmented_slow_fast": augmented,
        "delta_log_loss_augmented_minus_fast": augmented["log_loss"] - baseline["log_loss"],
        "delta_brier_augmented_minus_fast": augmented["brier"] - baseline["brier"],
        "train_slow_coverage": coverage(train),
        "test_slow_coverage": coverage(test),
        "normalization": "TRAIN_ONLY_OBSERVED_SLOW_VALUES_PLUS_MISSINGNESS_INDICATORS",
        "split": "CHRONOLOGICAL_15MIN_BLOCK_WITH_CONTRACT_CLOSE_PURGE",
        "interpretation": "NEGATIVE_DELTA_FAVORS_AUGMENTED_ON_THIS_HOLDOUT_ONLY",
        "decision": "NO_RUNTIME_PROMOTION_FROM_THIS_DIAGNOSTIC",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    rows = [json.loads(line) for line in a.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = compare(rows)
    if a.output.exists():
        raise SystemExit("refusing to overwrite slow-fast diagnostic")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
