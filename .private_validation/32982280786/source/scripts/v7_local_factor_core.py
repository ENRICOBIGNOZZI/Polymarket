#!/usr/bin/env python3
from __future__ import annotations

import itertools
import math
import random
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence


def clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def logit(p: float) -> float:
    p = clamp(float(p), 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def logistic(z: float) -> float:
    if z >= 0:
        e = math.exp(-min(40.0, z))
        return 1.0 / (1.0 + e)
    e = math.exp(max(-40.0, z))
    return e / (1.0 + e)


def stdev(xs: Sequence[float]) -> float:
    return statistics.stdev(xs) if len(xs) >= 2 else 0.0


@dataclass(frozen=True)
class StandardizedPanel:
    times: tuple[int, ...]
    values: dict[str, tuple[float, ...]]
    means: dict[str, float]
    scales: dict[str, float]


@dataclass(frozen=True)
class PairFit:
    market_a: str
    market_b: str
    controls: tuple[str, ...]
    loading_a: float
    loading_b: float
    residual_a: tuple[float, ...]
    residual_b: tuple[float, ...]
    phi_a: float
    phi_b: float
    residual_mean_a: float
    residual_mean_b: float
    residual_sd_a: float
    residual_sd_b: float
    residual_z_a: float
    residual_z_b: float
    adf_a: float
    adf_b: float
    pair_stat: float


@dataclass(frozen=True)
class PairSignal:
    market_a: str
    market_b: str
    side_a: str
    side_b: str
    weight_a: float
    weight_b: float
    hold_seconds: int
    residual_change_a: float
    residual_change_b: float
    factor_exposure_a: float
    factor_exposure_b: float
    pvalue: float


@dataclass(frozen=True)
class JointFillDistribution:
    both: float
    a_only: float
    b_only: float
    none: float
    observations: int = 0

    def valid(self, tol: float = 1e-8) -> bool:
        vals = (self.both, self.a_only, self.b_only, self.none)
        return all(math.isfinite(x) and x >= -tol for x in vals) and abs(sum(vals) - 1.0) <= tol


@dataclass(frozen=True)
class JointExecutionEV:
    ev: float
    full_completion_component: float
    a_only_component: float
    b_only_component: float
    capital_latency_cost: float


def longest_regular_suffix(times: Sequence[int], bucket_seconds: int) -> tuple[int, ...]:
    ordered = sorted(set(int(t) for t in times))
    if not ordered:
        return ()
    start = len(ordered) - 1
    while start > 0 and ordered[start] - ordered[start - 1] == bucket_seconds:
        start -= 1
    return tuple(ordered[start:])


def build_regular_panel(
    histories: Mapping[str, Mapping[int, float]],
    market_ids: Sequence[str],
    bucket_seconds: int,
    min_points: int,
) -> StandardizedPanel | None:
    ids = [mid for mid in market_ids if mid in histories]
    if len(ids) < 4:
        return None
    common = set(histories[ids[0]])
    for mid in ids[1:]:
        common &= set(histories[mid])
    times = longest_regular_suffix(sorted(common), bucket_seconds)
    if len(times) < min_points:
        return None
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    std: dict[str, tuple[float, ...]] = {}
    for mid in ids:
        vals = [float(histories[mid][t]) for t in times]
        if not all(math.isfinite(v) for v in vals):
            return None
        mu = statistics.fmean(vals)
        sd = stdev(vals)
        if sd <= 1e-6:
            continue
        means[mid] = mu
        scales[mid] = sd
        std[mid] = tuple((v - mu) / sd for v in vals)
    if len(std) < 4:
        return None
    return StandardizedPanel(times=times, values=std, means=means, scales=scales)


def standardize_levels(levels: Mapping[str, Sequence[float]], times: Sequence[int]) -> StandardizedPanel | None:
    std: dict[str, tuple[float, ...]] = {}
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    for mid, raw in levels.items():
        vals = [float(x) for x in raw]
        if len(vals) != len(times) or len(vals) < 4:
            return None
        mu = statistics.fmean(vals)
        sd = stdev(vals)
        if sd <= 1e-8:
            return None
        means[mid] = mu
        scales[mid] = sd
        std[mid] = tuple((v - mu) / sd for v in vals)
    return StandardizedPanel(tuple(times), std, means, scales)


def ols_loading(target: Sequence[float], factor: Sequence[float]) -> float | None:
    if len(target) != len(factor) or len(target) < 4:
        return None
    tm, fm = statistics.fmean(target), statistics.fmean(factor)
    fvar = sum((x - fm) ** 2 for x in factor)
    if fvar <= 1e-10:
        return None
    return sum((y - tm) * (x - fm) for y, x in zip(target, factor)) / fvar


def ar1_fit(levels: Sequence[float]) -> tuple[float, float, float]:
    if len(levels) < 12:
        return 1.0, statistics.fmean(levels) if levels else 0.0, stdev(levels)
    lag = list(levels[:-1])
    lead = list(levels[1:])
    lm, ym = statistics.fmean(lag), statistics.fmean(lead)
    sxx = sum((x - lm) ** 2 for x in lag)
    if sxx <= 1e-12:
        return 1.0, statistics.fmean(levels), stdev(levels)
    phi = sum((x - lm) * (y - ym) for x, y in zip(lag, lead)) / sxx
    intercept = ym - phi * lm
    mu = intercept / (1.0 - phi) if abs(1.0 - phi) > 1e-8 else statistics.fmean(levels)
    return phi, mu, stdev(levels)


def adf_t_stat(levels: Sequence[float]) -> float:
    if len(levels) < 12:
        return 0.0
    x = list(levels[:-1])
    dy = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    xm, ym = statistics.fmean(x), statistics.fmean(dy)
    sxx = sum((v - xm) ** 2 for v in x)
    if sxx <= 1e-12:
        return 0.0
    gamma = sum((a - xm) * (b - ym) for a, b in zip(x, dy)) / sxx
    alpha = ym - gamma * xm
    rss = sum((b - alpha - gamma * a) ** 2 for a, b in zip(x, dy))
    se = math.sqrt(max(0.0, rss / max(1, len(x) - 2)) / sxx)
    return gamma / se if se > 1e-12 else 0.0


def fit_pair(panel: StandardizedPanel, market_a: str, market_b: str, min_controls: int = 2) -> PairFit | None:
    if market_a == market_b or market_a not in panel.values or market_b not in panel.values:
        return None
    controls = tuple(sorted(mid for mid in panel.values if mid not in {market_a, market_b}))
    if len(controls) < min_controls:
        return None
    n = len(panel.times)
    # One pair-excluded factor is shared by both target regressions.  This is the
    # basis on which loading signs and hedge weights may be compared.
    factor = tuple(statistics.fmean(panel.values[mid][j] for mid in controls) for j in range(n))
    loading_a = ols_loading(panel.values[market_a], factor)
    loading_b = ols_loading(panel.values[market_b], factor)
    if loading_a is None or loading_b is None:
        return None
    residual_a = tuple(y - loading_a * f for y, f in zip(panel.values[market_a], factor))
    residual_b = tuple(y - loading_b * f for y, f in zip(panel.values[market_b], factor))
    phi_a, mu_a, sd_a = ar1_fit(residual_a)
    phi_b, mu_b, sd_b = ar1_fit(residual_b)
    if sd_a <= 1e-8 or sd_b <= 1e-8:
        return None
    adf_a = adf_t_stat(residual_a)
    adf_b = adf_t_stat(residual_b)
    # Intersection null: a usable pair requires both residuals to reject a unit root.
    pair_stat = max(adf_a, adf_b)
    return PairFit(
        market_a=market_a,
        market_b=market_b,
        controls=controls,
        loading_a=loading_a,
        loading_b=loading_b,
        residual_a=residual_a,
        residual_b=residual_b,
        phi_a=phi_a,
        phi_b=phi_b,
        residual_mean_a=mu_a,
        residual_mean_b=mu_b,
        residual_sd_a=sd_a,
        residual_sd_b=sd_b,
        residual_z_a=(residual_a[-1] - mu_a) / sd_a,
        residual_z_b=(residual_b[-1] - mu_b) / sd_b,
        adf_a=adf_a,
        adf_b=adf_b,
        pair_stat=pair_stat,
    )


def all_pairs(panel: StandardizedPanel) -> list[tuple[str, str]]:
    return list(itertools.combinations(sorted(panel.values), 2))


def _joint_block_indices(n_increments: int, block: int, rng: random.Random) -> list[int]:
    out: list[int] = []
    while len(out) < n_increments:
        start = rng.randrange(n_increments)
        out.extend((start + j) % n_increments for j in range(block))
    return out[:n_increments]


def null_panel_bootstrap(
    panel: StandardizedPanel,
    rng: random.Random,
    block: int | None = None,
) -> StandardizedPanel | None:
    mids = sorted(panel.values)
    n = len(panel.times)
    if n < 12:
        return None
    ninc = n - 1
    block = block or max(2, min(ninc, int(round(math.sqrt(ninc)))))
    increments: dict[str, list[float]] = {}
    drift: dict[str, float] = {}
    for mid in mids:
        vals = panel.values[mid]
        diffs = [vals[i] - vals[i - 1] for i in range(1, n)]
        drift[mid] = statistics.fmean(diffs)
        increments[mid] = [x - drift[mid] for x in diffs]
    # Resample time blocks jointly across the entire panel so contemporaneous
    # cross-sectional dependence is retained while each reconstructed level path
    # is I(1).  The first-stage standardization/factor/loadings are then re-estimated.
    indices = _joint_block_indices(ninc, block, rng)
    levels: dict[str, list[float]] = {mid: [panel.values[mid][0]] for mid in mids}
    for idx in indices:
        for mid in mids:
            levels[mid].append(levels[mid][-1] + drift[mid] + increments[mid][idx])
    return standardize_levels(levels, panel.times)


def panel_pair_bootstrap_pvalues(
    panel: StandardizedPanel,
    pairs: Sequence[tuple[str, str]] | None = None,
    reps: int = 300,
    seed: int = 20260826,
    min_controls: int = 2,
) -> dict[tuple[str, str], tuple[PairFit, float]]:
    # Crucially, p-values are computed for all pair hypotheses before residual-z,
    # phi or economic screening.  This prevents those data-dependent screens from
    # entering BH as if they were pre-specified hypotheses.
    pairs = list(pairs or all_pairs(panel))
    observed: dict[tuple[str, str], PairFit] = {}
    for pair in pairs:
        fit = fit_pair(panel, pair[0], pair[1], min_controls=min_controls)
        if fit is not None:
            observed[pair] = fit
    if not observed:
        return {}
    left = {pair: 0 for pair in observed}
    total = max(50, int(reps))
    rng = random.Random(seed)
    for _ in range(total):
        boot = null_panel_bootstrap(panel, rng)
        if boot is None:
            continue
        for pair, observed_fit in observed.items():
            fit = fit_pair(boot, pair[0], pair[1], min_controls=min_controls)
            if fit is not None and fit.pair_stat <= observed_fit.pair_stat:
                left[pair] += 1
    return {
        pair: (fit, (left[pair] + 1.0) / (total + 1.0))
        for pair, fit in observed.items()
    }


def bh_selected(pvalues: Mapping[tuple[str, str], float], q: float) -> set[tuple[str, str]]:
    ordered = sorted((p, pair) for pair, p in pvalues.items() if math.isfinite(p))
    m = len(ordered)
    cutoff = 0.0
    for i, (p, _pair) in enumerate(ordered, start=1):
        if p <= clamp(q, 1e-6, 0.5) * i / max(1, m):
            cutoff = p
    return {pair for p, pair in ordered if cutoff > 0.0 and p <= cutoff}


def half_life_bars(phi: float) -> float:
    if not 0.0 < phi < 1.0:
        return math.inf
    return -math.log(2.0) / math.log(phi)


def residual_change(phi: float, current: float, mean: float, steps: float) -> float:
    if not 0.0 < phi < 1.0:
        return 0.0
    return (phi ** max(0.0, steps) - 1.0) * (current - mean)


def price_factor_exposure(side: str, probability: float, yes_sd: float, loading: float) -> float:
    sign = 1.0 if side.upper() == "YES" else -1.0
    p = clamp(probability, 1e-6, 1.0 - 1e-6)
    return sign * p * (1.0 - p) * yes_sd * loading


def build_pair_signal(
    fit: PairFit,
    pvalue: float,
    probabilities: Mapping[str, float],
    yes_scales: Mapping[str, float],
    bucket_seconds: int,
    now: int,
    end_ts: Mapping[str, int | None],
    exit_buffer_seconds: int,
    min_abs_z: float = 0.75,
    max_hold_seconds: int = 24 * 3600,
    min_weight: float = 0.05,
    max_weight: float = 10.0,
) -> PairSignal | None:
    za, zb = fit.residual_z_a, fit.residual_z_b
    if za * zb >= 0.0 or min(abs(za), abs(zb)) < min_abs_z:
        return None
    if not (0.0 < fit.phi_a < 0.999 and 0.0 < fit.phi_b < 0.999):
        return None
    side_a = "NO" if za > 0.0 else "YES"
    side_b = "NO" if zb > 0.0 else "YES"
    try:
        exposure_a = price_factor_exposure(
            side_a, probabilities[fit.market_a], yes_scales[fit.market_a], fit.loading_a
        )
        exposure_b = price_factor_exposure(
            side_b, probabilities[fit.market_b], yes_scales[fit.market_b], fit.loading_b
        )
    except KeyError:
        return None
    if exposure_a * exposure_b >= 0.0 or abs(exposure_b) <= 1e-10:
        return None
    weight_b = abs(exposure_a / exposure_b)
    if not min_weight <= weight_b <= max_weight:
        return None
    ends = [end_ts.get(fit.market_a), end_ts.get(fit.market_b)]
    if any(end is None for end in ends):
        return None
    room = min(int(end) for end in ends if end is not None) - now - max(0, int(exit_buffer_seconds))
    if room < bucket_seconds:
        return None
    half_life = max(half_life_bars(fit.phi_a), half_life_bars(fit.phi_b))
    if not math.isfinite(half_life):
        return None
    hold = int(max(bucket_seconds, min(max_hold_seconds, room, 2.0 * half_life * bucket_seconds)))
    steps = hold / bucket_seconds
    return PairSignal(
        market_a=fit.market_a,
        market_b=fit.market_b,
        side_a=side_a,
        side_b=side_b,
        weight_a=1.0,
        weight_b=weight_b,
        hold_seconds=hold,
        residual_change_a=residual_change(
            fit.phi_a, fit.residual_a[-1], fit.residual_mean_a, steps
        ),
        residual_change_b=residual_change(
            fit.phi_b, fit.residual_b[-1], fit.residual_mean_b, steps
        ),
        factor_exposure_a=exposure_a,
        factor_exposure_b=exposure_b,
        pvalue=pvalue,
    )


def frechet_joint_bounds(p_a: float, p_b: float) -> tuple[float, float]:
    p_a, p_b = clamp(p_a, 0.0, 1.0), clamp(p_b, 0.0, 1.0)
    return max(0.0, p_a + p_b - 1.0), min(p_a, p_b)


def estimate_joint_distribution(
    observations: Sequence[tuple[bool, bool]],
    prior: float = 0.5,
) -> JointFillDistribution:
    counts = [float(prior)] * 4
    for a, b in observations:
        if a and b:
            counts[0] += 1.0
        elif a:
            counts[1] += 1.0
        elif b:
            counts[2] += 1.0
        else:
            counts[3] += 1.0
    total = sum(counts)
    return JointFillDistribution(
        both=counts[0] / total,
        a_only=counts[1] / total,
        b_only=counts[2] / total,
        none=counts[3] / total,
        observations=len(observations),
    )


def joint_execution_ev(
    distribution: JointFillDistribution,
    completed_pair_pnl: float,
    a_only_unwind_pnl: float,
    b_only_unwind_pnl: float,
    capital_latency_cost: float = 0.0,
) -> JointExecutionEV | None:
    if not distribution.valid():
        return None
    full = distribution.both * completed_pair_pnl
    a_only = distribution.a_only * a_only_unwind_pnl
    b_only = distribution.b_only * b_only_unwind_pnl
    cost = max(0.0, capital_latency_cost)
    return JointExecutionEV(full + a_only + b_only - cost, full, a_only, b_only, cost)
