#!/usr/bin/env python3
from __future__ import annotations

import math
import statistics
from typing import Any

try:
    import v6_micro_taker_v2 as base
except ModuleNotFoundError:
    from scripts import v6_micro_taker_v2 as base

BASE_FEATURES = base.features
FEATURE_VERSION = 3
FEATURE_COUNT = 14


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def augment_vector(x: list[float], mid: float) -> list[float]:
    core = [float(v) for v in x[:10]]
    if len(core) < 10:
        core += [0.0] * (10 - len(core))
    m = clip(float(mid), 1e-5, 1.0 - 1e-5)
    log_odds = clip(math.log(m / (1.0 - m)) / 4.0, -3.0, 3.0)
    flow_agreement = clip(0.5 * (core[6] + core[7]), -1.0, 1.0)
    pressure_interaction = clip(0.25 * (core[1] + core[2]) * (core[3] + core[4]), -2.0, 2.0)
    liquidity_regime = math.tanh(core[9] - core[8])
    return core + [log_odds, flow_agreement, pressure_interaction, liquidity_regime]


def features(y: Any, n: Any, flow: Any, flow_window: int):
    out = BASE_FEATURES(y, n, flow, flow_window)
    if out is None:
        return None
    x, mid, spread = out
    return augment_vector(list(x), mid), mid, spread


def _solve(A: list[list[float]], b: list[float]) -> list[float] | None:
    p = len(b)
    for i in range(p):
        pivot = max(range(i, p), key=lambda r: abs(A[r][i]))
        if abs(A[pivot][i]) < 1e-12:
            return None
        A[i], A[pivot] = A[pivot], A[i]
        b[i], b[pivot] = b[pivot], b[i]
        d = A[i][i]
        A[i] = [v / d for v in A[i]]
        b[i] /= d
        for r in range(p):
            if r == i:
                continue
            q = A[r][i]
            if abs(q) < 1e-14:
                continue
            A[r] = [A[r][c] - q * A[i][c] for c in range(p)]
            b[r] -= q * b[i]
    return b


def _usable_row(row: dict[str, Any], category: str | None) -> tuple[list[float], float, float] | None:
    if row.get("y") is None or (category is not None and str(row.get("category") or "unknown") != category):
        return None
    raw_x = row.get("x")
    if not isinstance(raw_x, list):
        return None
    version = int(base.finite(row.get("feature_version"), 0.0))
    if version == FEATURE_VERSION and len(raw_x) == FEATURE_COUNT:
        x = [float(v) for v in raw_x]
    elif version == 2 and len(raw_x) == 10:
        x = augment_vector([float(v) for v in raw_x], base.finite(row.get("mid"), 0.5))
    else:
        return None
    spread = max(1e-6, base.finite(row.get("spread"), 1e-3))
    target = float(row["y"]) / spread
    return x, target, spread


def solve_weighted_ridge(
    rows: list[dict[str, Any]],
    *,
    ridge: float,
    now: int,
    half_life_seconds: float,
    category: str | None = None,
) -> tuple[list[float], int]:
    """EW ridge with Huber IRLS and backward-compatible V2 samples.

    The target remains the causal fixed-horizon markout. V3 adds nonlinear
    state interactions and a robust residual weighting pass so jump events do
    not dominate the online short-horizon model.
    """
    selected: list[tuple[list[float], float, float, float]] = []
    decay = math.log(2.0) / max(1.0, half_life_seconds)
    for row in rows[-30000:]:
        usable = _usable_row(row, category)
        if usable is None:
            continue
        x, target, _spread = usable
        age = max(0.0, now - base.finite(row.get("ts"), now))
        selected.append((x, target, math.exp(-decay * age), age))
    n = len(selected)
    if n < 50:
        return [0.0] * FEATURE_COUNT, n

    def fit(extra_weights: list[float] | None = None) -> list[float] | None:
        p = FEATURE_COUNT
        A = [[0.0] * p for _ in range(p)]
        b = [0.0] * p
        for idx, (x, target, time_weight, _age) in enumerate(selected):
            w = time_weight * (extra_weights[idx] if extra_weights is not None else 1.0)
            for i in range(p):
                b[i] += w * x[i] * target
                for j in range(p):
                    A[i][j] += w * x[i] * x[j]
        for i in range(1, p):
            A[i][i] += ridge
        return _solve(A, b)

    beta = fit()
    if beta is None:
        return [0.0] * FEATURE_COUNT, n
    residuals = [target - sum(a * b for a, b in zip(beta, x)) for x, target, _w, _age in selected]
    med = statistics.median(residuals)
    mad = statistics.median(abs(r - med) for r in residuals)
    scale = max(1e-4, 1.4826 * mad)
    huber_c = 1.5 * scale
    robust_weights = [1.0 if abs(r - med) <= huber_c else huber_c / max(abs(r - med), 1e-12) for r in residuals]
    refined = fit(robust_weights)
    return (refined if refined is not None else beta), n


def main() -> int:
    base.FEATURE_VERSION = FEATURE_VERSION
    base.FEATURE_COUNT = FEATURE_COUNT
    base.features = features
    base.solve_weighted_ridge = solve_weighted_ridge
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
