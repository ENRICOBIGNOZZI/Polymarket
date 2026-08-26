#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Mapping, Sequence


def standardize(values: Sequence[float]) -> list[float]:
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    if sd <= 1e-12:
        raise ValueError("series must have nonzero variance")
    return [(value - mean) / sd for value in values]


def mean_series(series: Mapping[str, Sequence[float]], names: Sequence[str]) -> list[float]:
    if not names:
        raise ValueError("factor needs at least one control series")
    n = len(series[names[0]])
    if any(len(series[name]) != n for name in names):
        raise ValueError("all series must have the same length")
    return [statistics.fmean(series[name][i] for name in names) for i in range(n)]


def loading_on(target: Sequence[float], factor: Sequence[float]) -> float:
    if len(target) != len(factor):
        raise ValueError("target and factor length mismatch")
    tm = statistics.fmean(target)
    fm = statistics.fmean(factor)
    fvar = sum((value - fm) ** 2 for value in factor)
    if fvar <= 1e-12:
        raise ValueError("factor must have nonzero variance")
    return sum((value - tm) * (f - fm) for value, f in zip(target, factor)) / fvar


def target_specific_loo_loading(series: Mapping[str, Sequence[float]], target: str) -> float:
    controls = [name for name in series if name != target]
    return loading_on(series[target], mean_series(series, controls))


def shared_pair_factor_loadings(
    series: Mapping[str, Sequence[float]], target_a: str, target_b: str
) -> tuple[float, float]:
    controls = [name for name in series if name not in {target_a, target_b}]
    if len(controls) < 2:
        raise ValueError("pair-level leave-two-out factor requires at least two controls")
    factor = mean_series(series, controls)
    return loading_on(series[target_a], factor), loading_on(series[target_b], factor)


def deterministic_fixture(points: int = 240) -> dict[str, list[float]]:
    if points < 24:
        raise ValueError("fixture requires at least 24 points")
    common = [math.sin(2.0 * math.pi * t / points) for t in range(points)]
    contaminant = [math.cos(4.0 * math.pi * t / points) for t in range(points)]
    control_noise = [math.sin(6.0 * math.pi * t / points) for t in range(points)]
    raw = {
        "A": [common[i] + 5.0 * contaminant[i] for i in range(points)],
        "B": [common[i] - 2.0 * contaminant[i] for i in range(points)],
        "C": [common[i] + 0.2 * control_noise[i] for i in range(points)],
        "D": [common[i] - 0.2 * control_noise[i] for i in range(points)],
    }
    return {name: standardize(values) for name, values in raw.items()}


def run_audit(points: int = 240) -> dict[str, float | bool | str]:
    series = deterministic_fixture(points)
    loo_a = target_specific_loo_loading(series, "A")
    loo_b = target_specific_loo_loading(series, "B")
    shared_a, shared_b = shared_pair_factor_loadings(series, "A", "B")
    loo_ratio = abs(loo_a / loo_b)
    shared_ratio = abs(shared_a / shared_b)
    ratio_distortion = max(loo_ratio, shared_ratio) / min(loo_ratio, shared_ratio)
    sign_disagreement = (loo_a * loo_b) * (shared_a * shared_b) < 0.0
    return {
        "schema": "lf_v6_pair_factor_basis_audit_v1",
        "points": points,
        "target_specific_loo_loading_a": loo_a,
        "target_specific_loo_loading_b": loo_b,
        "shared_leave_pair_out_loading_a": shared_a,
        "shared_leave_pair_out_loading_b": shared_b,
        "target_specific_abs_ratio": loo_ratio,
        "shared_abs_ratio": shared_ratio,
        "ratio_distortion": ratio_distortion,
        "pair_factor_sign_relation_changes": sign_disagreement,
        "finding": "target-specific leave-one-out loadings are not commensurable pair hedge coefficients",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V6 Local Factor pair-factor basis consistency")
    parser.add_argument("--points", type=int, default=240)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_audit(args.points)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
