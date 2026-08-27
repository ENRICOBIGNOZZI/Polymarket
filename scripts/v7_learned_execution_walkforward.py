#!/usr/bin/env python3
"""Blocked walk-forward validator for V7 learned execution.

This module is deliberately separate from the estimator.  A single terminal
holdout remains diagnostic; economic validation authority is expanding-window,
non-overlapping walk-forward evidence with label maturity, embargo, UTC-day
block bootstrap, and fold-stability statistics.  It is PAPER/read-only and
cannot promote or mutate execution state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

from v7_learned_execution_hardened import EPS
from v7_learned_execution_schema import MARKOUTS
from v7_learned_execution_model import (
    ExecutionModelError,
    JointExample,
    Kernel,
    OrderExample,
    build_joint,
    build_orders,
    load_ledger,
    predict_distribution,
    predict_mean,
    product_marginal_probability,
)

DAY_MS = 86_400_000
SCHEMA = "polymarket_v7_learned_execution_walkforward_v1"


def walk_forward_splits(
    rows: Sequence[Any], *, min_train: int, min_test: int, folds: int, embargo_ms: int
) -> list[tuple[list[Any], list[Any]]]:
    """Expanding-window, contiguous non-overlapping, label-mature OOS folds."""
    if folds <= 0 or min_train <= 0 or min_test <= 0 or embargo_ms < 0:
        raise ExecutionModelError("walk_forward:invalid_contract")
    ordered = sorted(
        rows,
        key=lambda row: (
            int(row.ts_ms),
            str(getattr(row, "order_id", getattr(row, "group_id", ""))),
        ),
    )
    available = len(ordered) - min_train
    max_folds = min(folds, available // min_test) if available >= min_test else 0
    if max_folds <= 0:
        return []
    base, rem = divmod(available, max_folds)
    start = min_train
    out: list[tuple[list[Any], list[Any]]] = []
    for k in range(max_folds):
        width = base + int(k < rem)
        test = list(ordered[start : start + width])
        if len(test) >= min_test:
            cutoff = int(test[0].ts_ms) - embargo_ms
            train = [
                row
                for row in ordered[:start]
                if int(getattr(row, "label_ts_ms", row.ts_ms)) <= cutoff
            ]
            if len(train) >= min_train:
                out.append((train, test))
        start += width
    return out


def _day_block_bootstrap(
    observations: Sequence[tuple[int, float]], *, samples: int, seed_key: str
) -> dict[str, Any]:
    """Bootstrap mean loss improvement by UTC-day blocks; positive is better."""
    by_day: dict[int, list[float]] = defaultdict(list)
    for ts_ms, value in observations:
        if math.isfinite(value):
            by_day[int(ts_ms) // DAY_MS].append(float(value))
    blocks = [(sum(values), len(values)) for _, values in sorted(by_day.items()) if values]
    observed = (
        sum(total for total, _ in blocks) / sum(n for _, n in blocks)
        if blocks
        else None
    )
    out: dict[str, Any] = {
        "days": len(blocks),
        "samples": int(samples),
        "mean_improvement": observed,
    }
    if len(blocks) < 2 or samples <= 0:
        out.update(
            state="INSUFFICIENT_BLOCKS",
            ci_lower=None,
            ci_upper=None,
            p_nonpositive=None,
        )
        return out
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(samples):
        chosen = [rng.choice(blocks) for _ in blocks]
        draws.append(
            sum(total for total, _ in chosen) / sum(n for _, n in chosen)
        )
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    out.update(
        state="BOOTSTRAPPED",
        ci_lower=lo,
        ci_upper=hi,
        p_nonpositive=sum(value <= 0.0 for value in draws) / len(draws),
    )
    return out


def _log_loss(y: int, p: float) -> float:
    q = min(1.0 - EPS, max(EPS, p))
    return -(y * math.log(q) + (1 - y) * math.log(1 - q))


def validate_binary(
    rows: Sequence[OrderExample],
    attr: str,
    *,
    bandwidth: float,
    folds: int,
    min_train: int,
    min_test: int,
    embargo_ms: int,
    bootstrap_samples: int,
    seed_key: str,
) -> dict[str, Any]:
    fold_rows: list[dict[str, Any]] = []
    brier_obs: list[tuple[int, float]] = []
    log_obs: list[tuple[int, float]] = []
    for fold_no, (train, test) in enumerate(
        walk_forward_splits(
            rows,
            min_train=min_train,
            min_test=min_test,
            folds=folds,
            embargo_ms=embargo_ms,
        ),
        1,
    ):
        y_train = [int(getattr(row, attr)) for row in train]
        if len(set(y_train)) < 2:
            fold_rows.append(
                {
                    "fold": fold_no,
                    "state": "INSUFFICIENT_STATE_VARIATION",
                    "train_n": len(train),
                    "test_n": len(test),
                }
            )
            continue
        kernel = Kernel.fit([row.x for row in train], bandwidth)
        base = min(1.0 - EPS, max(EPS, statistics.fmean(y_train)))
        brier_fold: list[float] = []
        log_fold: list[float] = []
        for row in test:
            y = int(getattr(row, attr))
            p = min(1.0 - EPS, max(EPS, predict_mean(kernel, row.x, y_train)))
            db = (y - base) ** 2 - (y - p) ** 2
            dl = _log_loss(y, base) - _log_loss(y, p)
            brier_obs.append((row.ts_ms, db))
            log_obs.append((row.ts_ms, dl))
            brier_fold.append(db)
            log_fold.append(dl)
        fold_rows.append(
            {
                "fold": fold_no,
                "state": "OOS_SCORED",
                "train_n": len(train),
                "test_n": len(test),
                "train_end_ts_ms": max(row.ts_ms for row in train),
                "test_start_ts_ms": min(row.ts_ms for row in test),
                "brier_improvement": statistics.fmean(brier_fold),
                "log_loss_improvement": statistics.fmean(log_fold),
            }
        )
    scored = [row for row in fold_rows if row["state"] == "OOS_SCORED"]
    return {
        "state": "OOS_SCORED" if scored else "INSUFFICIENT_EVIDENCE",
        "requested_folds": folds,
        "scored_folds": len(scored),
        "folds": fold_rows,
        "positive_brier_fold_fraction": (
            sum(row["brier_improvement"] > 0 for row in scored) / len(scored)
            if scored
            else None
        ),
        "worst_brier_improvement": min(
            (row["brier_improvement"] for row in scored), default=None
        ),
        "brier_day_block_bootstrap": _day_block_bootstrap(
            brier_obs, samples=bootstrap_samples, seed_key=seed_key + ":brier"
        ),
        "log_loss_day_block_bootstrap": _day_block_bootstrap(
            log_obs, samples=bootstrap_samples, seed_key=seed_key + ":log"
        ),
    }


class _MarkoutRow:
    def __init__(self, row: OrderExample, horizon: str):
        self.ts_ms = row.ts_ms
        self.label_ts_ms = row.markout_ts_ms[horizon]
        self.order_id = row.order_id
        self.x = row.x
        self.y = row.markouts[horizon]


def validate_markout(
    rows: Sequence[OrderExample],
    horizon: str,
    *,
    bandwidth: float,
    folds: int,
    min_train: int,
    min_test: int,
    embargo_ms: int,
    bootstrap_samples: int,
    seed_key: str,
) -> dict[str, Any]:
    sample = [_MarkoutRow(row, horizon) for row in rows if horizon in row.markouts]
    fold_rows: list[dict[str, Any]] = []
    mse_obs: list[tuple[int, float]] = []
    for fold_no, (train, test) in enumerate(
        walk_forward_splits(
            sample,
            min_train=min_train,
            min_test=min_test,
            folds=folds,
            embargo_ms=embargo_ms,
        ),
        1,
    ):
        labels = [row.y for row in train]
        kernel = Kernel.fit([row.x for row in train], bandwidth)
        base = statistics.fmean(labels)
        diffs: list[float] = []
        for row in test:
            pred = predict_mean(kernel, row.x, labels)
            diff = (row.y - base) ** 2 - (row.y - pred) ** 2
            mse_obs.append((row.ts_ms, diff))
            diffs.append(diff)
        fold_rows.append(
            {
                "fold": fold_no,
                "state": "OOS_SCORED",
                "train_n": len(train),
                "test_n": len(test),
                "mse_improvement": statistics.fmean(diffs),
            }
        )
    return {
        "state": "OOS_SCORED" if fold_rows else "INSUFFICIENT_EVIDENCE",
        "requested_folds": folds,
        "scored_folds": len(fold_rows),
        "folds": fold_rows,
        "positive_mse_fold_fraction": (
            sum(row["mse_improvement"] > 0 for row in fold_rows) / len(fold_rows)
            if fold_rows
            else None
        ),
        "worst_mse_improvement": min(
            (row["mse_improvement"] for row in fold_rows), default=None
        ),
        "mse_day_block_bootstrap": _day_block_bootstrap(
            mse_obs, samples=bootstrap_samples, seed_key=seed_key + ":mse"
        ),
    }


def validate_joint(
    rows: Sequence[JointExample],
    *,
    bandwidth: float,
    folds: int,
    min_train: int,
    min_test: int,
    embargo_ms: int,
    bootstrap_samples: int,
    seed_key: str,
) -> dict[str, Any]:
    fold_rows: list[dict[str, Any]] = []
    marginal_obs: list[tuple[int, float]] = []
    empirical_obs: list[tuple[int, float]] = []
    for fold_no, (train, test) in enumerate(
        walk_forward_splits(
            rows,
            min_train=min_train,
            min_test=min_test,
            folds=folds,
            embargo_ms=embargo_ms,
        ),
        1,
    ):
        labels = [row.state for row in train]
        if len(set(labels)) < 2:
            fold_rows.append(
                {
                    "fold": fold_no,
                    "state": "INSUFFICIENT_STATE_VARIATION",
                    "train_n": len(train),
                    "test_n": len(test),
                }
            )
            continue
        kernel = Kernel.fit([row.x for row in train], bandwidth)
        counts = Counter(labels)
        marginal_fold: list[float] = []
        empirical_fold: list[float] = []
        for row in test:
            dist = predict_distribution(kernel, row.x, labels)
            direct = -math.log(max(EPS, dist.get(row.state, 0.0)))
            marginal = -math.log(product_marginal_probability(labels, row.state))
            empirical = -math.log(max(EPS, counts[row.state] / len(labels)))
            dm, de = marginal - direct, empirical - direct
            marginal_obs.append((row.ts_ms, dm))
            empirical_obs.append((row.ts_ms, de))
            marginal_fold.append(dm)
            empirical_fold.append(de)
        fold_rows.append(
            {
                "fold": fold_no,
                "state": "OOS_SCORED",
                "train_n": len(train),
                "test_n": len(test),
                "nll_improvement_vs_product_marginals": statistics.fmean(marginal_fold),
                "nll_improvement_vs_empirical_joint": statistics.fmean(empirical_fold),
            }
        )
    scored = [row for row in fold_rows if row["state"] == "OOS_SCORED"]
    return {
        "state": "OOS_SCORED" if scored else "INSUFFICIENT_EVIDENCE",
        "requested_folds": folds,
        "scored_folds": len(scored),
        "folds": fold_rows,
        "positive_vs_marginal_fold_fraction": (
            sum(row["nll_improvement_vs_product_marginals"] > 0 for row in scored)
            / len(scored)
            if scored
            else None
        ),
        "worst_vs_marginal_improvement": min(
            (row["nll_improvement_vs_product_marginals"] for row in scored),
            default=None,
        ),
        "vs_product_marginals_day_block_bootstrap": _day_block_bootstrap(
            marginal_obs,
            samples=bootstrap_samples,
            seed_key=seed_key + ":marginal",
        ),
        "vs_empirical_joint_day_block_bootstrap": _day_block_bootstrap(
            empirical_obs,
            samples=bootstrap_samples,
            seed_key=seed_key + ":empirical",
        ),
    }


def analyze_walk_forward(
    events: Sequence[Any],
    sha: str,
    *,
    bandwidth: float = 1.0,
    folds: int = 4,
    min_order_train: int = 80,
    min_order_test: int = 20,
    min_markout_train: int = 60,
    min_markout_test: int = 15,
    min_joint_train: int = 60,
    min_joint_test: int = 15,
    embargo_ms: int = 0,
    bootstrap_samples: int = 1000,
) -> dict[str, Any]:
    orders, order_stats = build_orders(events, sha)
    joint, joint_stats = build_joint(orders)

    by_strategy: dict[str, list[OrderExample]] = defaultdict(list)
    for row in orders:
        by_strategy[row.strategy].append(row)
    strategy_validation: dict[str, Any] = {}
    for strategy, sample in sorted(by_strategy.items()):
        strategy_validation[strategy] = {
            "fill": validate_binary(
                sample,
                "fill",
                bandwidth=bandwidth,
                folds=folds,
                min_train=min_order_train,
                min_test=min_order_test,
                embargo_ms=embargo_ms,
                bootstrap_samples=bootstrap_samples,
                seed_key=f"{sha}:{strategy}:fill",
            ),
            "completion": validate_binary(
                sample,
                "complete",
                bandwidth=bandwidth,
                folds=folds,
                min_train=min_order_train,
                min_test=min_order_test,
                embargo_ms=embargo_ms,
                bootstrap_samples=bootstrap_samples,
                seed_key=f"{sha}:{strategy}:completion",
            ),
            "markouts": {
                horizon: validate_markout(
                    sample,
                    horizon,
                    bandwidth=bandwidth,
                    folds=folds,
                    min_train=min_markout_train,
                    min_test=min_markout_test,
                    embargo_ms=embargo_ms,
                    bootstrap_samples=bootstrap_samples,
                    seed_key=f"{sha}:{strategy}:{horizon}",
                )
                for horizon in MARKOUTS
            },
        }

    by_joint: dict[tuple[str, tuple[str, ...]], list[JointExample]] = defaultdict(list)
    for row in joint:
        by_joint[(row.strategy, row.leg_signature)].append(row)
    joint_validation: dict[str, Any] = {}
    for (strategy, signature), sample in sorted(
        by_joint.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        key = strategy + "::" + "|".join(signature)
        joint_validation[key] = validate_joint(
            sample,
            bandwidth=bandwidth,
            folds=folds,
            min_train=min_joint_train,
            min_test=min_joint_test,
            embargo_ms=embargo_ms,
            bootstrap_samples=bootstrap_samples,
            seed_key=f"{sha}:{key}",
        )

    return {
        "schema": SCHEMA,
        "model_sha": sha,
        "paper_only": True,
        "authenticated_execution": False,
        "read_only": True,
        "promotion_allowed": False,
        "decision": "MORE_EVIDENCE_REQUIRED",
        "validation_authority": "BLOCKED_WALK_FORWARD",
        "single_terminal_holdout_role": "DIAGNOSTIC_ONLY",
        "protocol": {
            "expanding_window": True,
            "test_folds_non_overlapping": True,
            "labels_mature_before_test_start": True,
            "embargo_ms": embargo_ms,
            "bootstrap_block": "UTC_DAY",
            "bootstrap_samples": bootstrap_samples,
            "folds_requested": folds,
            "product_of_marginals_role": "BENCHMARK_ONLY",
        },
        "order_stats": order_stats,
        "joint_stats": joint_stats,
        "strategy_validation": strategy_validation,
        "joint_validation": joint_validation,
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bandwidth", type=float, default=1.0)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--embargo-ms", type=int, default=0)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    args = parser.parse_args()
    report = analyze_walk_forward(
        load_ledger(args.ledger, args.model_sha),
        args.model_sha,
        bandwidth=args.bandwidth,
        folds=args.folds,
        embargo_ms=args.embargo_ms,
        bootstrap_samples=args.bootstrap_samples,
    )
    _atomic_json(args.output, report)
    print(json.dumps({"output": str(args.output), "decision": report["decision"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
