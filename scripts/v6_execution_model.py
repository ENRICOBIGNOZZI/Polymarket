#!/usr/bin/env python3
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def finite(x: object, default: float = math.nan) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) else default


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    """Protocol fee per share using the market-specific Polymarket descriptor."""
    if not 0.0 < price < 1.0 or rate <= 0.0:
        return 0.0
    return rate * (price * (1.0 - price)) ** max(0.0, exponent)


@dataclass(frozen=True)
class WalkResult:
    shares: float
    vwap: float
    cash: float
    worst_price: float
    depth_complete: bool


def walk_levels(levels: Iterable[tuple[float, float]], shares: float, *, buy: bool) -> WalkResult:
    """Walk executable depth without inventing unobserved liquidity."""
    target = max(0.0, finite(shares, 0.0))
    if target <= 0.0:
        return WalkResult(0.0, 0.0, 0.0, math.nan, True)
    clean = [(finite(p), max(0.0, finite(q, 0.0))) for p, q in levels]
    clean = [(p, q) for p, q in clean if math.isfinite(p) and 0.0 < p < 1.0 and q > 0.0]
    clean.sort(key=lambda x: x[0], reverse=not buy)
    remaining = target
    cash = 0.0
    done = 0.0
    worst = math.nan
    for price, size in clean:
        take = min(remaining, size)
        if take <= 0.0:
            continue
        cash += take * price
        done += take
        remaining -= take
        worst = price
        if remaining <= 1e-12:
            break
    return WalkResult(done, cash / done if done > 0.0 else math.nan, cash, worst, remaining <= 1e-9)


def weighted_depth(levels: Sequence[tuple[float, float]], *, bid_side: bool, tick: float, n: int = 5) -> float:
    clean = [(finite(p), max(0.0, finite(q, 0.0))) for p, q in levels]
    clean = [(p, q) for p, q in clean if math.isfinite(p) and 0.0 < p < 1.0 and q > 0.0]
    if not clean:
        return 0.0
    clean.sort(key=lambda x: x[0], reverse=bid_side)
    best = clean[0][0]
    scale = max(1e-5, 3.0 * max(1e-6, finite(tick, 0.01)))
    return sum(q * math.exp(-abs(p - best) / scale) for p, q in clean[: max(1, n)])


def queue_fill_probability(
    *,
    queue_ahead: float,
    order_shares: float,
    contra_flow_shares_per_second: float,
    ttl_seconds: float,
    cancellation_share: float = 0.0,
    queue_reset_probability: float = 0.0,
) -> float:
    """Conservative passive fill-hazard proxy."""
    q = max(0.0, finite(queue_ahead, 0.0))
    own = max(1e-9, finite(order_shares, 0.0))
    flow = max(0.0, finite(contra_flow_shares_per_second, 0.0))
    ttl = max(0.0, finite(ttl_seconds, 0.0))
    cancel = clamp(finite(cancellation_share, 0.0), 0.0, 0.95)
    reset = clamp(finite(queue_reset_probability, 0.0), 0.0, 1.0)
    effective_queue = q * (1.0 - cancel) + 0.5 * own
    expected_flow = flow * ttl
    if expected_flow <= 0.0:
        return 0.0
    hazard = expected_flow / max(effective_queue, 1e-9)
    probability = (1.0 - math.exp(-hazard)) * (1.0 - 0.75 * reset)
    return clamp(probability, 0.0, 0.995)


def state_slippage_bps(
    *,
    base_bps: float,
    spread: float,
    short_vol: float,
    participation: float,
    liquidity_score: float,
    max_bps: float = 150.0,
) -> float:
    """Residual slippage beyond explicit displayed-depth walking."""
    base = max(0.0, finite(base_bps, 0.0))
    spr = max(0.0, finite(spread, 0.0))
    vol = max(0.0, finite(short_vol, 0.0))
    part = clamp(finite(participation, 0.0), 0.0, 5.0)
    liq = clamp(finite(liquidity_score, 0.0), 0.0, 1.0)
    penalty = 0.08 * spr * 10000.0 + 0.20 * vol * 10000.0
    penalty += 8.0 * math.sqrt(part) * (1.25 - liq)
    return clamp(base + penalty, 0.0, max(0.0, max_bps))


