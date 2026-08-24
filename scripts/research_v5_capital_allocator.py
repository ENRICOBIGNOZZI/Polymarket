#!/usr/bin/env python3
"""Research-only V5 evidence-weighted capital allocation challenger.

The live V5 allocation remains unchanged. This module compares the fixed V5
capital split with a fail-closed shadow allocator on strictly future windows.
Source-strategy returns must already be net of spread, fees, slippage, queue/fill
realism, latency and adverse selection where applicable.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class AllocationPolicy:
    exploration_floor: float = 0.02
    minimum_reserve: float = 0.10
    max_strategy_fraction: float = 0.30
    min_trades: int = 30
    min_active_folds: int = 2
    max_bootstrap_pvalue: float = 0.05
    min_positive_fold_fraction: float = 0.50
    max_drawdown: float = 0.15


def _float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def load_v5_baseline(config: Mapping[str, Any]) -> tuple[dict[str, float], float]:
    multi = config.get("multi_strategy")
    if not isinstance(multi, Mapping) or multi.get("paper_only") is not True:
        raise ValueError("V5 paper-only multi_strategy configuration is required")
    raw = multi.get("strategies")
    if not isinstance(raw, list) or not raw:
        raise ValueError("multi_strategy.strategies must be non-empty")
    baseline: dict[str, float] = {}
    for item in raw:
        if not isinstance(item, Mapping) or not bool(item.get("enabled", True)):
            continue
        name = str(item.get("name", ""))
        fraction = _float(item.get("capital_fraction"), -1.0)
        if not name or fraction <= 0.0:
            raise ValueError("enabled strategies need names and positive capital_fraction")
        baseline[name] = fraction
    reserve = _float(multi.get("reserve_fraction"), -1.0)
    if reserve < 0.0 or abs(sum(baseline.values()) + reserve - 1.0) > 1e-9:
        raise ValueError("baseline capital fractions plus reserve must equal one")
    return baseline, reserve


def evidence_gate(row: Mapping[str, Any], policy: AllocationPolicy) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not bool(row.get("eligible_for_tiny_pilot", False)):
        reasons.append("existing_oos_gate_not_passed")
    if not bool(row.get("production_threshold_present", False)):
        reasons.append("no_frozen_production_threshold")
    if _int(row.get("trades")) < policy.min_trades:
        reasons.append("insufficient_oos_trades")
    if _int(row.get("active_folds")) < policy.min_active_folds:
        reasons.append("insufficient_active_folds")
    if _float(row.get("net_return")) <= 0.0:
        reasons.append("nonpositive_net_return")
    if _float(row.get("stress_1_5_return")) <= 0.0:
        reasons.append("nonpositive_1_5x_cost_return")
    if _float(row.get("stress_2_0_return")) <= 0.0:
        reasons.append("nonpositive_2_0x_cost_return")
    if _float(row.get("profit_factor")) <= 1.0:
        reasons.append("profit_factor_gate")
    if _float(row.get("bootstrap_pvalue"), 1.0) > policy.max_bootstrap_pvalue:
        reasons.append("bootstrap_gate")
    if _float(row.get("positive_fold_fraction")) < policy.min_positive_fold_fraction:
        reasons.append("fold_stability_gate")
    if _float(row.get("max_drawdown"), 1.0) > policy.max_drawdown:
        reasons.append("drawdown_gate")
    return not reasons, reasons


def conservative_score(row: Mapping[str, Any], policy: AllocationPolicy) -> float:
    passed, _ = evidence_gate(row, policy)
    if not passed:
        return 0.0
    robust = min(
        _float(row.get("net_return")),
        _float(row.get("stress_1_5_return")),
        _float(row.get("stress_2_0_return")),
    )
    drawdown_survival = max(0.0, 1.0 - _float(row.get("max_drawdown")) / policy.max_drawdown)
    confidence = max(0.0, 1.0 - _float(row.get("bootstrap_pvalue"), 1.0))
    stability = min(1.0, max(0.0, _float(row.get("positive_fold_fraction"))))
    return max(0.0, robust) * drawdown_survival * confidence * stability


def aggregate_training_rows(
    windows: Sequence[Mapping[str, Any]], strategy_names: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Aggregate past returns while using the latest cumulative gate state.

    OOS gate fields are cumulative diagnostics. Earlier windows may legitimately
    be ineligible only because the sample had not matured; requiring every early
    snapshot to pass would make future eligibility impossible.
    """
    output: dict[str, dict[str, Any]] = {}
    for name in strategy_names:
        rows: list[Mapping[str, Any]] = []
        for window in windows:
            strategies = window.get("strategies")
            if isinstance(strategies, Mapping) and isinstance(strategies.get(name), Mapping):
                rows.append(strategies[name])
        if not rows:
            output[name] = {}
            continue
        latest = rows[-1]
        output[name] = {
            "net_return": sum(_float(row.get("net_return")) for row in rows) / len(rows),
            "stress_1_5_return": sum(_float(row.get("stress_1_5_return")) for row in rows) / len(rows),
            "stress_2_0_return": sum(_float(row.get("stress_2_0_return")) for row in rows) / len(rows),
            "trades": _int(latest.get("trades")),
            "active_folds": _int(latest.get("active_folds")),
            "profit_factor": _float(latest.get("profit_factor")),
            "bootstrap_pvalue": _float(latest.get("bootstrap_pvalue"), 1.0),
            "positive_fold_fraction": _float(latest.get("positive_fold_fraction")),
            "max_drawdown": _float(latest.get("max_drawdown"), 1.0),
            "eligible_for_tiny_pilot": bool(latest.get("eligible_for_tiny_pilot", False)),
            "production_threshold_present": bool(latest.get("production_threshold_present", False)),
        }
    return output


