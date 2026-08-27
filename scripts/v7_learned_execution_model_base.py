#!/usr/bin/env python3
"""Read-only learned execution research on the canonical V7 PAPER ledger.

Predictors come only from ORDER_SUBMITTED state. Labels come only from later
resolved order/fill/markout events. Missing executable-book inputs are excluded,
not silently converted to zero. Multi-leg execution is learned as a direct
joint state distribution; products of marginal fill probabilities are benchmark-only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TERMINAL = frozenset({"ABORTED", "CANCELED", "CANCELLED", "CLOSED", "COMPLETE", "DONE", "EXPIRED", "FILLED", "REJECTED", "TIMEOUT"})
MARKOUTS = ("1s", "10s", "45s", "60s", "300s")
EPS = 1e-12
FEATURES = (
    "log_queue_plus_one", "log_bid_depth_plus_one", "log_ask_depth_plus_one",
    "spread", "book_imbalance", "size_to_min_touch_depth",
    "decision_receive_latency_s", "timeout_s", "timeout_present",
    "predicted_alpha", "predicted_alpha_present", "expected_ev", "expected_ev_present",
    "action_cross", "action_join", "action_improve", "action_fade",
    "action_taker", "action_maker", "action_cancel_replace",
)
JOINT_FEATURES = tuple(f"mean_{name}" for name in FEATURES) + (
    "leg_count", "max_log_queue_plus_one", "max_size_to_min_touch_depth",
)


class ExecutionModelError(ValueError):
    pass


@dataclass(frozen=True)
class OrderExample:
    order_id: str
    bundle_id: str | None
    leg_id: str | None
    ts_ms: int
    label_ts_ms: int
    x: tuple[float, ...]
    fill: int
    complete: int
    state: str
    markouts: dict[str, float]
    markout_ts_ms: dict[str, int]


@dataclass(frozen=True)
class JointExample:
    group_id: str
    leg_count: int
    ts_ms: int
    label_ts_ms: int
    x: tuple[float, ...]
    state: str


def get(event: Any, name: str, default: Any = None) -> Any:
    return event.get(name, default) if isinstance(event, dict) else getattr(event, name, default)


def finite_optional(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def required_finite(event: Any, name: str, *, nonnegative: bool = False, positive: bool = False) -> float:
    value = finite_optional(get(event, name))
    if value is None:
        raise ExecutionModelError(f"features:missing_or_nonfinite:{name}")
    if nonnegative and value < 0.0:
        raise ExecutionModelError(f"features:negative:{name}")
    if positive and value <= 0.0:
        raise ExecutionModelError(f"features:nonpositive:{name}")
    return value


def optional_value(event: Any, name: str, *, nonnegative: bool = False) -> tuple[float, float]:
    raw = get(event, name)
    if raw is None:
        return 0.0, 0.0
    value = finite_optional(raw)
    if value is None or (nonnegative and value < 0.0):
        raise ExecutionModelError(f"features:invalid_optional:{name}")
    return value, 1.0


def features(event: Any) -> tuple[float, ...]:
    if not str(get(event, "book_snapshot_id", "") or "").strip():
        raise ExecutionModelError("features:missing_book_snapshot_id")
    bid = required_finite(event, "bid", nonnegative=True)
    ask = required_finite(event, "ask", nonnegative=True)
    bd = required_finite(event, "bid_depth", nonnegative=True)
    ad = required_finite(event, "ask_depth", nonnegative=True)
    queue = required_finite(event, "queue_ahead", nonnegative=True)
    size = required_finite(event, "intended_size", positive=True)
    if bid > ask:
        raise ExecutionModelError("features:crossed_book")
    receive = int(get(event, "receive_ts_ms", 0) or 0)
    decision = int(get(event, "decision_ts_ms", 0) or 0)
    if receive <= 0 or decision <= 0 or decision < receive:
        raise ExecutionModelError("features:invalid_receive_decision_clock")
    action = str(get(event, "intended_action", "") or "").upper().strip()
    if not action:
        raise ExecutionModelError("features:missing_action")
    timeout, timeout_present = optional_value(event, "timeout_ms", nonnegative=True)
    alpha, alpha_present = optional_value(event, "predicted_alpha")
    ev, ev_present = optional_value(event, "expected_ev")
    touch = min(bd, ad) if bd > 0.0 and ad > 0.0 else max(bd, ad)
    if touch <= 0.0:
        raise ExecutionModelError("features:zero_executable_touch_depth")
    depth = bd + ad
    if depth <= 0.0:
        raise ExecutionModelError("features:zero_book_depth")
    flags = tuple(float(token in action) for token in ("CROSS", "JOIN", "IMPROVE", "FADE", "TAKER", "MAKER"))
    return (
        math.log1p(queue), math.log1p(bd), math.log1p(ad), ask - bid,
        (bd - ad) / depth, size / touch, (decision - receive) / 1000.0,
        timeout / 1000.0, timeout_present, alpha, alpha_present, ev, ev_present,
        *flags, float("CANCEL" in action or "REPLACE" in action),
    )


def event_ts(event: Any) -> int:
    ts = int(get(event, "recorded_ts_ms", 0) or 0)
    if ts <= 0:
        raise ExecutionModelError("event:missing_recorded_ts_ms")
    return ts


def validate_stream(events: Sequence[Any], sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ExecutionModelError("model_sha:not_exact_git_sha")
    for i, event in enumerate(events, 1):
        if str(get(event, "model_sha", "")) != sha:
            raise ExecutionModelError(f"event_{i}:mixed_sha")
        if get(event, "paper_only", None) is not True:
            raise ExecutionModelError(f"event_{i}:not_paper_only")
        if get(event, "authenticated_execution", None) is not False:
            raise ExecutionModelError(f"event_{i}:authenticated_execution_forbidden")


def build_orders(events: Sequence[Any], sha: str) -> tuple[list[OrderExample], dict[str, int]]:
    validate_stream(events, sha)
    submitted: dict[str, Any] = {}
    later: dict[str, list[Any]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for event in events:
        typ = str(get(event, "event_type", "")).upper()
        oid = str(get(event, "order_id", "") or "")
        if typ == "ORDER_SUBMITTED":
            if not oid or oid in submitted:
                raise ExecutionModelError("submission:missing_or_duplicate_order_id")
            submitted[oid] = event
            stats["submissions"] += 1
        elif oid:
            later[oid].append(event)

    out: list[OrderExample] = []
    for oid, sub in submitted.items():
        try:
            x = features(sub)
        except ExecutionModelError:
            stats["excluded_missing_or_invalid_features"] += 1
            continue
        intended = required_finite(sub, "intended_size", positive=True)
        decision_ts = int(get(sub, "decision_ts_ms", 0) or 0)
        filled = 0.0
        terminal = False
        explicit_complete = False
        resolution_ts = 0
        fill_info: dict[str, tuple[float, int, int]] = {}
        events_for_order = later.get(oid, [])

        # Pass 1: establish execution outcomes and immutable fill identities.
        for event in events_for_order:
            typ = str(get(event, "event_type", "")).upper()
            state = str(get(event, "order_state", "") or "").upper()
            ts = event_ts(event)
            if typ == "FILL":
                fid = str(get(event, "fill_id", "") or "")
                if not fid:
                    raise ExecutionModelError(f"fill:missing_fill_id:{oid}")
                if fid in fill_info:
                    raise ExecutionModelError(f"fill:duplicate_fill_id:{fid}")
                qty = required_finite(event, "filled_size", positive=True)
                ex_ts = int(get(event, "exchange_ts_ms", 0) or 0)
                if ex_ts <= 0:
                    raise ExecutionModelError(f"fill:missing_exchange_ts:{fid}")
                fill_info[fid] = (qty, ts, ex_ts)
                filled += qty
                if get(event, "complete", None) is True:
                    explicit_complete = True
                    terminal = True
                    resolution_ts = max(resolution_ts, ts)
                if filled >= intended * (1.0 - 1e-9):
                    terminal = True
                    resolution_ts = max(resolution_ts, ts)
            if state in TERMINAL:
                terminal = True
                resolution_ts = max(resolution_ts, ts)
            if get(event, "complete", None) is True:
                explicit_complete = True

        complete = explicit_complete or filled >= intended * (1.0 - 1e-9)
        if complete and resolution_ts <= 0:
            resolution_ts = max((info[1] for info in fill_info.values()), default=0)
        if not terminal or resolution_ts <= 0:
            stats["unresolved_orders"] += 1
            continue

        # Pass 2: read append-only MARKOUT labels linked to stable fill_id.
        num: Counter[str] = Counter()
        den: Counter[str] = Counter()
        markout_ts: dict[str, int] = {}
        seen_markout: set[tuple[str, str]] = set()
        for event in events_for_order:
            if str(get(event, "event_type", "")).upper() != "MARKOUT":
                continue
            fid = str(get(event, "fill_id", "") or "")
            if fid not in fill_info:
                raise ExecutionModelError(f"markout:orphan_fill_id:{fid or 'missing'}")
            marks = get(event, "markouts", {}) or {}
            if not isinstance(marks, dict) or len(marks) != 1:
                raise ExecutionModelError(f"markout:invalid_horizon_payload:{fid}")
            horizon, raw_value = next(iter(marks.items()))
            if horizon not in MARKOUTS:
                raise ExecutionModelError(f"markout:unsupported_horizon:{horizon}")
            key = (fid, horizon)
            if key in seen_markout:
                raise ExecutionModelError(f"markout:duplicate_fill_horizon:{fid}:{horizon}")
            seen_markout.add(key)
            value = finite_optional(raw_value)
            if value is None:
                raise ExecutionModelError(f"markout:nonfinite:{fid}:{horizon}")
            qty, fill_recorded_ts, fill_exchange_ts = fill_info[fid]
            mark_recorded_ts = event_ts(event)
            mark_exchange_ts = int(get(event, "exchange_ts_ms", 0) or 0)
            if mark_exchange_ts <= 0 or mark_recorded_ts < fill_recorded_ts or mark_exchange_ts < fill_exchange_ts:
                raise ExecutionModelError(f"markout:noncausal_clock:{fid}:{horizon}")
            num[horizon] += qty * value
            den[horizon] += qty
            markout_ts[horizon] = max(markout_ts.get(horizon, 0), mark_recorded_ts)

        state = "COMPLETE" if complete else "PARTIAL" if filled > 0.0 else "NO_FILL"
        bundle = str(get(sub, "bundle_id", "") or "") or None
        leg = str(get(sub, "leg_id", "") or "") or None
        marks = {h: num[h] / den[h] for h in MARKOUTS if den[h] > 0.0}
        out.append(OrderExample(oid, bundle, leg, decision_ts, resolution_ts, x, int(filled > 0.0), int(complete), state, marks, markout_ts))
        stats[f"state_{state.lower()}"] += 1
    out.sort(key=lambda row: (row.ts_ms, row.order_id))
    stats["resolved_feature_complete_orders"] = len(out)
    return out, dict(stats)


def build_joint(orders: Sequence[OrderExample]) -> tuple[list[JointExample], dict[str, int]]:
    groups: dict[str, list[OrderExample]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for order in orders:
        if order.bundle_id:
            groups[order.bundle_id].append(order)
        else:
            stats["skipped_missing_bundle_id"] += 1
    out: list[JointExample] = []
    for gid, legs in groups.items():
        if len(legs) < 2:
            continue
        if any(not leg.leg_id for leg in legs) or len({leg.leg_id for leg in legs}) != len(legs):
            stats["skipped_missing_or_duplicate_leg_id"] += 1
            continue
        legs = sorted(legs, key=lambda row: str(row.leg_id))
        mean = tuple(statistics.fmean(row.x[j] for row in legs) for j in range(len(FEATURES)))
        x = mean + (float(len(legs)), max(row.x[0] for row in legs), max(row.x[5] for row in legs))
        out.append(JointExample(gid, len(legs), max(row.ts_ms for row in legs), max(row.label_ts_ms for row in legs), x, "|".join(row.state for row in legs)))
    out.sort(key=lambda row: (row.ts_ms, row.group_id))
    stats["joint_examples"] = len(out)
    return out, dict(stats)


def split(rows: Sequence[Any], min_train: int, min_test: int, test_fraction: float, embargo_ms: int) -> tuple[list[Any], list[Any]]:
    if not 0.05 <= test_fraction <= 0.5:
        raise ExecutionModelError("split:test_fraction_out_of_range")
    rows = sorted(rows, key=lambda row: (int(row.ts_ms), str(getattr(row, "order_id", getattr(row, "group_id", "")))))
    if len(rows) < min_train + min_test:
        raise ExecutionModelError("split:insufficient_examples")
    cut = min(max(min_train, int(len(rows) * (1.0 - test_fraction))), len(rows) - min_test)
    test = list(rows[cut:])
    test_start = int(test[0].ts_ms)
    cutoff = test_start - max(0, embargo_ms)
    train = [row for row in rows[:cut] if int(getattr(row, "label_ts_ms", row.ts_ms)) <= cutoff]
    if len(train) < min_train:
        raise ExecutionModelError("split:embargo_or_label_maturity_removed_training_sample")
    return train, test


@dataclass(frozen=True)
class Kernel:
    train_x: tuple[tuple[float, ...], ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    bandwidth: float

    @classmethod
    def fit(cls, rows: Sequence[Sequence[float]], bandwidth: float) -> "Kernel":
        if not rows or bandwidth <= 0.0 or not math.isfinite(bandwidth):
            raise ExecutionModelError("kernel:invalid_training_contract")
        width = len(rows[0])
        if width == 0 or any(len(row) != width for row in rows):
            raise ExecutionModelError("kernel:bad_shape")
        mean = tuple(statistics.fmean(row[j] for row in rows) for j in range(width))
        scale = []
        for j, mu in enumerate(mean):
            sd = math.sqrt(statistics.fmean((row[j] - mu) ** 2 for row in rows))
            scale.append(sd if sd > 1e-9 else 1.0)
        norm = tuple(tuple((value - mu) / sd for value, mu, sd in zip(row, mean, scale)) for row in rows)
        return cls(norm, mean, tuple(scale), bandwidth)

    def weights(self, row: Sequence[float]) -> list[float]:
        if len(row) != len(self.mean):
            raise ExecutionModelError("kernel:prediction_shape_mismatch")
        z = tuple((value - mu) / sd for value, mu, sd in zip(row, self.mean, self.scale))
        raw = [math.exp(-sum((a - b) ** 2 for a, b in zip(z, train)) / (2.0 * self.bandwidth ** 2)) for train in self.train_x]
        total = sum(raw)
        return [1.0 / len(raw)] * len(raw) if total <= EPS else [value / total for value in raw]


def predict_mean(kernel: Kernel, row: Sequence[float], labels: Sequence[float]) -> float:
    return sum(weight * float(label) for weight, label in zip(kernel.weights(row), labels))


def predict_distribution(kernel: Kernel, row: Sequence[float], labels: Sequence[str]) -> dict[str, float]:
    weights, classes = kernel.weights(row), sorted(set(labels))
    mass = {label: EPS for label in classes}
    for weight, label in zip(weights, labels):
        mass[label] += weight
    total = sum(mass.values())
    return {label: value / total for label, value in mass.items()}


def product_marginal_probability(train_states: Sequence[str], state: str) -> float:
    parts, target = [row.split("|") for row in train_states], state.split("|")
    if not parts or any(len(row) != len(target) for row in parts):
        return EPS
    probability = 1.0
    for j, label in enumerate(target):
        counts = Counter(row[j] for row in parts)
        probability *= (counts[label] + 1.0) / (len(parts) + 3.0)
    return max(EPS, probability)


def binary_report(train: Sequence[OrderExample], test: Sequence[OrderExample], attr: str, bandwidth: float) -> dict[str, Any]:
    y_train, y_test = [int(getattr(row, attr)) for row in train], [int(getattr(row, attr)) for row in test]
    if len(set(y_train)) < 2:
        return {"state": "INSUFFICIENT_STATE_VARIATION", "train_n": len(train), "test_n": len(test)}
    kernel = Kernel.fit([row.x for row in train], bandwidth)
    probs = [min(1.0 - EPS, max(EPS, predict_mean(kernel, row.x, y_train))) for row in test]
    base = min(1.0 - EPS, max(EPS, statistics.fmean(y_train)))
    logloss = lambda p: statistics.fmean(-(y * math.log(q) + (1 - y) * math.log(1 - q)) for y, q in zip(y_test, p))
    brier = lambda p: statistics.fmean((y - q) ** 2 for y, q in zip(y_test, p))
    return {"state": "OOS_SCORED", "train_n": len(train), "test_n": len(test), "oos_log_loss": logloss(probs), "baseline_log_loss": logloss([base] * len(test)), "oos_brier": brier(probs), "baseline_brier": brier([base] * len(test)), "test_positive_rate": statistics.fmean(y_test)}


def markout_report(train: Sequence[OrderExample], test: Sequence[OrderExample], bandwidth: float, min_train: int, min_test: int, embargo_ms: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    test_start = min(row.ts_ms for row in test)
    label_cutoff = test_start - max(0, embargo_ms)
    for horizon in MARKOUTS:
        a = [(row.x, row.markouts[horizon]) for row in train if horizon in row.markouts and row.markout_ts_ms.get(horizon, 0) <= label_cutoff]
        b = [(row.x, row.markouts[horizon]) for row in test if horizon in row.markouts]
        if len(a) < min_train or len(b) < min_test:
            out[horizon] = {"state": "INSUFFICIENT_EVIDENCE", "train_n": len(a), "test_n": len(b)}
            continue
        kernel, labels = Kernel.fit([x for x, _ in a], bandwidth), [y for _, y in a]
        pred, truth = [predict_mean(kernel, x, labels) for x, _ in b], [y for _, y in b]
        base = statistics.fmean(labels)
        rmse = lambda p: math.sqrt(statistics.fmean((y - q) ** 2 for y, q in zip(truth, p)))
        out[horizon] = {"state": "OOS_SCORED", "train_n": len(a), "test_n": len(b), "oos_rmse": rmse(pred), "baseline_rmse": rmse([base] * len(b)), "test_mean_markout": statistics.fmean(truth)}
    return out


def joint_report(rows: Sequence[JointExample], bandwidth: float, test_fraction: float, embargo_ms: int, min_train: int, min_test: int) -> dict[str, Any]:
    grouped: dict[int, list[JointExample]] = defaultdict(list)
    for row in rows:
        grouped[row.leg_count].append(row)
    out: dict[str, Any] = {}
    for nlegs, sample in sorted(grouped.items()):
        try:
            train, test = split(sample, min_train, min_test, test_fraction, embargo_ms)
        except ExecutionModelError as exc:
            out[str(nlegs)] = {"state": "INSUFFICIENT_EVIDENCE", "n": len(sample), "reason": str(exc)}
            continue
        labels = [row.state for row in train]
        if len(set(labels)) < 2:
            out[str(nlegs)] = {"state": "INSUFFICIENT_STATE_VARIATION", "train_n": len(train), "test_n": len(test)}
            continue
        kernel = Kernel.fit([row.x for row in train], bandwidth)
        distributions = [predict_distribution(kernel, row.x, labels) for row in test]
        direct = statistics.fmean(-math.log(max(EPS, dist.get(row.state, 0.0))) for row, dist in zip(test, distributions))
        marginal = statistics.fmean(-math.log(product_marginal_probability(labels, row.state)) for row in test)
        counts = Counter(labels)
        empirical = statistics.fmean(-math.log(max(EPS, counts[row.state] / len(labels))) for row in test)
        out[str(nlegs)] = {"state": "OOS_SCORED", "train_n": len(train), "test_n": len(test), "oos_joint_nll": direct, "empirical_joint_baseline_nll": empirical, "product_of_marginals_benchmark_nll": marginal, "uses_product_of_marginals_for_decision": False, "observed_joint_states": sorted(set(labels))}
    return out


def analyze(events: Sequence[Any], sha: str, *, test_fraction: float = 0.25, embargo_ms: int = 0, min_order_train: int = 50, min_order_test: int = 20, min_markout_train: int = 20, min_markout_test: int = 10, min_joint_train: int = 30, min_joint_test: int = 10, bandwidth: float = 1.0) -> dict[str, Any]:
    orders, order_stats = build_orders(events, sha)
    train, test = split(orders, min_order_train, min_order_test, test_fraction, embargo_ms)
    joint, joint_stats = build_joint(orders)
    return {
        "schema": "polymarket_v7_learned_execution_research_v2", "model_sha": sha,
        "paper_only": True, "authenticated_execution": False, "read_only": True,
        "promotion_allowed": False, "decision": "MORE_EVIDENCE_REQUIRED",
        "causal_contract": {
            "predictors_from": "ORDER_SUBMITTED_only",
            "missing_executable_book_inputs": "exclude_not_zero_impute",
            "labels_from_future_events": True,
            "training_labels_mature_before_test_start": True,
            "markout_source": "append_only_MARKOUT_by_fill_id",
            "chronological_split": True,
            "embargo_ms": max(0, embargo_ms),
            "mixed_sha_allowed": False,
            "joint_grouping": "bundle_id_with_unique_leg_id",
            "joint_model": "direct_kernel_joint_state_distribution",
            "product_of_marginals_role": "benchmark_only",
        },
        "feature_names": list(FEATURES), "joint_feature_names": list(JOINT_FEATURES),
        "order_stats": order_stats, "joint_stats": joint_stats,
        "train_n": len(train), "test_n": len(test), "train_end_ts_ms": max(row.ts_ms for row in train), "test_start_ts_ms": min(row.ts_ms for row in test),
        "fill_model": binary_report(train, test, "fill", bandwidth),
        "completion_model": binary_report(train, test, "complete", bandwidth),
        "markout_models": markout_report(train, test, bandwidth, min_markout_train, min_markout_test, embargo_ms),
        "joint_state_models": joint_report(joint, bandwidth, test_fraction, embargo_ms, min_joint_train, min_joint_test),
    }


def load_ledger(path: Path, sha: str) -> list[Any]:
    try:
        from v7_execution_ledger import load_events
        return load_events(path, expected_model_sha=sha)
    except Exception as exc:
        raise ExecutionModelError(f"canonical_ledger_rejected:{exc}") from exc


def atomic_json(path: Path, value: dict[str, Any]) -> None:
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
        test_fraction=args.test_fraction, embargo_ms=args.embargo_ms,
        min_order_train=args.min_order_train, min_order_test=args.min_order_test,
        min_markout_train=args.min_markout_train, min_markout_test=args.min_markout_test,
        min_joint_train=args.min_joint_train, min_joint_test=args.min_joint_test,
        bandwidth=args.bandwidth,
    )
    atomic_json(args.output, report)
    print(json.dumps({"output": str(args.output), "decision": report["decision"], "train_n": report["train_n"], "test_n": report["test_n"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
