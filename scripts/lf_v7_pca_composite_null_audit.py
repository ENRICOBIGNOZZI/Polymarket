#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from dataclasses import dataclass, asdict
from pathlib import Path

SOURCE_PR = 466
SOURCE_HEAD = "918f83f23b4fc586747457dcd9d7ef2dd9ddcbbb"
SOURCE_BOOTSTRAP = "joint_all_series_increment_i1"
PROPOSED_BOOTSTRAP = "conditional_target_residual_increment_i1"


def mean(xs):
    return statistics.fmean(xs)


def stdev(xs):
    return statistics.stdev(xs) if len(xs) >= 2 else 0.0


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def normalize(v):
    n = math.sqrt(max(0.0, dot(v, v)))
    return [x / n for x in v] if n > 1e-12 else None


def matvec(a, x):
    return [dot(row, x) for row in a]


def adf_t(levels):
    if len(levels) < 12:
        return 0.0
    lag = list(levels[:-1])
    delta = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    xm, ym = mean(lag), mean(delta)
    sxx = sum((x - xm) ** 2 for x in lag)
    if sxx <= 1e-12:
        return 0.0
    gamma = sum((x - xm) * (y - ym) for x, y in zip(lag, delta)) / sxx
    alpha = ym - gamma * xm
    rss = sum((y - alpha - gamma * x) ** 2 for x, y in zip(lag, delta))
    se = math.sqrt(max(0.0, rss / max(1, len(lag) - 2)) / sxx)
    return gamma / se if se > 1e-12 else 0.0


def fit_one_pc(panel):
    """Stylized target-excluded one-PC analogue of the V7 PCA residual test."""
    points = len(panel)
    if points < 12 or not panel or len(panel[0]) < 4:
        return None
    target = [row[0] for row in panel]
    controls = [[row[j] for row in panel] for j in range(1, len(panel[0]))]
    target_mean, target_sd = mean(target), stdev(target)
    control_means = [mean(row) for row in controls]
    control_sds = [stdev(row) for row in controls]
    if target_sd <= 1e-8 or any(x <= 1e-8 for x in control_sds):
        return None
    target_std = [(x - target_mean) / target_sd for x in target]
    control_std = [
        [(x - m) / s for x in row]
        for row, m, s in zip(controls, control_means, control_sds)
    ]
    covariance = [
        [dot(control_std[i], control_std[j]) / max(1, points - 1) for j in range(len(controls))]
        for i in range(len(controls))
    ]
    vector = normalize([1.0 + 0.1 * i for i in range(len(controls))])
    if vector is None:
        return None
    previous = 0.0
    for _ in range(100):
        candidate = normalize(matvec(covariance, vector))
        if candidate is None:
            return None
        eigenvalue = dot(candidate, matvec(covariance, candidate))
        vector = candidate
        if abs(eigenvalue - previous) <= 1e-10 * max(1.0, abs(eigenvalue)):
            break
        previous = eigenvalue
    factor = [sum(vector[j] * control_std[j][t] for j in range(len(controls))) for t in range(points)]
    denom = sum(x * x for x in factor) + 1e-4 * points
    beta = sum(x * y for x, y in zip(factor, target_std)) / denom
    fitted_std = [beta * x for x in factor]
    residual = [y - f for y, f in zip(target_std, fitted_std)]
    return {
        "adf_t": adf_t(residual),
        "residual": residual,
        "fitted_std": fitted_std,
        "target_mean": target_mean,
        "target_sd": target_sd,
    }


def block_indices(count, block, rng):
    out = []
    while len(out) < count:
        start = rng.randrange(count)
        out.extend((start + j) % count for j in range(block))
    return out[:count]


def joint_all_series_i1_bootstrap(panel, rng):
    """Replicates the inferential issue in PR #466: every panel series is forced to I(1)."""
    points, width = len(panel), len(panel[0])
    differences = [[panel[t][j] - panel[t - 1][j] for j in range(width)] for t in range(1, points)]
    drift = [mean([row[j] for row in differences]) for j in range(width)]
    centered = [[row[j] - drift[j] for j in range(width)] for row in differences]
    indices = block_indices(points - 1, max(2, round(math.sqrt(points - 1))), rng)
    output = [list(panel[0])]
    for index in indices:
        output.append([output[-1][j] + drift[j] + centered[index][j] for j in range(width)])
    return output


