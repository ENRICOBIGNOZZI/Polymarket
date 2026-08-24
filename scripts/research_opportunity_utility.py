#!/usr/bin/env python3
"""Research-only robust utility scorer for cross-source Polymarket candidates.

This module is deliberately downstream of HF/LF signal generation. It does not
estimate alpha, emit production intents, mutate the live champion, or submit
orders. It ranks candidates by conservative expected net PnL per capital-hour
and attributes why raw signal edge fails to become executable edge.

Maker-dependent multi-leg candidates can supply empirical paired-fill evidence
plus a taker fallback edge. The scorer rejects a candidate when even an
optimistic two-sided 95% Wilson upper confidence bound on paired fills is below
the break-even paired-fill probability implied by maker success versus taker
fallback. Missing execution evidence fails closed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any, Iterable

Z95 = 1.959963984540054
OUTPUT_FIELDS = [
    "rank",
    "timestamp",
    "source",
    "candidate_id",
    "event_id",
    "raw_edge",
    "net_edge",
    "execution_cost_wedge",
    "execution_cost_fraction_of_raw",
    "cost_reduction_fraction_to_break_even",
    "stress_1_5_edge",
    "stress_2_0_edge",
    "fill_lower_bound",
    "pair_fill_upper_bound",
    "required_pair_fill_probability",
    "optimistic_pair_execution_edge",
    "paired_execution_feasible",
    "adverse_selection_edge",
    "latency_edge",
    "uncertainty_edge",
    "concentration_edge",
    "capital_required",
    "holding_hours",
    "conservative_edge",
    "conservative_expected_pnl",
    "utility_per_capital_hour",
    "edge_bottleneck",
    "research_eligible",
    "failure_reasons",
    "realized_net_pnl",
]
REQUIRED_EXECUTION_FIELDS = (
    "adverse_selection_edge",
    "latency_edge",
    "uncertainty_edge",
    "concentration_edge",
    "holding_hours",
)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def wilson_bounds(successes: int, trials: int, z: float = Z95) -> tuple[float, float]:
    if trials <= 0 or successes < 0 or successes > trials:
        return 0.0, 0.0
    p = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (p + z2 / (2.0 * trials)) / denominator
    radius = (
        z
        * math.sqrt((p * (1.0 - p) / trials) + z2 / (4.0 * trials * trials))
        / denominator
    )
    return max(0.0, center - radius), min(1.0, center + radius)


def wilson_lower_bound(successes: int, trials: int, z: float = Z95) -> float:
    return wilson_bounds(successes, trials, z)[0]


def wilson_upper_bound(successes: int, trials: int, z: float = Z95) -> float:
    return wilson_bounds(successes, trials, z)[1]


def stressed_edge(raw_edge: float, net_edge: float, multiplier: float) -> float:
    """Stress the observed execution-cost wedge, never manufacture a rebate."""
    base_cost = max(0.0, raw_edge - net_edge)
    return raw_edge - multiplier * base_cost


def execution_cost_diagnostics(raw_edge: float, net_edge: float) -> dict[str, float]:
    """Quantify how much of a positive raw signal is consumed by execution."""
    raw = finite(raw_edge, math.nan)
    net = finite(net_edge, math.nan)
    if not math.isfinite(raw) or not math.isfinite(net):
        return {
            "execution_cost_wedge": math.nan,
            "execution_cost_fraction_of_raw": math.nan,
            "cost_reduction_fraction_to_break_even": math.nan,
        }

    wedge = max(0.0, raw - net)
    cost_fraction = wedge / raw if raw > 0.0 else math.nan
    reduction = 0.0
    if raw > 0.0 and net < 0.0 and wedge > 0.0:
        # net = raw - cost. If cost falls to cost*(1-r), break-even requires
        # r = -net/cost. This is diagnostic only, not a production target.
        reduction = max(0.0, min(1.0, -net / wedge))
    return {
        "execution_cost_wedge": wedge,
        "execution_cost_fraction_of_raw": cost_fraction,
        "cost_reduction_fraction_to_break_even": reduction,
    }


def paired_execution_feasibility(
    maker_edge: float,
    taker_fallback_edge: float,
    pair_fills: int,
    pair_probes: int,
) -> dict[str, Any]:
    """Test whether paired maker execution can plausibly clear break-even.

    Expected edge under the conservative forced-completion model is

        q * maker_edge + (1-q) * taker_fallback_edge.

    When maker entry is positive and taker fallback is negative, break-even
    requires q > -taker/(maker-taker). The two-sided 95% Wilson upper bound is
    deliberately optimistic: if even it is below the hurdle, the observation
    is rejected for maker-dependent capital allocation.
    """
    maker = finite(maker_edge, math.nan)
    fallback = finite(taker_fallback_edge, math.nan)
    fills = max(0, integer(pair_fills))
    probes = max(0, integer(pair_probes))

    result = {
        "pair_fill_upper_bound": 0.0,
        "required_pair_fill_probability": 1.0,
        "optimistic_pair_execution_edge": math.nan,
        "paired_execution_feasible": False,
    }
    if (
        not math.isfinite(maker)
        or not math.isfinite(fallback)
        or probes <= 0
        or fills > probes
    ):
        return result

    upper = wilson_upper_bound(fills, probes)
    if maker <= 0.0:
        required = 1.0
    elif fallback >= 0.0:
        required = 0.0
    elif maker <= fallback:
        required = 1.0
    else:
        required = max(0.0, min(1.0, -fallback / (maker - fallback)))

    optimistic = upper * maker + (1.0 - upper) * fallback
    result.update(
        {
            "pair_fill_upper_bound": upper,
            "required_pair_fill_probability": required,
            "optimistic_pair_execution_edge": optimistic,
            "paired_execution_feasible": (
                maker > 0.0 and upper >= required and optimistic > 0.0
            ),
        }
    )
    return result


def classify_edge_bottleneck(
    *,
    raw_edge: float,
    net_edge: float,
    stress_2_0_edge: float,
    conservative_edge: float,
    fill_lower_bound: float,
    pair_required: bool,
    paired_execution_feasible: bool,
    missing_evidence: bool,
) -> str:
    """Attribute the first binding failure without changing any live threshold."""
    if missing_evidence:
        return "missing_execution_evidence"
    if raw_edge <= 0.0:
        return "signal_not_positive"
    if net_edge <= 0.0:
        return "execution_cost_bound"
    if stress_2_0_edge <= 0.0:
        return "cost_stress_fragile"
    if pair_required and not paired_execution_feasible:
        return "paired_fill_bound"
    if fill_lower_bound <= 0.0:
        return "fillability_bound"
    if conservative_edge <= 0.0:
        return "risk_penalty_bound"
    return "robust_candidate"


def score_row(row: dict[str, Any]) -> dict[str, Any]:
    raw_edge = finite(row.get("raw_edge"), math.nan)
    net_edge = finite(row.get("net_edge"), math.nan)
    capital = max(0.0, finite(row.get("capital_required")))
    holding_hours = max(1.0 / 60.0, finite(row.get("holding_hours"), 1.0))
    fills = max(0, integer(row.get("fills")))
    probes = max(0, integer(row.get("probes")))
    fill_lb = wilson_lower_bound(fills, probes)
    adverse = max(0.0, finite(row.get("adverse_selection_edge")))
    latency = max(0.0, finite(row.get("latency_edge")))
    uncertainty = max(0.0, finite(row.get("uncertainty_edge")))
    concentration = max(0.0, finite(row.get("concentration_edge")))

    reasons: list[str] = []
    if not math.isfinite(raw_edge) or not math.isfinite(net_edge):
        reasons.append("missing_edge")
        raw_edge = 0.0 if not math.isfinite(raw_edge) else raw_edge
        net_edge = 0.0 if not math.isfinite(net_edge) else net_edge
    for field in REQUIRED_EXECUTION_FIELDS:
        if row.get(field) in (None, ""):
            reasons.append(f"missing_{field}")
    if capital <= 0.0:
        reasons.append("no_executable_capital")
    if probes <= 0:
        reasons.append("missing_forward_fill_evidence")
    elif fills <= 0:
        reasons.append("no_observed_fills")

    cost_diag = execution_cost_diagnostics(raw_edge, net_edge)
    stress15 = stressed_edge(raw_edge, net_edge, 1.5)
    stress20 = stressed_edge(raw_edge, net_edge, 2.0)
    execution_survival = min(net_edge, stress15, stress20)
    conservative_edge = (
        execution_survival - adverse - latency - uncertainty - concentration
    )
    conservative_pnl = capital * fill_lb * conservative_edge
    capital_hours = capital * holding_hours
    utility = conservative_pnl / capital_hours if capital_hours > 0.0 else 0.0

    pair_required = row.get("taker_fallback_edge") not in (None, "")
    pair_result = {
        "pair_fill_upper_bound": math.nan,
        "required_pair_fill_probability": math.nan,
        "optimistic_pair_execution_edge": math.nan,
        "paired_execution_feasible": True,
    }
    if pair_required:
        pair_fills = max(0, integer(row.get("pair_fills")))
        pair_probes = max(0, integer(row.get("pair_probes")))
        pair_result = paired_execution_feasibility(
            net_edge,
            finite(row.get("taker_fallback_edge"), math.nan),
            pair_fills,
            pair_probes,
        )
        if pair_probes <= 0:
            reasons.append("missing_paired_fill_evidence")
        elif not pair_result["paired_execution_feasible"]:
            reasons.append("paired_fill_hurdle_not_met")

    if net_edge <= 0.0:
        reasons.append("nonpositive_net_edge")
    if stress15 <= 0.0:
        reasons.append("nonpositive_1.5x_stress")
    if stress20 <= 0.0:
        reasons.append("nonpositive_2.0x_stress")
    if conservative_edge <= 0.0:
        reasons.append("nonpositive_conservative_edge")
    if conservative_pnl <= 0.0:
        reasons.append("nonpositive_conservative_expected_pnl")

    missing_evidence = any(
        reason.startswith("missing_") or reason == "no_executable_capital"
        for reason in reasons
    )
    bottleneck = classify_edge_bottleneck(
        raw_edge=raw_edge,
        net_edge=net_edge,
        stress_2_0_edge=stress20,
        conservative_edge=conservative_edge,
        fill_lower_bound=fill_lb,
        pair_required=pair_required,
        paired_execution_feasible=bool(pair_result["paired_execution_feasible"]),
        missing_evidence=missing_evidence,
    )

    return {
        "timestamp": integer(row.get("timestamp")),
        "source": str(row.get("source") or "unknown"),
        "candidate_id": str(row.get("candidate_id") or row.get("source_id") or ""),
        "event_id": str(row.get("event_id") or ""),
        "raw_edge": raw_edge,
        "net_edge": net_edge,
        **cost_diag,
        "stress_1_5_edge": stress15,
        "stress_2_0_edge": stress20,
        "fill_lower_bound": fill_lb,
        **pair_result,
        "adverse_selection_edge": adverse,
        "latency_edge": latency,
        "uncertainty_edge": uncertainty,
        "concentration_edge": concentration,
        "capital_required": capital,
        "holding_hours": holding_hours,
        "conservative_edge": conservative_edge,
        "conservative_expected_pnl": conservative_pnl,
        "utility_per_capital_hour": utility,
        "edge_bottleneck": bottleneck,
        "research_eligible": int(not reasons),
        "failure_reasons": "|".join(reasons),
        "realized_net_pnl": finite(row.get("realized_net_pnl"), math.nan),
    }


def rank_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [score_row(row) for row in rows]
    scored.sort(
        key=lambda row: (
            -int(row["research_eligible"]),
            -finite(row["utility_per_capital_hour"]),
            -finite(row["conservative_expected_pnl"]),
            str(row["candidate_id"]),
        )
    )
    for rank, row in enumerate(scored, 1):
        row["rank"] = rank
    return scored


def chronological_oos(
    scored: list[dict[str, Any]],
    *,
    min_train_rows: int = 20,
    test_rows: int = 10,
    purge_seconds: int = 3600,
) -> dict[str, Any]:
    """Evaluate the fixed scorer against a net-edge baseline without look-ahead."""
    ordered = sorted(
        (row for row in scored if integer(row.get("timestamp")) > 0),
        key=lambda row: integer(row["timestamp"]),
    )
    folds: list[dict[str, Any]] = []
    cursor = max(1, min_train_rows)
    width = max(1, test_rows)
    purge = max(0, purge_seconds)
    while cursor < len(ordered):
        train_end_index = cursor - 1
        train_end_ts = integer(ordered[train_end_index]["timestamp"])
        cutoff = train_end_ts + purge
        test_start_index = cursor
        while (
            test_start_index < len(ordered)
            and integer(ordered[test_start_index]["timestamp"]) <= cutoff
        ):
            test_start_index += 1
        if test_start_index >= len(ordered):
            break
        test_end_index = min(len(ordered), test_start_index + width)
        test = ordered[test_start_index:test_end_index]
        realized = [
            row
            for row in test
            if math.isfinite(finite(row.get("realized_net_pnl"), math.nan))
        ]
        if realized:
            robust_pool = [
                row for row in realized if int(row["research_eligible"]) == 1
            ]
            robust = max(
                robust_pool,
                key=lambda row: finite(row["utility_per_capital_hour"]),
                default=None,
            )
            baseline = max(
                realized, key=lambda row: finite(row["net_edge"]), default=None
            )
            folds.append(
                {
                    "train_end_ts": train_end_ts,
                    "test_start_ts": integer(test[0]["timestamp"]),
                    "test_end_ts": integer(test[-1]["timestamp"]),
                    "realized_rows": len(realized),
                    "robust_candidate": robust and str(robust["candidate_id"]),
                    "robust_realized_net_pnl": (
                        robust and finite(robust["realized_net_pnl"])
                    ),
                    "baseline_candidate": baseline and str(baseline["candidate_id"]),
                    "baseline_realized_net_pnl": (
                        baseline and finite(baseline["realized_net_pnl"])
                    ),
                }
            )
        cursor = test_end_index

    robust_pnl = sum(
        finite(fold.get("robust_realized_net_pnl"))
        for fold in folds
        if fold.get("robust_candidate")
    )
    baseline_pnl = sum(
        finite(fold.get("baseline_realized_net_pnl"))
        for fold in folds
        if fold.get("baseline_candidate")
    )
    return {
        "schema": "polymarket_robust_opportunity_oos_v3",
        "folds": folds,
        "fold_count": len(folds),
        "robust_realized_net_pnl": robust_pnl,
        "baseline_net_edge_realized_net_pnl": baseline_pnl,
        "incremental_realized_net_pnl": robust_pnl - baseline_pnl,
        "evidence_ready": (
            len(folds) >= 2 and robust_pnl > baseline_pnl and robust_pnl > 0.0
        ),
        "selection_rule": (
            "max positive utility_per_capital_hour after 2x costs, fill "
            "uncertainty, pair-fill hurdle, adverse selection, latency, "
            "uncertainty and concentration"
        ),
        "baseline_rule": "max reported net_edge",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Research-only robust opportunity utility challenger"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ranking-output", type=Path, required=True)
    parser.add_argument("--oos-output", type=Path, required=True)
    parser.add_argument("--min-train-rows", type=int, default=20)
    parser.add_argument("--test-rows", type=int, default=10)
    parser.add_argument("--purge-seconds", type=int, default=3600)
    args = parser.parse_args()

    ranked = rank_rows(read_rows(args.input))
    write_csv(args.ranking_output, ranked)
    report = chronological_oos(
        ranked,
        min_train_rows=args.min_train_rows,
        test_rows=args.test_rows,
        purge_seconds=args.purge_seconds,
    )
    args.oos_output.parent.mkdir(parents=True, exist_ok=True)
    args.oos_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
