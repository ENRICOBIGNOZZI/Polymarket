#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence


def stdev(values: Sequence[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def standardize(values: Sequence[float]) -> tuple[float, ...]:
    vals = tuple(float(x) for x in values)
    mu = statistics.fmean(vals)
    sd = stdev(vals)
    if sd <= 1e-12:
        raise ValueError("cannot standardize a constant series")
    return tuple((x - mu) / sd for x in vals)


def covariance(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("series must have the same nontrivial length")
    am = statistics.fmean(a)
    bm = statistics.fmean(b)
    return sum((x - am) * (y - bm) for x, y in zip(a, b)) / (len(a) - 1)


def correlation(a: Sequence[float], b: Sequence[float]) -> float:
    denom = stdev(a) * stdev(b)
    if denom <= 1e-12:
        return 0.0
    return covariance(a, b) / denom


def ols_loading(target: Sequence[float], factor: Sequence[float]) -> float | None:
    if len(target) != len(factor) or len(target) < 4:
        return None
    fm = statistics.fmean(factor)
    tm = statistics.fmean(target)
    fvar = sum((x - fm) ** 2 for x in factor)
    if fvar <= 1e-12:
        return None
    return sum((x - fm) * (y - tm) for x, y in zip(factor, target)) / fvar


def equal_weight_factor(controls: Mapping[str, Sequence[float]]) -> tuple[float, ...]:
    names = sorted(controls)
    if len(names) < 2:
        raise ValueError("at least two controls are required")
    n = len(controls[names[0]])
    if any(len(controls[name]) != n for name in names):
        raise ValueError("control lengths differ")
    return tuple(statistics.fmean(controls[name][i] for name in names) for i in range(n))


def anchor_oriented_factor(controls: Mapping[str, Sequence[float]]) -> tuple[float, ...]:
    """Target-free comparator showing why raw equal-weight averaging is not orientation invariant.

    This is an audit comparator, not a production recommendation. A production
    successor can use a target-excluded first principal component or another
    pre-specified control-only orientation rule. The key contract is that the
    common factor must not disappear merely because economically related controls
    are coded with opposite probability direction.
    """
    names = sorted(controls)
    if len(names) < 2:
        raise ValueError("at least two controls are required")
    anchor = tuple(float(x) for x in controls[names[0]])
    oriented: dict[str, tuple[float, ...]] = {names[0]: anchor}
    for name in names[1:]:
        row = tuple(float(x) for x in controls[name])
        sign = -1.0 if covariance(anchor, row) < 0.0 else 1.0
        oriented[name] = tuple(sign * x for x in row)
    return equal_weight_factor(oriented)


@dataclass(frozen=True)
class AuditResult:
    observations: int
    controls: int
    raw_equal_factor_sd: float
    raw_equal_loading_a: float | None
    raw_equal_loading_b: float | None
    oriented_factor_sd: float
    oriented_latent_abs_correlation: float
    oriented_loading_a: float | None
    oriented_loading_b: float | None
    sign_flip_abs_correlation: float
    raw_factor_annihilated: bool
    orientation_invariant_up_to_global_sign: bool


def deterministic_fixture() -> tuple[tuple[float, ...], dict[str, tuple[float, ...]], tuple[float, ...], tuple[float, ...]]:
    n = 72
    latent = [
        math.sin(2.0 * math.pi * t / 12.0) + 0.25 * math.sin(2.0 * math.pi * t / 7.0)
        for t in range(n)
    ]
    factor = standardize(latent)
    controls = {
        "c1": factor,
        "c2": factor,
        "c3": tuple(-x for x in factor),
        "c4": tuple(-x for x in factor),
    }
    noise_a = standardize([math.sin(2.0 * math.pi * t / 5.0) for t in range(n)])
    noise_b = standardize([math.cos(2.0 * math.pi * t / 9.0) for t in range(n)])
    target_a = standardize([0.80 * f + 0.15 * e for f, e in zip(factor, noise_a)])
    target_b = standardize([-0.65 * f + 0.15 * e for f, e in zip(factor, noise_b)])
    return factor, controls, target_a, target_b


def run_audit() -> AuditResult:
    latent, controls, target_a, target_b = deterministic_fixture()
    raw = equal_weight_factor(controls)
    oriented = anchor_oriented_factor(controls)

    recoded = {
        "c1": tuple(-x for x in controls["c1"]),
        "c2": controls["c2"],
        "c3": controls["c3"],
        "c4": tuple(-x for x in controls["c4"]),
    }
    oriented_recoded = anchor_oriented_factor(recoded)

    raw_sd = stdev(raw)
    return AuditResult(
        observations=len(latent),
        controls=len(controls),
        raw_equal_factor_sd=raw_sd,
        raw_equal_loading_a=ols_loading(target_a, raw),
        raw_equal_loading_b=ols_loading(target_b, raw),
        oriented_factor_sd=stdev(oriented),
        oriented_latent_abs_correlation=abs(correlation(oriented, latent)),
        oriented_loading_a=ols_loading(target_a, oriented),
        oriented_loading_b=ols_loading(target_b, oriented),
        sign_flip_abs_correlation=abs(correlation(oriented, oriented_recoded)),
        raw_factor_annihilated=raw_sd <= 1e-12,
        orientation_invariant_up_to_global_sign=abs(correlation(oriented, oriented_recoded)) >= 1.0 - 1e-12,
    )


def main() -> int:
    result = run_audit()
    payload = {
        "schema": "lf_v7_control_orientation_audit_v1",
        "decision": "MORE_EVIDENCE_REQUIRED",
        "finding": "pair-excluded equal-weight controls are not orientation invariant and can annihilate a genuine common factor",
        "result": asdict(result),
        "required_successor_contract": [
            "freeze pair and nuisance controls before target price inference",
            "estimate one common target-excluded factor basis for both legs",
            "make the factor basis invariant up to a global sign to opposite control probability coding",
            "compare equal-weight mean against target-excluded control PCA or another pre-specified control-only orientation rule on identical chronological panels",
            "run unit-root null-preserving marginal inference and dependence-robust multiplicity only after the common factor is well-defined",
            "require price-PnL hedge units, n-step fidelity/TTR alignment, joint fill states and partial abort/unwind economics before threshold aggression",
        ],
        "safety": {
            "paper_only": True,
            "authenticated_execution": False,
            "real_money_execution": False,
            "max_drawdown": 0.15,
            "max_market_fraction": 0.05,
            "max_event_fraction": 0.15,
            "max_gross_fraction": 0.70,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.raw_factor_annihilated and result.orientation_invariant_up_to_global_sign else 1


if __name__ == "__main__":
    raise SystemExit(main())
