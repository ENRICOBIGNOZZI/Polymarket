#!/usr/bin/env python3
"""Scoring/report layer for the hardened V7 learned-execution consumer."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

import v7_learned_execution_model_base as b
from v7_learned_execution_schema import *

Kernel = b.Kernel
split = b.split
predict_mean = b.predict_mean
predict_distribution = b.predict_distribution
product_marginal_probability = b.product_marginal_probability
atomic_json = b.atomic_json
MARKOUTS = b.MARKOUTS
EPS = b.EPS

def binary_report(train: Sequence[OrderExample], test: Sequence[OrderExample], attr: str, bandwidth: float) -> dict[str, Any]:
    y_train = [int(getattr(row, attr)) for row in train]
    y_test = [int(getattr(row, attr)) for row in test]
    if len(set(y_train)) < 2:
        return {"state": "INSUFFICIENT_STATE_VARIATION", "train_n": len(train), "test_n": len(test)}
    kernel = Kernel.fit([row.x for row in train], bandwidth)
    probs = [min(1.0 - EPS, max(EPS, predict_mean(kernel, row.x, y_train))) for row in test]
    base = min(1.0 - EPS, max(EPS, statistics.fmean(y_train)))
    def logloss(p: Sequence[float]) -> float:
        return statistics.fmean(-(y * math.log(q) + (1 - y) * math.log(1 - q)) for y, q in zip(y_test, p))
    def brier(p: Sequence[float]) -> float:
        return statistics.fmean((y - q) ** 2 for y, q in zip(y_test, p))
    return {
        "state": "OOS_SCORED", "train_n": len(train), "test_n": len(test),
        "oos_log_loss": logloss(probs), "baseline_log_loss": logloss([base] * len(test)),
        "oos_brier": brier(probs), "baseline_brier": brier([base] * len(test)),
        "test_positive_rate": statistics.fmean(y_test),
    }


def markout_report(train: Sequence[OrderExample], test: Sequence[OrderExample], bandwidth: float, min_train: int, min_test: int, embargo_ms: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cutoff = min(row.ts_ms for row in test) - max(0, embargo_ms)
    for horizon in MARKOUTS:
        a = [(row.x, row.markouts[horizon]) for row in train if horizon in row.markouts and row.markout_ts_ms[horizon] <= cutoff]
        z = [(row.x, row.markouts[horizon]) for row in test if horizon in row.markouts]
        if len(a) < min_train or len(z) < min_test:
            out[horizon] = {"state": "INSUFFICIENT_EVIDENCE", "train_n": len(a), "test_n": len(z)}
            continue
        kernel = Kernel.fit([x for x, _ in a], bandwidth)
        labels = [y for _, y in a]
        pred = [predict_mean(kernel, x, labels) for x, _ in z]
        truth = [y for _, y in z]
        base = statistics.fmean(labels)
        rmse = lambda p: math.sqrt(statistics.fmean((y - q) ** 2 for y, q in zip(truth, p)))
        out[horizon] = {"state": "OOS_SCORED", "train_n": len(a), "test_n": len(z), "oos_rmse": rmse(pred), "baseline_rmse": rmse([base] * len(z)), "test_mean_markout": statistics.fmean(truth)}
    return out


def strategy_reports(orders: Sequence[OrderExample], *, bandwidth: float, test_fraction: float, embargo_ms: int, min_order_train: int, min_order_test: int, min_markout_train: int, min_markout_test: int) -> dict[str, Any]:
    groups: dict[str, list[OrderExample]] = defaultdict(list)
    for order in orders:
        groups[order.strategy].append(order)
    out: dict[str, Any] = {}
    for strategy, sample in sorted(groups.items()):
        try:
            train, test = split(sample, min_order_train, min_order_test, test_fraction, embargo_ms)
        except ExecutionModelError as exc:
            out[strategy] = {"state": "INSUFFICIENT_EVIDENCE", "n": len(sample), "reason": str(exc)}
            continue
        out[strategy] = {
            "state": "OOS_SCORED", "train_n": len(train), "test_n": len(test),
            "train_end_ts_ms": max(row.ts_ms for row in train), "test_start_ts_ms": min(row.ts_ms for row in test),
            "fill_model": binary_report(train, test, "fill", bandwidth),
            "completion_model": binary_report(train, test, "complete", bandwidth),
            "markout_models": markout_report(train, test, bandwidth, min_markout_train, min_markout_test, embargo_ms),
        }
    return out


def joint_report(rows: Sequence[JointExample], bandwidth: float, test_fraction: float, embargo_ms: int, min_train: int, min_test: int) -> dict[str, Any]:
    groups: dict[tuple[str, tuple[str, ...]], list[JointExample]] = defaultdict(list)
    for row in rows:
        groups[(row.strategy, row.leg_signature)].append(row)
    out: dict[str, Any] = {}
    for (strategy, signature), sample in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
        key = strategy + "::" + "|".join(signature)
        try:
            train, test = split(sample, min_train, min_test, test_fraction, embargo_ms)
        except ExecutionModelError as exc:
            out[key] = {"state": "INSUFFICIENT_EVIDENCE", "n": len(sample), "reason": str(exc), "strategy": strategy, "leg_signature": list(signature)}
            continue
        labels = [row.state for row in train]
        if len(set(labels)) < 2:
            out[key] = {"state": "INSUFFICIENT_STATE_VARIATION", "train_n": len(train), "test_n": len(test), "strategy": strategy, "leg_signature": list(signature)}
            continue
        kernel = Kernel.fit([row.x for row in train], bandwidth)
        distributions = [predict_distribution(kernel, row.x, labels) for row in test]
        direct = statistics.fmean(-math.log(max(EPS, dist.get(row.state, 0.0))) for row, dist in zip(test, distributions))
        marginal = statistics.fmean(-math.log(product_marginal_probability(labels, row.state)) for row in test)
        counts = Counter(labels)
        empirical = statistics.fmean(-math.log(max(EPS, counts[row.state] / len(labels))) for row in test)
        out[key] = {
            "state": "OOS_SCORED", "train_n": len(train), "test_n": len(test), "strategy": strategy,
            "leg_signature": list(signature), "oos_joint_nll": direct,
            "empirical_joint_baseline_nll": empirical, "product_of_marginals_benchmark_nll": marginal,
            "uses_product_of_marginals_for_decision": False, "observed_joint_states": sorted(set(labels)),
        }
    return out


def analyze(events: Sequence[Any], sha: str, *, test_fraction: float = 0.25, embargo_ms: int = 0, min_order_train: int = 50, min_order_test: int = 20, min_markout_train: int = 20, min_markout_test: int = 10, min_joint_train: int = 30, min_joint_test: int = 10, bandwidth: float = 1.0) -> dict[str, Any]:
    orders, order_stats = build_orders(events, sha)
    joint, joint_stats = build_joint(orders)
    signatures = sorted({(row.strategy, row.leg_signature) for row in joint}, key=lambda value: (value[0], value[1]))
    return {
        "schema": "polymarket_v7_learned_execution_research_v4", "model_sha": sha,
        "paper_only": True, "authenticated_execution": False, "read_only": True,
        "promotion_allowed": False, "decision": "MORE_EVIDENCE_REQUIRED",
        "causal_contract": {
            "predictors_from": "ORDER_SUBMITTED_only", "missing_executable_book_inputs": "exclude_not_zero_impute",
            "exact_execution_lineage": "strategy_order_fill_markout_token_side_leg_bundle",
            "labels_from_future_events": True, "training_labels_mature_before_test_start": True,
            "markout_source": "append_only_MARKOUT_by_fill_id", "markout_horizon_maturity": "exchange_and_receive_clock_enforced",
            "markout_fill_coverage": "all_fills_required_per_order_horizon", "chronological_split": True,
            "embargo_ms": max(0, embargo_ms), "mixed_sha_allowed": False,
            "order_model_pooling": "strategy_stratified_only",
            "joint_grouping": "bundle_id_with_explicit_expected_leg_signature",
            "joint_pooling": "strategy_and_leg_signature_stratified",
            "joint_feature_encoding": "ordered_per_leg_concat_plus_bundle_summary",
            "joint_model": "direct_kernel_joint_state_distribution", "product_of_marginals_role": "benchmark_only",
        },
        "feature_names": list(FEATURES),
        "joint_feature_schema_by_signature": {
            strategy + "::" + "|".join(signature): list(joint_feature_names(len(signature), signature))
            for strategy, signature in signatures
        },
        "order_stats": order_stats, "joint_stats": joint_stats,
        "strategy_models": strategy_reports(
            orders, bandwidth=bandwidth, test_fraction=test_fraction, embargo_ms=embargo_ms,
            min_order_train=min_order_train, min_order_test=min_order_test,
            min_markout_train=min_markout_train, min_markout_test=min_markout_test,
        ),
        "joint_state_models": joint_report(joint, bandwidth, test_fraction, embargo_ms, min_joint_train, min_joint_test),
    }


def load_ledger(path: Path, sha: str) -> list[Any]:
    try:
        from v7_execution_ledger import load_events
        return load_events(path, expected_model_sha=sha)
    except Exception as exc:
        raise ExecutionModelError(f"canonical_ledger_rejected:{exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test-fraction", type=float, default=0.25)
    parser.add_argument("--embargo-ms", type=int, default=0)
    parser.add_argument("--bandwidth", type=float, default=1.0)
    parser.add_argument("--min-order-train", type=int, default=50)
    parser.add_argument("--min-order-test", type=int, default=20)
    parser.add_argument("--min-markout-train", type=int, default=20)
    parser.add_argument("--min-markout-test", type=int, default=10)
    parser.add_argument("--min-joint-train", type=int, default=30)
    parser.add_argument("--min-joint-test", type=int, default=10)
    args = parser.parse_args()
    report = analyze(
        load_ledger(args.ledger, args.model_sha), args.model_sha,
        test_fraction=args.test_fraction, embargo_ms=args.embargo_ms, bandwidth=args.bandwidth,
        min_order_train=args.min_order_train, min_order_test=args.min_order_test,
        min_markout_train=args.min_markout_train, min_markout_test=args.min_markout_test,
        min_joint_train=args.min_joint_train, min_joint_test=args.min_joint_test,
    )
    atomic_json(args.output, report)
    print(json.dumps({
        "output": str(args.output), "decision": report["decision"],
        "resolved_orders": report["order_stats"].get("resolved_feature_complete_orders", 0),
        "joint_examples": report["joint_stats"].get("joint_examples", 0),
    }, sort_keys=True))
    return 0


__all__ = [
    "FEATURES", "ExecutionModelError", "JointExample", "Kernel", "OrderExample", "analyze", "build_joint",
    "build_orders", "joint_feature_names", "joint_report", "load_ledger", "main", "predict_distribution",
    "predict_mean", "product_marginal_probability", "split",
]
