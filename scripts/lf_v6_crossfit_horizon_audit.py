#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path


def hadamard(order: int) -> list[list[int]]:
    if order < 2 or order & (order - 1):
        raise ValueError("order must be a power of two")
    matrix = [[1]]
    while len(matrix) < order:
        matrix = [row + row for row in matrix] + [row + [-x for x in row] for row in matrix]
    return matrix


def standardize(values: list[float]) -> list[float]:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    if sd <= 0.0:
        raise ValueError("series must have positive sample variance")
    return [(x - mean) / sd for x in values]


def loading(target: list[float], factor: list[float]) -> float:
    target_mean = statistics.fmean(target)
    factor_mean = statistics.fmean(factor)
    factor_var = sum((x - factor_mean) ** 2 for x in factor)
    if factor_var <= 1e-12:
        return 0.0
    return sum((x - target_mean) * (f - factor_mean) for x, f in zip(target, factor)) / factor_var


def factor(panel: list[list[float]], exclude: int | None = None) -> list[float]:
    members = [series for index, series in enumerate(panel) if index != exclude]
    if not members:
        raise ValueError("factor needs at least one peer")
    return [statistics.fmean(series[t] for series in members) for t in range(len(members[0]))]


def orthogonal_panel(markets: int = 5, points: int = 32) -> list[list[float]]:
    if markets >= points:
        raise ValueError("need fewer markets than Hadamard rows")
    matrix = hadamard(points)
    return [standardize([float(x) for x in matrix[row]]) for row in range(1, markets + 1)]


def self_inclusion_diagnostic(markets: int = 5, points: int = 32) -> dict[str, object]:
    panel = orthogonal_panel(markets, points)
    inclusive = factor(panel)
    inclusive_loadings = [loading(series, inclusive) for series in panel]
    leave_one_out_loadings = [loading(series, factor(panel, exclude=index)) for index, series in enumerate(panel)]
    return {
        "design": "mutually orthogonal, mean-zero, equal-variance market histories with no common factor",
        "markets": markets,
        "points": points,
        "inclusive_loadings": inclusive_loadings,
        "leave_one_out_loadings": leave_one_out_loadings,
        "max_abs_leave_one_out_loading": max(abs(x) for x in leave_one_out_loadings),
        "min_abs_inclusive_loading": min(abs(x) for x in inclusive_loadings),
        "interpretation": (
            "Because each target is included in the cross-sectional mean used as its factor, the incumbent factor regression "
            "manufactures a loading of one even when all market histories are mutually orthogonal. A leave-one-out factor "
            "correctly returns zero loading in this controlled design."
        ),
    }


def half_life(phi: float) -> float:
    if not 0.0 < phi < 1.0:
        raise ValueError("phi must lie in (0,1)")
    return -math.log(2.0) / math.log(phi)


def incumbent_hold_bars(phi: float) -> float:
    return max(1.0, min(24.0, 2.0 * half_life(phi)))


def horizon_diagnostic(phi: float) -> dict[str, float]:
    bars = incumbent_hold_bars(phi)
    one_step_fraction = 1.0 - phi
    hold_horizon_fraction = 1.0 - phi**bars
    return {
        "phi": phi,
        "half_life_bars": half_life(phi),
        "incumbent_hold_bars": bars,
        "incumbent_one_step_reversion_fraction": one_step_fraction,
        "hold_horizon_reversion_fraction": hold_horizon_fraction,
        "horizon_to_one_step_ratio": hold_horizon_fraction / one_step_fraction,
    }


def source_contract(root: Path) -> dict[str, bool]:
    local_factor = (root / "scripts/v6_local_factor_intents.py").read_text(encoding="utf-8")
    loop = (root / "scripts/paper_v6_loop.sh").read_text(encoding="utf-8")
    config = json.loads((root / "config/paper_v6.json").read_text(encoding="utf-8"))
    return {
        "target_is_inside_its_factor": (
            'factor = [statistics.fmean(standardized[m.market_id][j] for m in usable)' in local_factor
        ),
        "loading_floor_is_only_point_zero_five": 'if abs(loading) < 0.05:' in local_factor,
        "forecast_is_one_step_ar_change": '(phi - 1.0) * (resid[-1] - rmu)' in local_factor,
        "hold_is_two_half_lives_capped_at_24": '2.0 * max(half_lives, default=2.0)' in local_factor,
        "live_factor_min_common_48": '--min-common-points 48' in loop,
        "live_factor_min_z_1": '--min-z 1.00' in loop,
        "live_factor_markets_400": '--markets 400' in loop,
        "hard_max_drawdown_unchanged": float(config["max_drawdown"]) == 0.15,
        "hard_market_concentration_unchanged": float(config["max_market_fraction"]) == 0.025,
        "hard_event_concentration_unchanged": float(config["max_event_fraction"]) == 0.08,
        "paper_completion_threshold_unchanged": '--completion-threshold 0.75' in loop,
        "paper_max_leg_risk_unchanged": '--max-leg-risk-usd 12' in loop,
    }


def build_report(root: Path) -> dict[str, object]:
    contract = source_contract(root)
    return {
        "schema": "lf_v6_crossfit_horizon_audit_v1",
        "source_contract": contract,
        "self_inclusion": self_inclusion_diagnostic(),
        "horizon_cells": [horizon_diagnostic(phi) for phi in (0.90, 0.95, 0.98)],
        "paper_only_successor_profile": {
            "preconditions": [
                "leave-one-out or otherwise cross-fitted factor per target market",
                "dependence-aware unit-root null calibration before multiplicity control",
                "hold-horizon-matched residual forecast used for executable edge",
                "same chronological sample and cost model as incumbent",
            ],
            "aggressive_discovery_after_preconditions": {
                "markets": 700,
                "min_liquidity": 5.0,
                "max_clusters": 30,
                "min_common_points": 36,
                "min_abs_residual_z": 0.75,
            },
            "unchanged_execution_economics": {
                "min_edge": 0.00020,
                "max_trade_usd": 60.0,
                "slippage_bps": 5.0,
            },
        },
        "decision": "MORE_EVIDENCE_REQUIRED",
        "required_common_sample_test": (
            "Compare incumbent vs cross-fitted horizon-matched local factors on identical chronological panels. Report factor "
            "relevance, candidate survival, robust multiplicity-adjusted significance, maker fill/exit feasibility, net edge "
            "and portfolio PnL under 1x/1.5x/2x costs before any integration."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.root)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
