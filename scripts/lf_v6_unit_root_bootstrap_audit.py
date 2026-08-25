#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path


def score_statistic(levels: list[float]) -> float:
    mean = statistics.fmean(levels)
    return statistics.fmean(
        (levels[i - 1] - mean) * (levels[i] - levels[i - 1])
        for i in range(1, len(levels))
    )


def iid_unit_root_score_expectation(innovation_variance: float) -> float:
    if innovation_variance < 0.0 or not math.isfinite(innovation_variance):
        raise ValueError("innovation variance must be finite and nonnegative")
    return -0.5 * innovation_variance


def circular_block_sample(values: list[float], size: int, block: int, rng: random.Random) -> list[float]:
    out: list[float] = []
    while len(out) < size:
        start = rng.randrange(len(values))
        out.extend(values[(start + j) % len(values)] for j in range(block))
    return out[:size]


def score_centered_block_pvalue(levels: list[float], seed: int, reps: int) -> float:
    observed = score_statistic(levels)
    mean = statistics.fmean(levels)
    scores = [
        (levels[i - 1] - mean) * (levels[i] - levels[i - 1])
        for i in range(1, len(levels))
    ]
    centered = [value - observed for value in scores]
    n = len(centered)
    block = max(2, min(n, int(round(math.sqrt(n)))))
    rng = random.Random(seed)
    left = 0
    for _ in range(reps):
        sample = circular_block_sample(centered, n, block, rng)
        if statistics.fmean(sample) <= observed:
            left += 1
    return (left + 1) / (reps + 1)


def adf_tstat(levels: list[float]) -> float:
    lag = levels[:-1]
    delta = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    lag_mean = statistics.fmean(lag)
    delta_mean = statistics.fmean(delta)
    sxx = sum((x - lag_mean) ** 2 for x in lag)
    if sxx <= 1e-12:
        return 0.0
    gamma = sum((x - lag_mean) * (y - delta_mean) for x, y in zip(lag, delta)) / sxx
    intercept = delta_mean - gamma * lag_mean
    rss = sum((y - (intercept + gamma * x)) ** 2 for x, y in zip(lag, delta))
    sigma2 = rss / max(1, len(lag) - 2)
    se = math.sqrt(max(0.0, sigma2) / sxx)
    return gamma / se if se > 1e-12 else 0.0


def unit_root_block_adf_pvalue(levels: list[float], seed: int, reps: int) -> float:
    observed = adf_tstat(levels)
    increments = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    drift = statistics.fmean(increments)
    centered = [value - drift for value in increments]
    n = len(centered)
    block = max(2, min(n, int(round(math.sqrt(n)))))
    rng = random.Random(seed)
    left = 0
    for _ in range(reps):
        sampled = circular_block_sample(centered, n, block, rng)
        path = [levels[0]]
        for innovation in sampled:
            path.append(path[-1] + drift + innovation)
        if adf_tstat(path) <= observed:
            left += 1
    return (left + 1) / (reps + 1)


def random_walk(length: int, rho: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    levels = [0.0]
    previous = 0.0
    for _ in range(1, length):
        innovation = rng.gauss(0.0, 1.0)
        shock = rho * previous + innovation
        levels.append(levels[-1] + shock)
        previous = shock
    return levels


def stationary_ar1(length: int, phi: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    levels = [rng.gauss(0.0, 1.0)]
    for _ in range(1, length):
        levels.append(phi * levels[-1] + rng.gauss(0.0, 1.0))
    return levels


def rejection_rate(paths: list[list[float]], pvalue_fn, reps: int, seed: int, alpha: float = 0.10) -> float:
    rejected = 0
    for index, levels in enumerate(paths):
        if pvalue_fn(levels, seed + index, reps) <= alpha:
            rejected += 1
    return rejected / len(paths)


def run_experiment(paths: int, reps: int) -> dict[str, object]:
    null_rows = []
    for length in (48, 96, 336):
        for rho in (0.0, 0.5):
            samples = [
                random_walk(length, rho, 73_000_000 + length * 1000 + int(rho * 100) * 100 + i)
                for i in range(paths)
            ]
            null_rows.append(
                {
                    "points": length,
                    "innovation_ar1_rho": rho,
                    "score_centered_block_rejection_rate": rejection_rate(
                        samples, score_centered_block_pvalue, reps, 81_000_000 + length
                    ),
                    "unit_root_block_adf_rejection_rate": rejection_rate(
                        samples, unit_root_block_adf_pvalue, reps, 82_000_000 + length
                    ),
                }
            )
    power_samples = [stationary_ar1(336, 0.90, 93_600_000 + i) for i in range(paths)]
    return {
        "decision": "MORE_EVIDENCE_REQUIRED",
        "null": "I(1) levels with iid or AR(1) increments; no stationary residual alpha by construction",
        "alpha": 0.10,
        "paths_per_cell": paths,
        "bootstrap_reps": reps,
        "analytic_iid_unit_root_score_expectation_variance_1": iid_unit_root_score_expectation(1.0),
        "null_results": null_rows,
        "stationary_power_check": {
            "points": 336,
            "phi": 0.90,
            "score_centered_block_rejection_rate": rejection_rate(
                power_samples, score_centered_block_pvalue, reps, 83_000_000
            ),
            "unit_root_block_adf_rejection_rate": rejection_rate(
                power_samples, unit_root_block_adf_pvalue, reps, 84_000_000
            ),
        },
        "interpretation": (
            "For an iid random walk the sample-mean score has expectation -innovation_variance/2, "
            "so a negative score is mechanically present under the unit-root null. Resampling the centered score sequence "
            "does not impose the unit-root null on levels. A null-preserving bootstrap must reconstruct level paths "
            "under gamma=0 before multiplicity control."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only V6 unit-root bootstrap calibration audit")
    parser.add_argument("--paths", type=int, default=200)
    parser.add_argument("--reps", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.paths < 10 or args.reps < 50:
        raise SystemExit("paths >= 10 and reps >= 50 are required")
    result = run_experiment(args.paths, args.reps)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