def conditional_target_residual_i1_bootstrap(panel, rng):
    """Impose the unit-root null only on the tested target residual; condition on observed controls."""
    fitted = fit_one_pc(panel)
    if fitted is None:
        return None
    residual = fitted["residual"]
    differences = [residual[t] - residual[t - 1] for t in range(1, len(residual))]
    drift = mean(differences)
    centered = [x - drift for x in differences]
    indices = block_indices(len(residual) - 1, max(2, round(math.sqrt(len(residual) - 1))), rng)
    null_residual = [residual[0]]
    for index in indices:
        null_residual.append(null_residual[-1] + drift + centered[index])
    target = [
        (fitted["fitted_std"][t] + null_residual[t]) * fitted["target_sd"] + fitted["target_mean"]
        for t in range(len(residual))
    ]
    output = [list(row) for row in panel]
    for t, value in enumerate(target):
        output[t][0] = value
    return output


def bootstrap_pvalues(panel, repetitions=79, seed=20260826):
    observed = fit_one_pc(panel)
    if observed is None:
        return None
    observed_t = observed["adf_t"]
    joint_rng = random.Random(seed)
    conditional_rng = random.Random(seed + 999_983)
    joint_left = conditional_left = 0
    for _ in range(repetitions):
        joint_fit = fit_one_pc(joint_all_series_i1_bootstrap(panel, joint_rng))
        if joint_fit is not None and joint_fit["adf_t"] <= observed_t:
            joint_left += 1
        conditional_panel = conditional_target_residual_i1_bootstrap(panel, conditional_rng)
        conditional_fit = fit_one_pc(conditional_panel) if conditional_panel is not None else None
        if conditional_fit is not None and conditional_fit["adf_t"] <= observed_t:
            conditional_left += 1
    return {
        "observed_adf_t": observed_t,
        "joint_all_i1_p": (joint_left + 1) / (repetitions + 1),
        "conditional_target_residual_p": (conditional_left + 1) / (repetitions + 1),
    }


def generate_stationary_control_panel(points, seed, residual_phi=None):
    """Controls share a stationary AR factor; target residual is I(1) under null or AR(1) under alternative."""
    rng = random.Random(seed)
    factor = 0.0
    idiosyncratic = [0.0, 0.0, 0.0]
    residual = 0.0
    panel = []
    for t in range(points):
        if t:
            factor = 0.7 * factor + rng.gauss(0.0, 1.0)
            idiosyncratic = [0.3 * x + rng.gauss(0.0, 0.5) for x in idiosyncratic]
            if residual_phi is None:
                residual += rng.gauss(0.0, 0.5)
            else:
                residual = residual_phi * residual + rng.gauss(0.0, 0.5)
        controls = [factor + x for x in idiosyncratic]
        target = 1.2 * factor + residual
        panel.append([target] + controls)
    return panel


@dataclass(frozen=True)
class Cell:
    residual: str
    outer_paths: int
    repetitions: int
    joint_rejections_10pct: int
    conditional_rejections_10pct: int
    joint_median_p: float
    conditional_median_p: float


def diagnostic(outer_paths=40, repetitions=79, points=96):
    cells = []
    for label, phi in (("unit_root_null", None), ("ar1_phi_0.90", 0.90), ("ar1_phi_0.80", 0.80)):
        joint_p, conditional_p = [], []
        for index in range(outer_paths):
            panel = generate_stationary_control_panel(points, 10_000 + index, phi)
            result = bootstrap_pvalues(panel, repetitions, 30_000 + index)
            if result is None:
                continue
            joint_p.append(result["joint_all_i1_p"])
            conditional_p.append(result["conditional_target_residual_p"])
        cells.append(
            Cell(
                residual=label,
                outer_paths=len(joint_p),
                repetitions=repetitions,
                joint_rejections_10pct=sum(x <= 0.10 for x in joint_p),
                conditional_rejections_10pct=sum(x <= 0.10 for x in conditional_p),
                joint_median_p=statistics.median(joint_p),
                conditional_median_p=statistics.median(conditional_p),
            )
        )
    return {
        "source_pr": SOURCE_PR,
        "source_head": SOURCE_HEAD,
        "finding": "PR #466 bootstraps every target and control level as I(1), although the marginal null concerns the target residual conditional on a target-excluded control factor panel.",
        "source_bootstrap": SOURCE_BOOTSTRAP,
        "candidate_bootstrap": PROPOSED_BOOTSTRAP,
        "interpretation": "The all-series-I(1) bootstrap changes nuisance-control persistence. In the deterministic stationary-control fixture it is substantially less powerful than a conditional target-residual unit-root bootstrap. This is a calibration/power diagnostic, not Polymarket PnL evidence.",
        "cells": [asdict(cell) for cell in cells],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outer-paths", type=int, default=40)
    parser.add_argument("--repetitions", type=int, default=79)
    parser.add_argument("--points", type=int, default=96)
    parser.add_argument("--output")
    args = parser.parse_args()
    report = diagnostic(max(1, args.outer_paths), max(19, args.repetitions), max(36, args.points))
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
