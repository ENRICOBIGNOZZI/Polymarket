#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable


def harmonic_number(m: int) -> float:
    m = int(m)
    if m <= 0:
        return 0.0
    return sum(1.0 / k for k in range(1, m + 1))


def bh_rejections(pvalues: Iterable[float], q: float) -> int:
    ps = sorted(float(p) for p in pvalues if math.isfinite(float(p)))
    m = len(ps)
    cutoff_rank = 0
    level = min(0.5, max(1e-12, float(q)))
    for i, p in enumerate(ps, start=1):
        if p <= level * i / max(1, m):
            cutoff_rank = i
    return cutoff_rank


def by_rejections(pvalues: Iterable[float], q: float) -> int:
    ps = list(pvalues)
    if not ps:
        return 0
    return bh_rejections(ps, float(q) / harmonic_number(len(ps)))


def sharp_dependence_event(m: int, q: float, k: int) -> tuple[list[float], float]:
    """One event in the classical arbitrary-dependence sharp construction.

    On event E_k exactly k p-values equal k*q/m and the others equal one.
    E_k has probability q/k.  Averaging uniformly over the choice of the k
    hypotheses makes every marginal p-value valid at every BH grid point, while
    BH rejects on every E_k.  Summing over k gives global-null FDR q*H_m.
    """
    m = int(m)
    k = int(k)
    if not 1 <= k <= m:
        raise ValueError("k must be in 1..m")
    level = float(q)
    if level <= 0.0 or level * harmonic_number(m) >= 1.0:
        raise ValueError("construction requires 0 < q*H_m < 1")
    p = k * level / m
    return [p] * k + [1.0] * (m - k), level / k


def marginal_grid_cdf(m: int, q: float, rank: int) -> float:
    """Exact marginal CDF at t=rank*q/m under the sharp construction."""
    rank = max(0, min(int(rank), int(m)))
    return rank * float(q) / int(m) if m > 0 else 0.0


def audit(m: int, q: float) -> dict[str, object]:
    h = harmonic_number(m)
    worst_case_fdr = q * h
    by_q = q / h if h > 0.0 else 0.0
    representative_ranks = sorted({1, 2, 5, 10, max(1, m // 10), max(1, m // 2), m})
    examples = []
    for k in representative_ranks:
        pvalues, event_probability = sharp_dependence_event(m, q, k)
        examples.append(
            {
                "rank": k,
                "event_probability": event_probability,
                "nonnull_pvalue_value": k * q / m,
                "bh_rejections": bh_rejections(pvalues, q),
                "by_rejections": by_rejections(pvalues, q),
            }
        )
    grid_checks = []
    for r in representative_ranks:
        t = r * q / m
        cdf = marginal_grid_cdf(m, q, r)
        grid_checks.append({"rank": r, "threshold": t, "marginal_cdf": cdf, "valid": cdf <= t + 1e-15})
    return {
        "hypotheses": m,
        "nominal_bh_q": q,
        "harmonic_number": h,
        "arbitrary_dependence_global_null_fdr": worst_case_fdr,
        "benjamini_yekutieli_effective_q": by_q,
        "marginal_pvalues_valid_on_bh_grid": all(bool(row["valid"]) for row in grid_checks),
        "bh_rejects_on_every_positive_mass_event": all(int(row["bh_rejections"]) > 0 for row in examples),
        "representative_events": examples,
        "marginal_grid_checks": grid_checks,
        "interpretation": (
            "Valid marginal p-values alone do not justify ordinary BH under arbitrary dependence. "
            "Overlapping Local Factor pair hypotheses share targets, controls and bootstrap panels, so "
            "PRDS/independence must be established or a dependence-robust multiplicity method used before "
            "treating BH q as a calibrated FDR level."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit BH FDR sensitivity to dependent V7 LF pair hypotheses")
    parser.add_argument("--hypotheses", type=int, default=1322)
    parser.add_argument("--q", type=float, default=0.10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.hypotheses, args.q)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