def adverse_selection_penalty(
    *,
    spread: float,
    imbalance_against_us: float,
    short_vol: float,
    fill_probability: float,
    coefficient: float = 1.0,
) -> float:
    """Per-share markout penalty conditional on a passive fill."""
    spr = max(0.0, finite(spread, 0.0))
    imb = clamp(abs(finite(imbalance_against_us, 0.0)), 0.0, 1.0)
    vol = max(0.0, finite(short_vol, 0.0))
    pfill = clamp(finite(fill_probability, 0.0), 0.0, 1.0)
    coeff = max(0.0, finite(coefficient, 1.0))
    return coeff * (0.10 * spr + 0.35 * spr * imb + 0.75 * vol * pfill)


@dataclass(frozen=True)
class TakerCost:
    entry_vwap: float
    fee_per_share: float
    residual_slippage_per_share: float
    all_in_price: float
    participation: float
    depth_complete: bool


def taker_cost(
    *,
    asks: Sequence[tuple[float, float]],
    shares: float,
    fee_rate: float,
    fee_exponent: float,
    base_slippage_bps: float,
    spread: float,
    short_vol: float,
    liquidity_score: float,
) -> TakerCost:
    walk = walk_levels(asks, shares, buy=True)
    if not walk.depth_complete or not math.isfinite(walk.vwap):
        return TakerCost(math.nan, math.nan, math.nan, math.inf, math.inf, False)
    touch_shares = max(1e-9, sum(max(0.0, q) for _, q in asks[:1]))
    participation = max(0.0, shares / touch_shares)
    slip_bps = state_slippage_bps(
        base_bps=base_slippage_bps,
        spread=spread,
        short_vol=short_vol,
        participation=participation,
        liquidity_score=liquidity_score,
    )
    residual_slip = walk.vwap * slip_bps / 10000.0
    px = clamp(walk.vwap + residual_slip, 1e-6, 0.999999)
    fee = fee_per_share(px, fee_rate, fee_exponent)
    return TakerCost(walk.vwap, fee, residual_slip, px + fee, participation, True)


def robust_edge_lcb(
    *,
    fair_probability: float,
    all_in_entry_price: float,
    prediction_sigma: float,
    uncertainty_z: float,
    model_risk_penalty: float = 0.0,
) -> float:
    fair = clamp(finite(fair_probability, 0.5), 0.0, 1.0)
    entry = finite(all_in_entry_price, math.inf)
    sigma = max(0.0, finite(prediction_sigma, 0.0))
    z = max(0.0, finite(uncertainty_z, 0.0))
    risk = max(0.0, finite(model_risk_penalty, 0.0))
    return fair - entry - z * sigma - risk


def mean_variance_notional(
    *,
    edge: float,
    prediction_sigma: float,
    max_notional: float,
    equity: float,
    risk_budget_fraction: float,
    min_fraction: float = 0.10,
) -> float:
    """Bounded short-horizon sizing; intentionally not binary Kelly."""
    e = max(0.0, finite(edge, 0.0))
    sigma = max(1e-6, finite(prediction_sigma, 0.0))
    cap = max(0.0, min(finite(max_notional, 0.0), finite(equity, 0.0)))
    budget = max(0.0, finite(risk_budget_fraction, 0.0)) * max(0.0, finite(equity, 0.0))
    if e <= 0.0 or cap <= 0.0 or budget <= 0.0:
        return 0.0
    information_ratio = e / sigma
    scale = clamp(information_ratio / 2.0, max(0.0, min_fraction), 1.0)
    risk_cap = budget / sigma
    return max(0.0, min(cap * scale, risk_cap, cap))
