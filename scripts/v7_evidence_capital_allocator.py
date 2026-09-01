#!/usr/bin/env python3
"""Robust, evidence-gated capital-allocation proposal for V7 PAPER.

Information collection, exploitation, and cash reserve are separate budgets.
Exploitation requires a positive day-block lower confidence bound after
stressed costs, finite capacity, and drawdown clearance.  The proposal is
advisory: it never mutates engine envelopes or promotes a component.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_evidence_capital_allocator_v2"
REQUIRED_BENCHMARKS = {
    "polymarket_mid_diagnostic", "oracle_only_structural_model",
    "external_composite_plus_oracle", "settlement_model",
    "settlement_plus_microstructure", "unified_make_take_nothing_policy",
}
ATTRIBUTION_DIMENSIONS = {
    "engine", "action", "component_provenance", "market", "horizon",
    "latency_regime", "fill_path", "cost_component",
}


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def validate_policy(policy: dict[str, Any]) -> None:
    exploitation = policy.get("exploitation") if isinstance(policy.get("exploitation"), dict) else {}
    settlement = policy.get("settlement_model_floors") if isinstance(policy.get("settlement_model_floors"), dict) else {}
    capital = policy.get("capital") if isinstance(policy.get("capital"), dict) else {}
    required_true = {
        "require_complete_fee_slippage_rebate_latency_unwind_capital_costs",
        "require_positive_day_block_lcb95", "require_positive_stressed_pnl",
        "require_observed_capital_hours", "require_observed_executable_capacity",
        "require_observed_drawdown", "require_stable_conditional_calibration",
        "require_regime_stratification", "require_source_health_stratification",
    }
    if (
        policy.get("schema") != "polymarket_v7_economic_readiness_policy_v1"
        or policy.get("version") != 1
        or policy.get("paper_only") is not True
        or policy.get("authenticated_execution") is not False
        or policy.get("real_order_submission") is not False
        or policy.get("real_capital_at_risk") is not False
        or policy.get("automatic_capital_transfer") is not False
        or policy.get("automatic_promotion") is not False
        or policy.get("technical_readiness_may_be_green_while_economic_readiness_is_red") is not True
        or any(exploitation.get(key) is not True for key in required_true)
        or finite(exploitation.get("minimum_full_cost_stress_multiplier")) < 2.0
        or int(finite(exploitation.get("minimum_mature_terminal_units"))) < 300
        or int(finite(exploitation.get("minimum_day_blocks"))) < 30
        or int(finite(settlement.get("minimum_settlement_labeled_days"))) < 30
        or int(finite(settlement.get("minimum_settlement_labeled_contracts"))) < 2_500
        or int(finite(settlement.get("minimum_forward_oos_policy_trades"))) < 300
        or int(finite(settlement.get("minimum_conditional_calibration_contracts_per_bin"))) < 30
        or settlement.get("uncertainty_must_be_below_claimed_edge") is not True
        or capital.get("manual_promotion_artifact_required") is not True
        or set(policy.get("attribution_dimensions") or []) != ATTRIBUTION_DIMENSIONS
        or set(policy.get("causal_benchmarks") or []) != REQUIRED_BENCHMARKS
    ):
        raise ValueError("economic_readiness_policy")


def _day_series(economics: dict[str, Any], strategy: str) -> list[tuple[str, float]]:
    """Read whole-day stressed PnL blocks without treating rows as independent."""
    sources = economics.get("engine_day_stressed_net_pnl")
    if not isinstance(sources, dict):
        return []
    raw = sources.get(strategy)
    values: list[tuple[str, float]] = []
    if isinstance(raw, dict):
        for day, value in sorted(raw.items()):
            amount = finite(value, math.nan)
            if math.isfinite(amount):
                values.append((str(day), amount))
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, dict):
                day = str(item.get("day") or item.get("date") or index)
                amount = finite(item.get("pnl"), math.nan)
            else:
                day, amount = str(index), finite(item, math.nan)
            if math.isfinite(amount):
                values.append((day, amount))
    return values


def day_block_lcb95(values: list[float], *, samples: int = 4_000,
                    seed: int = 7) -> dict[str, Any]:
    """Deterministic 2.5th-percentile bootstrap LCB over whole day blocks."""
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if len(clean) < 2:
        return {
            "method": "UNAVAILABLE", "day_blocks": len(clean),
            "mean": statistics.fmean(clean) if clean else None, "lcb95": None,
            "bootstrap_samples": 0,
        }
    generator = random.Random(seed)
    means = sorted(
        statistics.fmean(generator.choice(clean) for _ in clean)
        for _ in range(max(500, samples))
    )
    lower_index = max(0, math.ceil(0.025 * len(means)) - 1)
    return {
        "method": "DAY_BLOCK_PERCENTILE_BOOTSTRAP",
        "day_blocks": len(clean), "mean": statistics.fmean(clean),
        "lcb95": means[lower_index], "bootstrap_samples": len(means),
    }


def _shrunk_covariance(
    series: dict[str, list[tuple[str, float]]], shrinkage: float,
) -> dict[str, dict[str, float | None]]:
    """Pairwise day covariance shrunk toward its diagonal."""
    by_strategy = {name: dict(rows) for name, rows in series.items()}
    output: dict[str, dict[str, float | None]] = {}
    names = sorted(series)
    for left in names:
        output[left] = {}
        for right in names:
            common = sorted(set(by_strategy[left]) & set(by_strategy[right]))
            if len(common) < 2:
                output[left][right] = None
                continue
            left_values = [by_strategy[left][day] for day in common]
            right_values = [by_strategy[right][day] for day in common]
            left_mean, right_mean = (
                statistics.fmean(left_values), statistics.fmean(right_values)
            )
            covariance = sum(
                (left_value - left_mean) * (right_value - right_mean)
                for left_value, right_value in zip(left_values, right_values)
            ) / (len(common) - 1)
            output[left][right] = covariance if left == right else (
                (1.0 - shrinkage) * covariance
            )
    return output


def _dependence_penalty(
    strategy: str, covariance: dict[str, dict[str, float | None]],
) -> float:
    own = covariance.get(strategy, {}).get(strategy)
    if own is None or own <= 0.0:
        return 1.0
    correlations: list[float] = []
    for other, covariance_value in covariance.get(strategy, {}).items():
        if other == strategy or covariance_value is None or covariance_value <= 0.0:
            continue
        other_variance = covariance.get(other, {}).get(other)
        if other_variance is not None and other_variance > 0.0:
            correlations.append(min(1.0, covariance_value / math.sqrt(own * other_variance)))
    return 1.0 + (statistics.fmean(correlations) if correlations else 0.0)


def _capped_weighted_allocation(
    pool: float, scores: dict[str, float], caps: dict[str, float],
) -> dict[str, float]:
    allocated = {strategy: 0.0 for strategy in scores}
    remaining = max(0.0, pool)
    active = {strategy for strategy, score in scores.items()
              if score > 0.0 and caps.get(strategy, 0.0) > 0.0}
    while active and remaining > 1e-9:
        total_score = sum(scores[strategy] for strategy in active)
        if total_score <= 0.0:
            break
        consumed = 0.0
        saturated: set[str] = set()
        for strategy in sorted(active):
            desired = remaining * scores[strategy] / total_score
            room = max(0.0, caps[strategy] - allocated[strategy])
            grant = min(desired, room)
            allocated[strategy] += grant
            consumed += grant
            if room - grant <= 1e-9:
                saturated.add(strategy)
        remaining -= consumed
        active -= saturated
        if not saturated:
            break
    return allocated


def propose(allocation: dict[str, Any], economics: dict[str, Any], *,
            exploration_fraction: float = 0.10,
            minimum_terminal_units: int = 300,
            minimum_day_blocks: int = 30,
            reserve_fraction: float = 0.85,
            maximum_concentration: float = 0.25,
            maximum_step_fraction: float = 0.25,
            covariance_shrinkage: float = 0.50,
            soft_drawdown: float = 0.05,
            hard_drawdown: float = 0.10,
            policy: dict[str, Any] | None = None) -> dict[str, Any]:
    settlement_minimum_days = 30
    settlement_minimum_contracts = 2_500
    settlement_minimum_forward_trades = 300
    settlement_minimum_calibration_per_bin = 30
    if policy is not None:
        validate_policy(policy)
        exploitation_policy = policy["exploitation"]
        settlement_policy = policy["settlement_model_floors"]
        capital_policy = policy["capital"]
        minimum_terminal_units = int(exploitation_policy["minimum_mature_terminal_units"])
        minimum_day_blocks = int(exploitation_policy["minimum_day_blocks"])
        exploration_fraction = float(capital_policy["exploration_fraction_per_engine_envelope"])
        reserve_fraction = float(capital_policy["cash_reserve_fraction_floor"])
        maximum_concentration = float(capital_policy["maximum_engine_concentration"])
        maximum_step_fraction = float(capital_policy["maximum_step_fraction_of_current_envelope"])
        settlement_minimum_days = int(settlement_policy["minimum_settlement_labeled_days"])
        settlement_minimum_contracts = int(settlement_policy["minimum_settlement_labeled_contracts"])
        settlement_minimum_forward_trades = int(settlement_policy["minimum_forward_oos_policy_trades"])
        settlement_minimum_calibration_per_bin = int(
            settlement_policy["minimum_conditional_calibration_contracts_per_bin"])
    if (allocation.get("paper_only") is not True
            or allocation.get("authenticated_execution") is not False
            or allocation.get("real_order_submission") is not False):
        raise ValueError("safe_paper_allocation_required")
    budgets = allocation.get("engine_budgets") if isinstance(allocation.get("engine_budgets"), dict) else {}
    if not budgets:
        raise ValueError("engine_budgets_required")
    account = finite(allocation.get("account_starting_capital"))
    if account <= 0.0:
        raise ValueError("positive_account_capital_required")
    mature = economics.get("engine_mature_terminal_units") if isinstance(economics.get("engine_mature_terminal_units"), dict) else {}
    stress = economics.get("engine_stressed_net_pnl") if isinstance(economics.get("engine_stressed_net_pnl"), dict) else {}
    hours = economics.get("engine_capital_hours") if isinstance(economics.get("engine_capital_hours"), dict) else {}
    capacities = economics.get("engine_capacity_usd") if isinstance(economics.get("engine_capacity_usd"), dict) else {}
    drawdowns = economics.get("engine_drawdown_fraction") if isinstance(economics.get("engine_drawdown_fraction"), dict) else {}
    drawdown_usd = economics.get("engine_drawdown_usd") if isinstance(economics.get("engine_drawdown_usd"), dict) else {}
    thresholds = economics.get("engine_minimum_terminal_units") if isinstance(economics.get("engine_minimum_terminal_units"), dict) else {}
    evidence_dimensions = economics.get("engine_evidence_dimensions") if isinstance(economics.get("engine_evidence_dimensions"), dict) else {}
    settlement_evidence = economics.get("engine_settlement_model_evidence") if isinstance(economics.get("engine_settlement_model_evidence"), dict) else {}
    benchmark_comparison = economics.get("benchmark_policy_comparison") if isinstance(economics.get("benchmark_policy_comparison"), dict) else {}
    benchmark_rows = benchmark_comparison.get("benchmarks") if isinstance(benchmark_comparison.get("benchmarks"), dict) else {}
    benchmark_complete = (
        set(benchmark_rows) == REQUIRED_BENCHMARKS
        and all(
            isinstance(row, dict) and row.get("causal") is True
            and int(finite(row.get("observation_count"))) > 0
            for row in benchmark_rows.values()
        )
        and benchmark_comparison.get("policy_observation_cut_frozen") is True
        and benchmark_comparison.get("trade_reselection_under_stress") is False
    )
    exploration_fraction = min(0.20, max(0.0, finite(exploration_fraction, 0.10)))
    reserve_fraction = min(0.99, max(0.80, finite(reserve_fraction, 0.85)))
    maximum_concentration = min(0.50, max(0.05, finite(maximum_concentration, 0.25)))
    maximum_step_fraction = min(1.0, max(0.05, finite(maximum_step_fraction, 0.25)))
    covariance_shrinkage = min(1.0, max(0.0, finite(covariance_shrinkage, 0.50)))
    if not 0.0 <= soft_drawdown < hard_drawdown <= 1.0:
        raise ValueError("drawdown_thresholds_invalid")
    day_series = {strategy: _day_series(economics, strategy) for strategy in budgets}
    covariance = _shrunk_covariance(day_series, covariance_shrinkage)
    rows: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    information_total = 0.0
    for strategy, raw_budget in sorted(budgets.items()):
        envelope = max(0.0, finite(raw_budget))
        information = envelope * exploration_fraction
        information_total += information
        units = max(0, int(finite(mature.get(strategy))))
        required_units = max(1, int(finite(
            thresholds.get(strategy), minimum_terminal_units)))
        strategy_stress = stress.get(strategy) if isinstance(stress.get(strategy), dict) else {}
        pnl_2x = finite(strategy_stress.get("2x"))
        capital_hours = max(0.0, finite(hours.get(strategy)))
        capacity = max(0.0, finite(capacities.get(strategy)))
        drawdown_observed = strategy in drawdowns or strategy in drawdown_usd
        drawdown = max(0.0, finite(
            drawdowns.get(strategy),
            finite(drawdown_usd.get(strategy), 0.0) / max(envelope, 1e-9),
        ))
        confidence = day_block_lcb95(
            [value for _, value in day_series[strategy]],
            seed=int(hashlib.sha256(strategy.encode()).hexdigest()[:8], 16),
        )
        blockers: list[str] = []
        dimensions = evidence_dimensions.get(strategy) if isinstance(
            evidence_dimensions.get(strategy), dict) else {}
        complete_cost_units = max(0, int(finite(
            dimensions.get("complete_cost_terminal_units"))))
        calibration_stable = dimensions.get("conditional_calibration_stable") is True
        calibration_count = max(0, int(finite(
            dimensions.get("conditional_calibration_count"))))
        regime_complete = dimensions.get("regime_stratification_complete") is True
        source_health_complete = dimensions.get("source_health_stratification_complete") is True
        attribution_complete = set(
            dimensions.get("attribution_dimensions") or []) == ATTRIBUTION_DIMENSIONS
        applicable_actions = dimensions.get("applicable_action_classes")
        action_rows = dimensions.get("action_classes")
        action_evidence_complete = (
            isinstance(applicable_actions, list) and bool(applicable_actions)
            and isinstance(action_rows, dict) and set(action_rows) == set(applicable_actions)
            and all(
                isinstance(row, dict)
                and int(finite(row.get("mature_terminal_units"))) > 0
                and row.get("complete_cost_vector") is True
                and finite(row.get("capital_hours")) > 0.0
                and finite(row.get("capacity_usd")) > 0.0
                and row.get("drawdown_observed") is True
                and row.get("positive_day_block_lcb") is True
                and row.get("positive_2x_full_cost_pnl") is True
                and row.get("conditional_calibration_stable") is True
                and row.get("regime_stratified") is True
                and row.get("source_health_stratified") is True
                for row in action_rows.values()
            )
        )
        if units < required_units:
            blockers.append("INSUFFICIENT_TERMINAL_UNITS")
        if complete_cost_units < units or units == 0:
            blockers.append("COMPLETE_COST_TERMINAL_UNITS_MISSING")
        if confidence["day_blocks"] < minimum_day_blocks:
            blockers.append("INSUFFICIENT_DAY_BLOCKS")
        if confidence["lcb95"] is None or confidence["lcb95"] <= 0.0:
            blockers.append("DAY_BLOCK_LCB95_NOT_POSITIVE")
        if pnl_2x <= 0.0:
            blockers.append("FULL_COST_2X_PNL_NOT_POSITIVE")
        if capital_hours <= 0.0:
            blockers.append("CAPITAL_HOURS_MISSING")
        if capacity <= 0.0:
            blockers.append("CAPACITY_MISSING_OR_ZERO")
        if not drawdown_observed:
            blockers.append("DRAWDOWN_MISSING")
        if drawdown >= hard_drawdown:
            blockers.append("HARD_DRAWDOWN_BREACH")
        if not calibration_stable or calibration_count <= 0:
            blockers.append("CONDITIONAL_CALIBRATION_NOT_STABLE")
        if not regime_complete:
            blockers.append("REGIME_STRATIFICATION_INCOMPLETE")
        if not source_health_complete:
            blockers.append("SOURCE_HEALTH_STRATIFICATION_INCOMPLETE")
        if not action_evidence_complete:
            blockers.append("ACTION_CLASS_EVIDENCE_INCOMPLETE")
        if not attribution_complete:
            blockers.append("ATTRIBUTION_DIMENSIONS_INCOMPLETE")
        if not benchmark_complete:
            blockers.append("CAUSAL_BENCHMARK_COMPARISON_INCOMPLETE")
        settlement = settlement_evidence.get(strategy) if isinstance(
            settlement_evidence.get(strategy), dict) else {}
        if strategy == "CRYPTO_SETTLEMENT_ENGINE":
            if int(finite(settlement.get("settlement_labeled_days"))) < settlement_minimum_days:
                blockers.append("SETTLEMENT_LABELED_DAYS_BELOW_30")
            if int(finite(settlement.get("settlement_labeled_contracts"))) < settlement_minimum_contracts:
                blockers.append("SETTLEMENT_LABELED_CONTRACTS_BELOW_2500")
            if int(finite(settlement.get("forward_oos_policy_trades"))) < settlement_minimum_forward_trades:
                blockers.append("FORWARD_OOS_POLICY_TRADES_BELOW_300")
            if int(finite(settlement.get("minimum_conditional_calibration_bin_count"))) < settlement_minimum_calibration_per_bin:
                blockers.append("SETTLEMENT_CONDITIONAL_CALIBRATION_INSUFFICIENT")
            uncertainty = finite(settlement.get("uncertainty_upper"), math.inf)
            edge = finite(settlement.get("claimed_edge_lower"), -math.inf)
            if not math.isfinite(uncertainty) or not math.isfinite(edge) or uncertainty >= edge:
                blockers.append("SETTLEMENT_UNCERTAINTY_NOT_BELOW_EDGE")
        eligible = not blockers
        risk = math.sqrt(max(0.0, finite(
            covariance.get(strategy, {}).get(strategy), 0.0)))
        dependence_penalty = _dependence_penalty(strategy, covariance)
        lcb = finite(confidence.get("lcb95"))
        score = (
            lcb / max(risk, abs(lcb), 1e-9) / dependence_penalty
            if eligible else 0.0
        )
        if drawdown >= soft_drawdown:
            score *= max(0.0, (hard_drawdown - drawdown) /
                         (hard_drawdown - soft_drawdown))
        scores[strategy] = score
        rows[strategy] = {
            "current_paper_envelope": envelope,
            "information_budget": information,
            "mature_terminal_units": units,
            "complete_cost_terminal_units": complete_cost_units,
            "minimum_terminal_units": required_units,
            "stressed_net_pnl_2x": pnl_2x if units else None,
            "capital_hours": capital_hours if units else None,
            "day_block_confidence": confidence,
            "capacity_usd": capacity if capacity > 0.0 else None,
            "drawdown_fraction": drawdown,
            "conditional_calibration_stable": calibration_stable,
            "conditional_calibration_count": calibration_count,
            "regime_stratification_complete": regime_complete,
            "source_health_stratification_complete": source_health_complete,
            "action_class_evidence_complete": action_evidence_complete,
            "attribution_dimensions_complete": attribution_complete,
            "settlement_model_evidence": settlement if strategy == "CRYPTO_SETTLEMENT_ENGINE" else None,
            "dependence_penalty": dependence_penalty,
            "exploitation_eligible": eligible,
            "evidence_score": score if eligible else None,
            "blocking_reasons": blockers,
            "proposed_exploitation": 0.0,
            "proposed_total": information,
        }
    reserve_floor = account * reserve_fraction
    if information_total > account - reserve_floor and information_total > 0.0:
        scale = (account - reserve_floor) / information_total
        information_total = 0.0
        for row in rows.values():
            row["information_budget"] *= scale
            row["proposed_total"] = row["information_budget"]
            information_total += row["information_budget"]
    exploitation_pool = max(0.0, account - reserve_floor - information_total)
    caps = {
        strategy: min(
            max(0.0, finite(rows[strategy]["capacity_usd"])),
            exploitation_pool * maximum_concentration,
            rows[strategy]["current_paper_envelope"] * maximum_step_fraction,
        )
        for strategy in rows
    }
    exploitation = _capped_weighted_allocation(exploitation_pool, scores, caps)
    for strategy, share in exploitation.items():
        rows[strategy]["proposed_exploitation"] = share
        rows[strategy]["proposed_total"] += share
    allocated = sum(float(row["proposed_total"]) for row in rows.values())
    eligible_count = sum(row["exploitation_eligible"] for row in rows.values())
    result = {
        "schema": SCHEMA, "timestamp": int(time.time()),
        "model_sha": economics.get("expected_model_sha"),
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "advisory_only": True, "automatic_transfer": False,
        "automatic_promotion": False,
        "active_paper_envelopes_unchanged": True,
        "information_fraction_per_engine_envelope": exploration_fraction,
        "minimum_terminal_units_for_exploitation": minimum_terminal_units,
        "minimum_day_blocks_for_exploitation": minimum_day_blocks,
        "reserve_fraction_floor": reserve_fraction,
        "reserve_floor": reserve_floor,
        "maximum_engine_concentration": maximum_concentration,
        "maximum_step_fraction_of_current_envelope": maximum_step_fraction,
        "covariance_shrinkage": covariance_shrinkage,
        "shrunk_day_covariance": covariance,
        "soft_drawdown_fraction": soft_drawdown,
        "hard_drawdown_fraction": hard_drawdown,
        "engines": rows, "information_budget_total": information_total,
        "exploitation_pool": exploitation_pool,
        "proposed_exploitation_total": sum(exploitation.values()),
        "proposed_allocated_total": allocated,
        "unallocated_exploitation_reserve": max(0.0, account - allocated),
        "manual_promotion_artifact_required": True,
        "technical_readiness": "GREEN",
        "economic_readiness": "GREEN" if eligible_count else "RED",
        "technical_green_may_coexist_with_economic_red": True,
        "benchmark_policy_comparison_complete": benchmark_complete,
        "required_benchmarks": sorted(REQUIRED_BENCHMARKS),
        "required_attribution_dimensions": sorted(ATTRIBUTION_DIMENSIONS),
        "economic_readiness_policy_applied": policy is not None,
        "state": "MANUAL_EXPLOITATION_PROPOSAL" if eligible_count else "INFORMATION_ONLY_CASH_DEFAULT",
        "reason_codes": [] if eligible_count else ["NO_STRATEGY_PASSES_ROBUST_EVIDENCE_GATES"],
    }
    result["proposal_sha256"] = hashlib.sha256(json.dumps(
        result, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--economics", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exploration-fraction", type=float, default=0.10)
    parser.add_argument("--minimum-terminal-units", type=int, default=300)
    parser.add_argument("--minimum-day-blocks", type=int, default=30)
    parser.add_argument("--reserve-fraction", type=float, default=0.85)
    parser.add_argument("--policy", type=Path, default=Path("config/v7_economic_readiness.json"))
    args = parser.parse_args()
    report = propose(
        load(args.allocation), load(args.economics),
        exploration_fraction=args.exploration_fraction,
        minimum_terminal_units=max(1, args.minimum_terminal_units),
        minimum_day_blocks=max(2, args.minimum_day_blocks),
        reserve_fraction=args.reserve_fraction,
        policy=load(args.policy),
    )
    atomic_json(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
