#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from statistics import NormalDist

NORMAL = NormalDist()


class StableRng:
    """Small version-stable RNG; 12 uniforms give a bounded Gaussian proxy."""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def uniform(self) -> float:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return (self.state + 0.5) / 2**32

    def normal(self) -> float:
        return sum(self.uniform() for _ in range(12)) - 6.0


def incumbent_ar_fit(resid: list[float]) -> tuple[float, float, float, float]:
    mu = statistics.fmean(resid)
    sd = statistics.stdev(resid)
    lag = resid[:-1]
    diff = [resid[i] - resid[i - 1] for i in range(1, len(resid))]
    ml = statistics.fmean(lag)
    md = statistics.fmean(diff)
    sxx = sum((x - ml) ** 2 for x in lag)
    if sxx < 1e-10:
        return 1.0, 0.0, mu, sd
    sxy = sum((x - ml) * (y - md) for x, y in zip(lag, diff))
    gamma = sxy / sxx
    intercept = md - gamma * ml
    rss = sum((y - (intercept + gamma * x)) ** 2 for x, y in zip(lag, diff))
    sigma2 = rss / max(1, len(lag) - 2)
    se = math.sqrt(max(0.0, sigma2) / sxx)
    tstat = gamma / se if se > 1e-12 else 0.0
    return 1.0 + gamma, tstat, mu, sd


def incumbent_pvalue(tstat: float) -> float:
    return min(1.0, max(0.0, 2.0 * (1.0 - NORMAL.cdf(abs(tstat)))))


def bh_cutoff(pvalues: list[float], q: float) -> float:
    ordered = sorted(p for p in pvalues if math.isfinite(p))
    cutoff = 0.0
    m = len(ordered)
    for rank, pvalue in enumerate(ordered, start=1):
        if pvalue <= q * rank / m:
            cutoff = pvalue
    return cutoff


def null_cluster(seed: int, points: int, markets: int = 5, innovation_rho: float = 0.0) -> dict[str, int | bool]:
    """Replicate V6 local-factor inference on an all-unit-root cluster.

    Each market is an I(1) logit-level process. Subtracting the cross-sectional
    mean factor leaves linear combinations of I(1) processes, so there is no
    stationary residual alpha by construction.
    """
    rng = StableRng(seed)
    standardized: dict[int, list[float]] = {}
    for market in range(markets):
        level = 0.0
        previous_innovation = 0.0
        path: list[float] = []
        for _ in range(points):
            innovation = rng.normal() + innovation_rho * previous_innovation
            previous_innovation = innovation
            level += innovation
            path.append(level)
        mean = statistics.fmean(path)
        sd = statistics.stdev(path)
        standardized[market] = [(x - mean) / sd for x in path]

    factor = [statistics.fmean(standardized[m][j] for m in range(markets)) for j in range(points)]
    factor_mean = statistics.fmean(factor)
    factor_var = sum((x - factor_mean) ** 2 for x in factor)

    candidates: list[tuple[float, float, float]] = []
    for market in range(markets):
        values = standardized[market]
        value_mean = statistics.fmean(values)
        loading = sum((x - value_mean) * (f - factor_mean) for x, f in zip(values, factor)) / factor_var
        if abs(loading) < 0.05:
            continue
        residual = [x - loading * f for x, f in zip(values, factor)]
        phi, tstat, residual_mean, residual_sd = incumbent_ar_fit(residual)
        if not (0.02 < phi < 0.999 and tstat < 0.0 and residual_sd > 0.0):
            continue
        residual_z = (residual[-1] - residual_mean) / residual_sd
        if abs(residual_z) < 1.0:
            continue
        candidates.append((incumbent_pvalue(tstat), residual_z, loading))

    cutoff = bh_cutoff([x[0] for x in candidates], 0.10) if candidates else 0.0
    eligible = [x for x in candidates if cutoff > 0.0 and x[0] <= cutoff]

    pairable = False
    for i, a in enumerate(eligible):
        for b in eligible[i + 1 :]:
            if a[1] * b[1] >= 0.0:
                continue
            side_sign_a = -1.0 if a[1] > 0.0 else 1.0
            side_sign_b = -1.0 if b[1] > 0.0 else 1.0
            if (side_sign_a * a[2]) * (side_sign_b * b[2]) < 0.0:
                pairable = True
                break
        if pairable:
            break

    return {
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "pairable": pairable,
    }


def run_cell(points: int, innovation_rho: float, clusters: int = 200) -> dict[str, float | int]:
    rows = [null_cluster(1000 + seed, points, innovation_rho=innovation_rho) for seed in range(clusters)]
    any_eligible = sum(int(row["eligible_count"]) > 0 for row in rows)
    pairable = sum(bool(row["pairable"]) for row in rows)
    eligible_total = sum(int(row["eligible_count"]) for row in rows)
    return {
        "points": points,
        "innovation_rho": innovation_rho,
        "clusters": clusters,
        "clusters_with_any_bh_rejection": any_eligible,
        "all_null_fdr": any_eligible / clusters,
        "clusters_with_pairable_false_signals": pairable,
        "pairable_rate": pairable / clusters,
        "eligible_signal_count": eligible_total,
    }


def source_contract(path: Path) -> dict[str, bool]:
    text = path.read_text(encoding="utf-8")
    checks = {
        "normal_pvalue": "2.0 * (1.0 - NORMAL.cdf(abs(tstat)))" in text,
        "unit_root_regression": "gamma = sxy / sxx" in text and "return 1.0 + gamma, t" in text,
        "bh_fdr": "cutoff=bh_cutoff" in text and "--fdr" in text,
        "same_pvalue_used_for_bh": "pvalue_from_t(tstat)" in text,
        "paper_loop_q_010": "--fdr 0.10" in Path("scripts/paper_v6_loop.sh").read_text(encoding="utf-8"),
    }
    return checks


def build_report() -> dict[str, object]:
    contract = source_contract(Path("scripts/v6_local_factor_intents.py"))
    cells = [
        run_cell(48, 0.0),
        run_cell(96, 0.0),
        run_cell(336, 0.0),
        run_cell(48, 0.5),
        run_cell(96, 0.5),
        run_cell(336, 0.5),
    ]
    return {
        "schema": "lf_v6_local_factor_fdr_audit_v1",
        "source_contract": contract,
        "nominal_bh_q": 0.10,
        "null_design": "five-market local clusters with I(1) logit levels; V6 cross-sectional mean factor; no stationary residual alpha",
        "cells": cells,
        "interpretation": (
            "Under an all-null design, FDR equals P(any rejection). The V6 standard-normal p-values are not valid "
            "unit-root p-values, so BH at q=0.10 does not deliver 10% FDR control in this diagnostic."
        ),
        "decision": "MORE_EVIDENCE_REQUIRED",
        "required_challenger": (
            "Keep broad local clusters and low paper-only discovery thresholds, but calibrate residual mean-reversion "
            "under the unit-root/dependence null before multiplicity control; compare common-sample executable OOS utility."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    report = build_report()
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
