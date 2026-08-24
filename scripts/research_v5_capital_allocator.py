#!/usr/bin/env python3
"""Research-only evidence-weighted capital allocation challenger for V5.

The live V5 allocator intentionally uses fixed capital fractions to collect clean
forward evidence from independent paper books. This module does not modify those
fractions. It evaluates a separate challenger that allocates only incremental
capital to strategy sleeves whose existing OOS gates already pass, while keeping
a small exploration floor for every enabled sleeve and sending unsupported
capital to reserve.

Evidence JSON schema used by the CLI:
{
  "windows": [
    {
      "timestamp": 1,
      "hours": 1.0,
      "strategies": {
        "micro": {
          "net_return": 0.001,
          "stress_1_5_return": 0.0008,
          "stress_2_0_return": 0.0006,
          "trades": 40,
          "active_folds": 3,
          "profit_factor": 1.2,
          "bootstrap_pvalue": 0.03,
          "positive_fold_fraction": 0.67,
          "max_drawdown": 0.02,
          "eligible_for_tiny_pilot": true,
          "production_threshold_present": true
        }
      }
    }
  ]
}

All returns must already be net of spread, fees, slippage and the execution model
used by the originating sleeve. The allocator only consumes those realized OOS
returns; it never reconstructs or relaxes source-model execution assumptions.
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
    strategies = multi.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        raise ValueError("multi_strategy.strategies must be non-empty")
    baseline: dict[str, float] = {}
    for raw in strategies:
        if not isinstance(raw, Mapping) or not bool(raw.get("enabled", True)):
            continue
        name = str(raw.get("name", ""))
        fraction = _float(raw.get("capital_fraction"), -1.0)
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
    robust_return = min(
        _float(row.get("net_return")),
        _float(row.get("stress_1_5_return")),
        _float(row.get("stress_2_0_return")),
    )
    drawdown = max(0.0, _float(row.get("max_drawdown")))
    drawdown_survival = max(0.0, 1.0 - drawdown / policy.max_drawdown)
    inference_confidence = max(0.0, 1.0 - _float(row.get("bootstrap_pvalue"), 1.0))
    fold_stability = min(1.0, max(0.0, _float(row.get("positive_fold_fraction"))))
    return max(0.0, robust_return) * drawdown_survival * inference_confidence * fold_stability


def aggregate_training_rows(
    windows: Sequence[Mapping[str, Any]], strategy_names: Sequence[str]
) -> dict[str, dict[str, Any]]:
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
        output[name] = {
            "net_return": sum(_float(row.get("net_return")) for row in rows) / len(rows),
            "stress_1_5_return": sum(_float(row.get("stress_1_5_return")) for row in rows) / len(rows),
            "stress_2_0_return": sum(_float(row.get("stress_2_0_return")) for row in rows) / len(rows),
            "trades": sum(_int(row.get("trades")) for row in rows),
            "active_folds": max((_int(row.get("active_folds")) for row in rows), default=0),
            "profit_factor": min((_float(row.get("profit_factor")) for row in rows), default=0.0),
            "bootstrap_pvalue": max((_float(row.get("bootstrap_pvalue"), 1.0) for row in rows), default=1.0),
            "positive_fold_fraction": min(
                (_float(row.get("positive_fold_fraction")) for row in rows), default=0.0
            ),
            "max_drawdown": max((_float(row.get("max_drawdown")) for row in rows), default=1.0),
            "eligible_for_tiny_pilot": all(bool(row.get("eligible_for_tiny_pilot", False)) for row in rows),
            "production_threshold_present": all(bool(row.get("production_threshold_present", False)) for row in rows),
        }
    return output


def _distribute_capped(
    names: Sequence[str], scores: Mapping[str, float], available: float, cap: float
) -> tuple[dict[str, float], float]:
    added = {name: 0.0 for name in names}
    remaining = max(0.0, available)
    active = {name for name in names if scores.get(name, 0.0) > 0.0}
    while remaining > 1e-12 and active:
        total_score = sum(scores[name] for name in active)
        if total_score <= 0.0:
            break
        consumed = 0.0
        saturated: set[str] = set()
        for name in sorted(active):
            share = remaining * scores[name] / total_score
            room = max(0.0, cap - added[name])
            grant = min(share, room)
            added[name] += grant
            consumed += grant
            if room - grant <= 1e-12:
                saturated.add(name)
        if consumed <= 1e-12:
            break
        remaining -= consumed
        active.difference_update(saturated)
    return added, remaining


def propose_allocation(
    baseline: Mapping[str, float], training_rows: Mapping[str, Mapping[str, Any]], policy: AllocationPolicy
) -> dict[str, Any]:
    names = sorted(baseline)
    if not names:
        raise ValueError("at least one strategy is required")
    exploration_total = policy.exploration_floor * len(names)
    if exploration_total + policy.minimum_reserve > 1.0 + 1e-12:
        raise ValueError("exploration floors plus minimum reserve exceed capital")
    if policy.max_strategy_fraction + 1e-12 < policy.exploration_floor:
        raise ValueError("max strategy fraction is below the exploration floor")

    allocations = {name: policy.exploration_floor for name in names}
    scores = {name: conservative_score(training_rows.get(name, {}), policy) for name in names}
    extra_capacity = max(0.0, 1.0 - policy.minimum_reserve - exploration_total)
    cap_for_extra = max(0.0, policy.max_strategy_fraction - policy.exploration_floor)
    added, unused = _distribute_capped(names, scores, extra_capacity, cap_for_extra)
    for name in names:
        allocations[name] += added[name]
    reserve = policy.minimum_reserve + unused

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
        "status": "EVIDENCE_WEIGHTED" if any(score > 0.0 for score in scores.values()) else "EXPLORATION_ONLY",
    }


def portfolio_return(weights: Mapping[str, float], window: Mapping[str, Any], field: str) -> float:
    strategies = window.get("strategies")
    if not isinstance(strategies, Mapping):
        return 0.0
    return sum(
        _float(weight) * _float(strategies.get(name, {}).get(field))
        for name, weight in weights.items()
        if isinstance(strategies.get(name), Mapping)
    )


def chronological_ablation(
    baseline: Mapping[str, float], windows: Sequence[Mapping[str, Any]], policy: AllocationPolicy, min_train_windows: int = 2
) -> dict[str, Any]:
    folds: list[dict[str, Any]] = []
    names = sorted(baseline)
    for index in range(min_train_windows, len(windows)):
        training = windows[:index]
        test = windows[index]
        training_rows = aggregate_training_rows(training, names)
        proposal = propose_allocation(baseline, training_rows, policy)
        challenger = proposal["strategy_fractions"]
        fold = {
            "test_index": index,
            "timestamp": test.get("timestamp"),
            "challenger_status": proposal["status"],
            "baseline_net_return": portfolio_return(baseline, test, "net_return"),
            "challenger_net_return": portfolio_return(challenger, test, "net_return"),
            "baseline_1_5x_return": portfolio_return(baseline, test, "stress_1_5_return"),
            "challenger_1_5x_return": portfolio_return(challenger, test, "stress_1_5_return"),
            "baseline_2_0x_return": portfolio_return(baseline, test, "stress_2_0_return"),
            "challenger_2_0x_return": portfolio_return(challenger, test, "stress_2_0_return"),
            "allocation": proposal,
        }
        fold["incremental_net_return"] = fold["challenger_net_return"] - fold["baseline_net_return"]
        fold["incremental_1_5x_return"] = fold["challenger_1_5x_return"] - fold["baseline_1_5x_return"]
        fold["incremental_2_0x_return"] = fold["challenger_2_0x_return"] - fold["baseline_2_0x_return"]
        folds.append(fold)

    total = lambda key: sum(_float(fold[key]) for fold in folds)
    result = {
        "folds": folds,
        "test_folds": len(folds),
        "incremental_net_return": total("incremental_net_return"),
        "incremental_1_5x_return": total("incremental_1_5x_return"),
        "incremental_2_0x_return": total("incremental_2_0x_return"),
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
    parser.add_argument("--exploration-floor", type=float, default=0.02)
    parser.add_argument("--minimum-reserve", type=float, default=0.10)
    parser.add_argument("--max-strategy-fraction", type=float, default=0.30)
    args = parser.parse_args()

    config = _read_json(Path(args.config))
    evidence = _read_json(Path(args.evidence))
    baseline, baseline_reserve = load_v5_baseline(config)
    windows = evidence.get("windows")
    if not isinstance(windows, list):
        raise ValueError("evidence.windows must be a list")
    policy = AllocationPolicy(
        exploration_floor=args.exploration_floor,
        minimum_reserve=max(args.minimum_reserve, baseline_reserve),
        max_strategy_fraction=args.max_strategy_fraction,
    )
    training_rows = aggregate_training_rows(windows, sorted(baseline))
    proposal = propose_allocation(baseline, training_rows, policy)
    report = {
        "schema": "polymarket_v5_evidence_weighted_allocator_v1",
        "research_only": True,
        "live_config_mutated": False,
        "baseline_strategy_fractions": baseline,
        "baseline_reserve_fraction": baseline_reserve,
        "proposal": proposal,
        "chronological_ablation": chronological_ablation(baseline, windows, policy),
        "promotion_rule": "require at least two strictly future folds with positive incremental net, 1.5x-cost and 2.0x-cost returns versus fixed V5 allocation",
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
