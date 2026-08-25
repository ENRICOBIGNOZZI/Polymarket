#!/usr/bin/env python3
from __future__ import annotations

import math
import statistics
from collections import defaultdict
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


def label_executable_samples(
    samples: list[dict[str, Any]],
    *,
    now: int,
    horizon_seconds: int,
    max_target_staleness_seconds: int,
) -> dict[str, Any]:
    """Causal pre-horizon label using a spread-net executable markout proxy.

    The future observation is still the last locally observed state at or before
    t+h. The response removes the two-sided half-spread hurdle so the model is
    trained on moves large enough to survive passive-to-aggressive execution
    before fees/slippage, which remain charged explicitly at decision time.
    """
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    by_market: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in samples:
        market_id = str(row.get("market_id") or "")
        ts = int(base.finite(row.get("ts"), 0.0))
        mid = base.finite(row.get("mid"), math.nan)
        spread = max(0.0, base.finite(row.get("spread"), 0.0))
        if market_id and ts > 0 and math.isfinite(mid):
            by_market[market_id].append((ts, mid, spread))
    for values in by_market.values():
        values.sort(key=lambda x: x[0])

    labeled = missing = stale = 0
    lags: list[int] = []
    for row in samples:
        if row.get("y") is not None:
            continue
        origin_ts = int(base.finite(row.get("ts"), 0.0))
        market_id = str(row.get("market_id") or "")
        origin_mid = base.finite(row.get("mid"), math.nan)
        origin_spread = max(0.0, base.finite(row.get("spread"), 0.0))
        if origin_ts <= 0 or not market_id or not math.isfinite(origin_mid):
            continue
        target_ts = origin_ts + horizon_seconds
        if now < target_ts:
            continue
        chosen = None
        for obs in by_market.get(market_id, []):
            if obs[0] <= origin_ts:
                continue
            if obs[0] > target_ts:
                break
            chosen = obs
        if chosen is None:
            missing += 1
            continue
        obs_ts, obs_mid, obs_spread = chosen
        lag = target_ts - obs_ts
        if lag > max_target_staleness_seconds:
            stale += 1
            continue
        raw_delta = obs_mid - origin_mid
        hurdle = 0.5 * (origin_spread + obs_spread)
        executable_delta = math.copysign(max(0.0, abs(raw_delta) - hurdle), raw_delta) if raw_delta else 0.0
        row["y"] = executable_delta
        row["raw_mid_delta"] = raw_delta
        row["spread_hurdle"] = hurdle
        row["target_observation_ts"] = obs_ts
        row["target_staleness_seconds"] = lag
        row["target_kind"] = "causal_spread_net_markout"
        labeled += 1
        lags.append(lag)
    return {
        "newly_labeled": labeled,
        "missing_pre_horizon_observation": missing,
        "stale_pre_horizon_observation": stale,
        "max_target_staleness_seconds": max(lags) if lags else None,
        "mean_target_staleness_seconds": (sum(lags) / len(lags)) if lags else None,
        "target_kind": "causal_spread_net_markout",
    }


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


def _usable_row(row: dict[str, Any], category: str | None) -> tuple[list[float], float] | None:
    if row.get("y") is None or (category is not None and str(row.get("category") or "unknown") != category):
        return None
    raw_x = row.get("x")
    if not isinstance(raw_x, list):
        return None
    if int(base.finite(row.get("feature_version"), 0.0)) != FEATURE_VERSION or len(raw_x) != FEATURE_COUNT:
        return None
    spread = max(1e-6, base.finite(row.get("spread"), 1e-3))
    return [float(v) for v in raw_x], float(row["y"]) / spread


def solve_weighted_ridge(
    rows: list[dict[str, Any]],
    *,
    ridge: float,
    now: int,
    half_life_seconds: float,
    category: str | None = None,
) -> tuple[list[float], int]:
    selected: list[tuple[list[float], float, float]] = []
    decay = math.log(2.0) / max(1.0, half_life_seconds)
    for row in rows[-30000:]:
        usable = _usable_row(row, category)
        if usable is None:
            continue
        x, target = usable
        age = max(0.0, now - base.finite(row.get("ts"), now))
        selected.append((x, target, math.exp(-decay * age)))
    n = len(selected)
    if n < 50:
        return [0.0] * FEATURE_COUNT, n

    def fit(extra_weights: list[float] | None = None) -> list[float] | None:
        p = FEATURE_COUNT
        A = [[0.0] * p for _ in range(p)]
        b = [0.0] * p
        for idx, (x, target, time_weight) in enumerate(selected):
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
    residuals = [target - sum(a * b for a, b in zip(beta, x)) for x, target, _w in selected]
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
    base.label_matured_samples = label_executable_samples
    base.solve_weighted_ridge = solve_weighted_ridge
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