def propose_allocation(
    baseline: Mapping[str, float], training_rows: Mapping[str, Mapping[str, Any]], policy: AllocationPolicy
) -> dict[str, Any]:
    names = sorted(baseline)
    if not names:
        raise ValueError("at least one strategy is required")
    floor_total = policy.exploration_floor * len(names)
    if floor_total + policy.minimum_reserve > 1.0 + 1e-12:
        raise ValueError("exploration floors plus reserve exceed capital")
    if policy.max_strategy_fraction < policy.exploration_floor:
        raise ValueError("max_strategy_fraction is below exploration floor")

    allocations = {name: policy.exploration_floor for name in names}
    scores = {name: conservative_score(training_rows.get(name, {}), policy) for name in names}
    remaining = 1.0 - policy.minimum_reserve - floor_total
    active = {name for name in names if scores[name] > 0.0}
    extra_cap = policy.max_strategy_fraction - policy.exploration_floor
    extra = {name: 0.0 for name in names}

    while remaining > 1e-12 and active:
        total_score = sum(scores[name] for name in active)
        if total_score <= 0.0:
            break
        round_budget = remaining
        consumed = 0.0
        saturated: set[str] = set()
        for name in sorted(active):
            desired = round_budget * scores[name] / total_score
            room = max(0.0, extra_cap - extra[name])
            grant = min(desired, room)
            extra[name] += grant
            consumed += grant
            if room - grant <= 1e-12:
                saturated.add(name)
        if consumed <= 1e-12:
            break
        remaining -= consumed
        active.difference_update(saturated)

    for name in names:
        allocations[name] += extra[name]
    reserve = policy.minimum_reserve + remaining
    details: dict[str, Any] = {}
    for name in names:
        passed, reasons = evidence_gate(training_rows.get(name, {}), policy)
        details[name] = {
            "baseline_fraction": _float(baseline[name]),
            "challenger_fraction": allocations[name],
            "gate_pass": passed,
            "score": scores[name],
            "reasons": reasons,
        }
    return {
        "strategy_fractions": allocations,
        "reserve_fraction": reserve,
        "details": details,
        "status": "EVIDENCE_WEIGHTED" if any(scores.values()) else "EXPLORATION_ONLY",
    }


def portfolio_return(weights: Mapping[str, float], window: Mapping[str, Any], field: str) -> float:
    strategies = window.get("strategies")
    if not isinstance(strategies, Mapping):
        return 0.0
    total = 0.0
    for name, weight in weights.items():
        row = strategies.get(name)
        if isinstance(row, Mapping):
            total += _float(weight) * _float(row.get(field))
    return total


def chronological_ablation(
    baseline: Mapping[str, float],
    windows: Sequence[Mapping[str, Any]],
    policy: AllocationPolicy,
    min_train_windows: int = 2,
) -> dict[str, Any]:
    folds: list[dict[str, Any]] = []
    names = sorted(baseline)
    for index in range(min_train_windows, len(windows)):
        training_rows = aggregate_training_rows(windows[:index], names)
        proposal = propose_allocation(baseline, training_rows, policy)
        challenger = proposal["strategy_fractions"]
        test = windows[index]
        fold = {"test_index": index, "timestamp": test.get("timestamp"), "allocation": proposal}
        for suffix, field in (("net", "net_return"), ("1_5x", "stress_1_5_return"), ("2_0x", "stress_2_0_return")):
            base_value = portfolio_return(baseline, test, field)
            challenge_value = portfolio_return(challenger, test, field)
            fold[f"baseline_{suffix}_return"] = base_value
            fold[f"challenger_{suffix}_return"] = challenge_value
            fold[f"incremental_{suffix}_return"] = challenge_value - base_value
        folds.append(fold)

    result = {
        "folds": folds,
        "test_folds": len(folds),
        "incremental_net_return": sum(_float(fold["incremental_net_return"]) for fold in folds),
        "incremental_1_5x_return": sum(_float(fold["incremental_1_5x_return"]) for fold in folds),
        "incremental_2_0x_return": sum(_float(fold["incremental_2_0x_return"]) for fold in folds),
    }
    result["evidence_ready"] = bool(
        len(folds) >= 2
        and result["incremental_net_return"] > 0.0
        and result["incremental_1_5x_return"] > 0.0
        and result["incremental_2_0x_return"] > 0.0
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Research V5 evidence-weighted capital allocator")
    parser.add_argument("--config", default="config/paper_v5.json")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    config = _read_json(Path(args.config))
    evidence = _read_json(Path(args.evidence))
    baseline, baseline_reserve = load_v5_baseline(config)
    windows = evidence.get("windows")
    if not isinstance(windows, list):
        raise ValueError("evidence.windows must be a list")
    policy = AllocationPolicy(minimum_reserve=max(0.10, baseline_reserve))
    proposal = propose_allocation(baseline, aggregate_training_rows(windows, sorted(baseline)), policy)
    report = {
        "schema": "polymarket_v5_evidence_weighted_allocator_v1",
        "research_only": True,
        "live_config_mutated": False,
        "baseline_strategy_fractions": baseline,
        "baseline_reserve_fraction": baseline_reserve,
        "proposal": proposal,
        "chronological_ablation": chronological_ablation(baseline, windows, policy),
        "promotion_rule": "at least two future folds with positive incremental normal, 1.5x and 2.0x cost-stressed returns versus fixed V5",
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
