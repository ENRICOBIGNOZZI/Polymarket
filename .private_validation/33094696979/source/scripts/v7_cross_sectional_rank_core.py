#!/usr/bin/env python3
"""Causal cross-sectional ranking core for the canonical V7 paper research path.

This module intentionally separates prediction from execution.  It predicts
cross-sectional *fixed-horizon logit markouts*, never terminal event
probabilities.  Historical evaluation is purged/embargoed and reports ranking
statistics only.  Executable candidate selection is a separate, fail-closed
step that requires authoritative fee metadata and fresh books.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Sequence

FEATURE_NAMES = (
    "mom_1",
    "mom_2",
    "mom_4",
    "mom_12",
    "mean_gap_4",
    "mean_gap_12",
    "accel_4",
    "vol_4",
    "vol_12",
    "level_abs",
)


def clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def logistic(z: float) -> float:
    if z >= 0.0:
        e = math.exp(-min(40.0, z))
        return 1.0 / (1.0 + e)
    e = math.exp(max(-40.0, z))
    return e / (1.0 + e)


def logit(p: float) -> float:
    p = clamp(float(p), 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def robust_z(values: Sequence[float], clip: float = 5.0) -> list[float]:
    if not values:
        return []
    center = median(values)
    mad = median([abs(x - center) for x in values])
    scale = max(1e-6, 1.4826 * mad)
    return [clamp((x - center) / scale, -clip, clip) for x in values]


def stdev(xs: Sequence[float]) -> float:
    return statistics.stdev(xs) if len(xs) >= 2 else 0.0


@dataclass(frozen=True)
class MarketMeta:
    market_id: str
    event_id: str
    group: str = "all"


@dataclass(frozen=True)
class TrainingRow:
    ts: int
    label_ts: int
    market_id: str
    event_id: str
    group: str
    probability: float
    features: tuple[float, ...]
    target_logit: float


@dataclass(frozen=True)
class ScoreRow:
    ts: int
    market_id: str
    event_id: str
    group: str
    probability: float
    features: tuple[float, ...]
    predicted_logit_move: float
    sigma_logit: float


@dataclass(frozen=True)
class RidgeFit:
    beta: tuple[float, ...]
    residual_sigma: float
    n_rows: int
    n_cross_sections: int
    train_start_ts: int
    train_end_ts: int


@dataclass(frozen=True)
class BookEconomics:
    market_id: str
    event_id: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    liquidity: float
    fee_rate: float
    fee_exponent: float = 1.0
    taker_only: bool = True
    authoritative_fee: bool = False
    received_ts: int = 0


@dataclass(frozen=True)
class ExecutableCandidate:
    market_id: str
    event_id: str
    side: str
    horizon_seconds: int
    predicted_logit_move: float
    predicted_probability: float
    entry_price: float
    expected_exit_bid: float
    gross_markout: float
    fees: float
    slippage: float
    capital_cost: float
    adverse_penalty: float
    net_edge: float
    uncertainty_probability: float
    economic_score: float
    max_notional: float = 0.0


def _lag_value(series: Mapping[int, float], ts: int, steps: int, bucket_seconds: int) -> float | None:
    return series.get(ts - steps * bucket_seconds)


def raw_features(series: Mapping[int, float], ts: int, bucket_seconds: int) -> tuple[float, ...] | None:
    p0 = series.get(ts)
    if p0 is None or not 0.0 < p0 < 1.0:
        return None
    lag_p: dict[int, float] = {}
    for k in (1, 2, 4, 12):
        value = _lag_value(series, ts, k, bucket_seconds)
        if value is None or not 0.0 < value < 1.0:
            return None
        lag_p[k] = value
    z0 = logit(p0)
    z1, z2, z4, z12 = (logit(lag_p[k]) for k in (1, 2, 4, 12))
    recent4: list[float] = []
    recent12: list[float] = []
    for k in range(13):
        p = _lag_value(series, ts, k, bucket_seconds)
        if p is None or not 0.0 < p < 1.0:
            return None
        z = logit(p)
        if k <= 4:
            recent4.append(z)
        recent12.append(z)
    diffs4 = [recent4[i] - recent4[i + 1] for i in range(len(recent4) - 1)]
    diffs12 = [recent12[i] - recent12[i + 1] for i in range(len(recent12) - 1)]
    mean4 = statistics.fmean(recent4[1:])
    mean12 = statistics.fmean(recent12[1:])
    return (
        z0 - z1,
        z0 - z2,
        z0 - z4,
        z0 - z12,
        z0 - mean4,
        z0 - mean12,
        (z0 - z2) - (z2 - z4),
        stdev(diffs4),
        stdev(diffs12),
        abs(z0),
    )


def _normalize_cross_section(raw: list[tuple[MarketMeta, float, tuple[float, ...]]]) -> list[tuple[MarketMeta, float, tuple[float, ...]]]:
    if not raw:
        return []
    cols = list(zip(*(x[2] for x in raw)))
    normalized_cols = [robust_z(list(col)) for col in cols]
    return [
        (meta, p, tuple(col[i] for col in normalized_cols))
        for i, (meta, p, _features) in enumerate(raw)
    ]


def score_snapshot(
    histories: Mapping[str, Mapping[int, float]],
    metadata: Mapping[str, MarketMeta],
    ts: int,
    bucket_seconds: int,
    min_cross_section: int = 10,
) -> list[tuple[MarketMeta, float, tuple[float, ...]]]:
    raw: list[tuple[MarketMeta, float, tuple[float, ...]]] = []
    for market_id, series in histories.items():
        meta = metadata.get(market_id)
        if meta is None:
            continue
        features = raw_features(series, ts, bucket_seconds)
        p = series.get(ts)
        if features is not None and p is not None:
            raw.append((meta, p, features))
    return _normalize_cross_section(raw) if len(raw) >= min_cross_section else []


def target_residuals(
    items: list[tuple[MarketMeta, float]],
    group_weight: float,
    min_group_size: int,
) -> dict[str, float]:
    if not items:
        return {}
    global_med = median([x[1] for x in items])
    groups: dict[str, list[float]] = {}
    for meta, move in items:
        groups.setdefault(meta.group, []).append(move)
    group_med = {g: median(xs) for g, xs in groups.items() if len(xs) >= min_group_size}
    w = clamp(group_weight, 0.0, 1.0)
    out: dict[str, float] = {}
    for meta, move in items:
        baseline = global_med
        if meta.group in group_med:
            baseline = (1.0 - w) * global_med + w * group_med[meta.group]
        out[meta.market_id] = move - baseline
    return out


def build_training_rows(
    histories: Mapping[str, Mapping[int, float]],
    metadata: Mapping[str, MarketMeta],
    bucket_seconds: int,
    horizon_steps: int,
    min_cross_section: int = 10,
    group_weight: float = 0.5,
    min_group_size: int = 5,
) -> list[TrainingRow]:
    all_times = sorted({t for series in histories.values() for t in series})
    horizon_seconds = horizon_steps * bucket_seconds
    rows: list[TrainingRow] = []
    for ts in all_times:
        snapshot = score_snapshot(histories, metadata, ts, bucket_seconds, min_cross_section)
        if not snapshot:
            continue
        moves: list[tuple[MarketMeta, float]] = []
        for meta, p, _features in snapshot:
            future = histories[meta.market_id].get(ts + horizon_seconds)
            if future is not None and 0.0 < future < 1.0:
                moves.append((meta, logit(future) - logit(p)))
        if len(moves) < min_cross_section:
            continue
        targets = target_residuals(moves, group_weight, min_group_size)
        for meta, p, features in snapshot:
            if meta.market_id in targets:
                rows.append(
                    TrainingRow(
                        ts=ts,
                        label_ts=ts + horizon_seconds,
                        market_id=meta.market_id,
                        event_id=meta.event_id,
                        group=meta.group,
                        probability=p,
                        features=features,
                        target_logit=targets[meta.market_id],
                    )
                )
    return rows


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    aug = [list(a[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("singular ridge system")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [x / scale for x in aug[col]]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            if factor:
                aug[r] = [x - factor * y for x, y in zip(aug[r], aug[col])]
    return [aug[i][-1] for i in range(n)]


def fit_ridge(
    rows: Sequence[TrainingRow],
    asof_ts: int,
    window_seconds: int,
    embargo_seconds: int,
    ridge: float = 0.05,
    half_life_seconds: int = 7 * 86400,
    min_rows: int = 100,
    min_cross_sections: int = 20,
) -> RidgeFit | None:
    eligible = [
        r for r in rows
        if r.label_ts <= asof_ts - embargo_seconds
        and r.ts >= asof_ts - window_seconds
    ]
    sections = sorted({r.ts for r in eligible})
    if len(eligible) < min_rows or len(sections) < min_cross_sections:
        return None
    p = len(FEATURE_NAMES)
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    weights: list[float] = []
    for r in eligible:
        age = max(0, asof_ts - r.label_ts)
        weight = 0.5 ** (age / max(1.0, float(half_life_seconds)))
        weights.append(weight)
        for j in range(p):
            xty[j] += weight * r.features[j] * r.target_logit
            for k in range(j, p):
                xtx[j][k] += weight * r.features[j] * r.features[k]
    for j in range(p):
        for k in range(j):
            xtx[j][k] = xtx[k][j]
    wsum = sum(weights)
    penalty = max(1e-8, ridge) * max(1.0, wsum)
    for j in range(p):
        xtx[j][j] += penalty
    beta = _solve_linear(xtx, xty)
    residuals = [r.target_logit - sum(b * x for b, x in zip(beta, r.features)) for r in eligible]
    rmse = math.sqrt(sum(w * e * e for w, e in zip(weights, residuals)) / max(1e-12, wsum))
    med = median(residuals)
    mad = median([abs(e - med) for e in residuals])
    sigma = max(1e-5, 0.5 * rmse + 0.5 * 1.4826 * mad)
    return RidgeFit(
        beta=tuple(beta),
        residual_sigma=sigma,
        n_rows=len(eligible),
        n_cross_sections=len(sections),
        train_start_ts=min(r.ts for r in eligible),
        train_end_ts=max(r.label_ts for r in eligible),
    )


def apply_fit(snapshot: Sequence[tuple[MarketMeta, float, tuple[float, ...]]], fit: RidgeFit, ts: int) -> list[ScoreRow]:
    return [
        ScoreRow(
            ts=ts,
            market_id=meta.market_id,
            event_id=meta.event_id,
            group=meta.group,
            probability=p,
            features=features,
            predicted_logit_move=sum(b * x for b, x in zip(fit.beta, features)),
            sigma_logit=fit.residual_sigma,
        )
        for meta, p, features in snapshot
    ]


def _rankdata(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = 0.5 * ((i + 1) + j)
        for k in range(i, j):
            ranks[order[k]] = rank
        i = j
    return ranks


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    mx, my = statistics.fmean(x), statistics.fmean(y)
    xx = sum((a - mx) ** 2 for a in x)
    yy = sum((b - my) ** 2 for b in y)
    if xx <= 1e-12 or yy <= 1e-12:
        return 0.0
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(xx * yy)


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    return pearson(_rankdata(x), _rankdata(y))


def walk_forward_evaluate(
    rows: Sequence[TrainingRow],
    bucket_seconds: int,
    horizon_steps: int,
    window_seconds: int,
    embargo_steps: int = 1,
    ridge: float = 0.05,
    half_life_seconds: int = 7 * 86400,
    min_train_rows: int = 100,
    min_train_cross_sections: int = 20,
    tail_fraction: float = 0.2,
) -> dict[str, object]:
    by_ts: dict[int, list[TrainingRow]] = {}
    for r in rows:
        by_ts.setdefault(r.ts, []).append(r)
    ics: list[float] = []
    spreads: list[float] = []
    hits: list[int] = []
    selected_sets: list[set[str]] = []
    deciles: dict[int, list[float]] = {i: [] for i in range(10)}
    predictions = 0
    fits = 0
    for ts in sorted(by_ts):
        fit = fit_ridge(
            rows,
            asof_ts=ts,
            window_seconds=window_seconds,
            embargo_seconds=embargo_steps * bucket_seconds,
            ridge=ridge,
            half_life_seconds=half_life_seconds,
            min_rows=min_train_rows,
            min_cross_sections=min_train_cross_sections,
        )
        if fit is None:
            continue
        section = by_ts[ts]
        pred = [sum(b * x for b, x in zip(fit.beta, r.features)) for r in section]
        true = [r.target_logit for r in section]
        if len(section) < 5:
            continue
        fits += 1
        predictions += len(section)
        ics.append(spearman(pred, true))
        hits.extend(int((a > 0) == (b > 0)) for a, b in zip(pred, true) if abs(a) > 1e-12 and abs(b) > 1e-12)
        order = sorted(range(len(section)), key=lambda i: pred[i])
        n_tail = max(1, int(len(order) * tail_fraction))
        bottom, top = order[:n_tail], order[-n_tail:]
        spreads.append(statistics.fmean(true[i] for i in top) - statistics.fmean(true[i] for i in bottom))
        selected_sets.append({section[i].market_id for i in top + bottom})
        for pos, idx in enumerate(order):
            dec = min(9, int(10 * pos / max(1, len(order))))
            deciles[dec].append(true[idx])
    turnover: list[float] = []
    for a, b in zip(selected_sets, selected_sets[1:]):
        union = a | b
        turnover.append(1.0 - len(a & b) / max(1, len(union)))
    decile_means = [statistics.fmean(deciles[i]) if deciles[i] else 0.0 for i in range(10)]
    return {
        "horizon_steps": horizon_steps,
        "cross_sections": fits,
        "predictions": predictions,
        "mean_rank_ic": statistics.fmean(ics) if ics else 0.0,
        "median_rank_ic": median(ics),
        "positive_ic_fraction": sum(x > 0 for x in ics) / len(ics) if ics else 0.0,
        "mean_top_bottom_logit_spread": statistics.fmean(spreads) if spreads else 0.0,
        "median_top_bottom_logit_spread": median(spreads),
        "directional_hit_rate": sum(hits) / len(hits) if hits else 0.0,
        "mean_turnover": statistics.fmean(turnover) if turnover else 0.0,
        "decile_target_means": decile_means,
        "decile_monotonicity": pearson(list(range(10)), decile_means),
        "economic_pnl_validated": False,
    }


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not 0.0 < price < 1.0 or rate <= 0.0:
        return 0.0
    return rate * (price * (1.0 - price)) ** max(0.0, exponent)


def candidate_from_score(
    score: ScoreRow,
    book: BookEconomics,
    horizon_seconds: int,
    now: int,
    slippage_bps: float,
    capital_cost_bps_per_hour: float,
    adverse_penalty_bps: float,
    max_book_age_seconds: int,
    exit_spread_multiplier: float = 1.0,
) -> ExecutableCandidate | None:
    # Economic ranking fails closed until authoritative per-market fee metadata
    # and a fresh book are present.  Historical price predictability alone is not
    # permission to emit a live-paper order.
    if not book.authoritative_fee:
        return None
    if book.received_ts <= 0 or now - book.received_ts > max_book_age_seconds or book.received_ts > now + 5:
        return None
    if score.predicted_logit_move == 0.0:
        return None
    side = "YES" if score.predicted_logit_move > 0.0 else "NO"
    q_yes = logistic(logit(score.probability) + score.predicted_logit_move)
    q_side = q_yes if side == "YES" else 1.0 - q_yes
    bid = book.yes_bid if side == "YES" else book.no_bid
    ask = book.yes_ask if side == "YES" else book.no_ask
    if not (0.0 < bid < ask < 1.0):
        return None
    spread = ask - bid
    expected_exit_bid = clamp(q_side - 0.5 * exit_spread_multiplier * spread, 1e-6, 1.0 - 1e-6)
    entry_fee = fee_per_share(ask, book.fee_rate, book.fee_exponent)
    exit_fee = fee_per_share(expected_exit_bid, book.fee_rate, book.fee_exponent)
    slip = (ask + expected_exit_bid) * max(0.0, slippage_bps) / 10000.0
    capital_cost = max(0.0, capital_cost_bps_per_hour) / 10000.0 * (horizon_seconds / 3600.0)
    adverse = max(0.0, adverse_penalty_bps) / 10000.0
    gross = expected_exit_bid - ask
    fees = entry_fee + exit_fee
    net = gross - fees - slip - capital_cost - adverse
    hi = logistic(logit(score.probability) + score.predicted_logit_move + score.sigma_logit)
    lo = logistic(logit(score.probability) + score.predicted_logit_move - score.sigma_logit)
    sigma_p = max(1e-5, 0.5 * abs(hi - lo))
    economic_score = net / sigma_p / math.sqrt(max(1.0 / 12.0, horizon_seconds / 3600.0))
    return ExecutableCandidate(
        market_id=score.market_id,
        event_id=score.event_id,
        side=side,
        horizon_seconds=horizon_seconds,
        predicted_logit_move=score.predicted_logit_move,
        predicted_probability=q_yes,
        entry_price=ask,
        expected_exit_bid=expected_exit_bid,
        gross_markout=gross,
        fees=fees,
        slippage=slip,
        capital_cost=capital_cost,
        adverse_penalty=adverse,
        net_edge=net,
        uncertainty_probability=sigma_p,
        economic_score=economic_score,
    )


def select_candidates(
    scored: Sequence[ScoreRow],
    books: Mapping[str, BookEconomics],
    horizon_seconds: int,
    now: int,
    min_net_edge: float,
    max_positions_per_side: int,
    max_trade_usd: float,
    sleeve_budget_usd: float,
    min_liquidity: float = 2.0,
    max_spread: float = 0.25,
    slippage_bps: float = 5.0,
    capital_cost_bps_per_hour: float = 0.25,
    adverse_penalty_bps: float = 2.0,
    max_book_age_seconds: int = 30,
) -> list[ExecutableCandidate]:
    candidates: list[ExecutableCandidate] = []
    for score in scored:
        book = books.get(score.market_id)
        if book is None or book.liquidity < min_liquidity:
            continue
        side_spread = (book.yes_ask - book.yes_bid) if score.predicted_logit_move > 0 else (book.no_ask - book.no_bid)
        if side_spread > max_spread:
            continue
        cand = candidate_from_score(
            score,
            book,
            horizon_seconds,
            now,
            slippage_bps,
            capital_cost_bps_per_hour,
            adverse_penalty_bps,
            max_book_age_seconds,
        )
        if cand is not None and cand.net_edge >= min_net_edge and cand.economic_score > 0:
            candidates.append(cand)
    selected: list[ExecutableCandidate] = []
    used_events: set[str] = set()
    for side in ("YES", "NO"):
        side_candidates = sorted((c for c in candidates if c.side == side), key=lambda c: c.economic_score, reverse=True)
        chosen: list[ExecutableCandidate] = []
        for cand in side_candidates:
            if cand.event_id in used_events:
                continue
            chosen.append(cand)
            used_events.add(cand.event_id)
            if len(chosen) >= max_positions_per_side:
                break
        if chosen:
            side_budget = 0.5 * sleeve_budget_usd
            strengths = [max(1e-9, c.economic_score) for c in chosen]
            denominator = sum(strengths)
            for cand, strength in zip(chosen, strengths):
                notional = min(max_trade_usd, side_budget * strength / denominator)
                selected.append(ExecutableCandidate(**{**cand.__dict__, "max_notional": notional}))
    return sorted(selected, key=lambda c: c.economic_score, reverse=True)
