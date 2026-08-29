#!/usr/bin/env python3
from __future__ import annotations

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


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def norm(a: Sequence[float]) -> float:
    return math.sqrt(max(0.0, dot(a, a)))


def normalize(a: Sequence[float]) -> list[float] | None:
    value = norm(a)
    return [x / value for x in a] if value > 1e-12 else None


def matvec(a: Sequence[Sequence[float]], x: Sequence[float]) -> list[float]:
    return [dot(row, x) for row in a]


def orthogonalize(v: Sequence[float], basis: Sequence[Sequence[float]]) -> list[float]:
    out = list(v)
    for q in basis:
        projection = dot(out, q)
        out = [x - projection * y for x, y in zip(out, q)]
    return out


def top_eigenpairs(
    matrix: Sequence[Sequence[float]],
    max_components: int,
    max_iter: int = 500,
    tol: float = 1e-10,
) -> list[tuple[float, tuple[float, ...]]]:
    n = len(matrix)
    if n == 0 or any(len(row) != n for row in matrix):
        return []
    basis: list[tuple[float, ...]] = []
    output: list[tuple[float, tuple[float, ...]]] = []
    for component in range(min(max_components, n)):
        seed = [1.0 + ((i + 1) * (component + 3) % 7) / 10.0 for i in range(n)]
        seed = orthogonalize(seed, basis)
        v = normalize(seed)
        if v is None:
            break
        previous = 0.0
        for _ in range(max_iter):
            candidate = normalize(orthogonalize(matvec(matrix, v), basis))
            if candidate is None:
                break
            v = candidate
            eigenvalue = dot(v, matvec(matrix, v))
            if abs(eigenvalue - previous) <= tol * max(1.0, abs(eigenvalue)):
                break
            previous = eigenvalue
        eigenvalue = dot(v, matvec(matrix, v))
        if not math.isfinite(eigenvalue) or eigenvalue <= 1e-10:
            break
        vector = tuple(v)
        basis.append(vector)
        output.append((eigenvalue, vector))
    return output


def solve_linear(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    aug = [list(a[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("singular system")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [x / scale for x in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [x - factor * y for x, y in zip(aug[row], aug[col])]
    return [aug[i][-1] for i in range(n)]


def ridge_coefficients(x: Sequence[Sequence[float]], y: Sequence[float], ridge: float = 1e-4) -> tuple[float, ...] | None:
    if not x or len(x) != len(y):
        return None
    width = len(x[0])
    if width == 0:
        return None
    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for row, target in zip(x, y):
        for j in range(width):
            xty[j] += row[j] * target
            for k in range(j, width):
                xtx[j][k] += row[j] * row[k]
    for j in range(width):
        for k in range(j):
            xtx[j][k] = xtx[k][j]
        xtx[j][j] += max(1e-8, ridge) * len(x)
    try:
        return tuple(solve_linear(xtx, xty))
    except ValueError:
        return None


def longest_regular_suffix(times: Sequence[int], bucket_seconds: int) -> tuple[int, ...]:
    ordered = sorted(set(int(t) for t in times))
    if not ordered:
        return ()
    start = len(ordered) - 1
    while start > 0 and ordered[start] - ordered[start - 1] == bucket_seconds:
        start -= 1
    return tuple(ordered[start:])


@dataclass(frozen=True)
class RawPanel:
    times: tuple[int, ...]
    values: dict[str, tuple[float, ...]]


@dataclass(frozen=True)
class PcaTargetModel:
    target: str
    controls: tuple[str, ...]
    target_mean: float
    target_scale: float
    control_means: tuple[float, ...]
    control_scales: tuple[float, ...]
    eigenvalues: tuple[float, ...]
    eigenvectors: tuple[tuple[float, ...], ...]
    beta: tuple[float, ...]
    residual_mean: float
    residual_sd: float
    innovation_sd: float
    phi: float
    adf_t: float
    residual_last: float
    explained_variance: float
    training_points: int


@dataclass(frozen=True)
class PcaScore:
    target: str
    current_probability: float
    current_residual: float
    residual_z: float
    predicted_logit_move: float
    sigma_logit: float
    horizon_steps: int


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
    fee_exponent: float
    authoritative_fee: bool
    received_ts: int


@dataclass(frozen=True)
class SingleLegCandidate:
    market_id: str
    event_id: str
    side: str
    horizon_seconds: int
    entry_price: float
    expected_exit_bid: float
    predicted_logit_move: float
    predicted_yes_probability: float
    gross_markout: float
    fees: float
    slippage: float
    capital_cost: float
    adverse_penalty: float
    net_edge: float
    uncertainty_probability: float
    economic_score: float


def build_raw_panel(
    histories: Mapping[str, Mapping[int, float]],
    market_ids: Sequence[str],
    bucket_seconds: int,
    min_points: int,
) -> RawPanel | None:
    ids = [mid for mid in market_ids if mid in histories]
    if len(ids) < 3:
        return None
    common = set(histories[ids[0]])
    for mid in ids[1:]:
        common &= set(histories[mid])
    times = longest_regular_suffix(sorted(common), bucket_seconds)
    if len(times) < min_points:
        return None
    values = {mid: tuple(float(histories[mid][t]) for t in times) for mid in ids}
    if any(not all(math.isfinite(x) for x in row) for row in values.values()):
        return None
    return RawPanel(times, values)


def covariance_matrix(rows: Sequence[Sequence[float]]) -> list[list[float]]:
    size = len(rows)
    if size == 0:
        return []
    points = len(rows[0])
    return [[dot(rows[i], rows[j]) / max(1, points - 1) for j in range(size)] for i in range(size)]


def ar1_fit(levels: Sequence[float]) -> tuple[float, float, float, float]:
    if len(levels) < 12:
        return 1.0, statistics.fmean(levels) if levels else 0.0, stdev(levels), 0.0
    lag = list(levels[:-1])
    lead = list(levels[1:])
    lm, ym = statistics.fmean(lag), statistics.fmean(lead)
    sxx = sum((x - lm) ** 2 for x in lag)
    if sxx <= 1e-12:
        return 1.0, statistics.fmean(levels), stdev(levels), 0.0
    phi = sum((x - lm) * (y - ym) for x, y in zip(lag, lead)) / sxx
    intercept = ym - phi * lm
    mu = intercept / (1.0 - phi) if abs(1.0 - phi) > 1e-8 else statistics.fmean(levels)
    innovations = [lead[i] - (intercept + phi * lag[i]) for i in range(len(lag))]
    return phi, mu, stdev(levels), stdev(innovations)


def adf_t_stat(levels: Sequence[float]) -> float:
    if len(levels) < 12:
        return 0.0
    lag = list(levels[:-1])
    delta = [levels[i] - levels[i - 1] for i in range(1, len(levels))]
    xm, ym = statistics.fmean(lag), statistics.fmean(delta)
    sxx = sum((value - xm) ** 2 for value in lag)
    if sxx <= 1e-12:
        return 0.0
    gamma = sum((x - xm) * (y - ym) for x, y in zip(lag, delta)) / sxx
    alpha = ym - gamma * xm
    rss = sum((y - alpha - gamma * x) ** 2 for x, y in zip(lag, delta))
    se = math.sqrt(max(0.0, rss / max(1, len(lag) - 2)) / sxx)
    return gamma / se if se > 1e-12 else 0.0


def fit_target(
    panel: RawPanel,
    target: str,
    max_components: int = 3,
    explained_variance_threshold: float = 0.80,
    ridge: float = 1e-4,
) -> PcaTargetModel | None:
    if target not in panel.values:
        return None
    controls = tuple(sorted(mid for mid in panel.values if mid != target))
    if len(controls) < 2:
        return None
    target_raw = list(panel.values[target])
    target_mean = statistics.fmean(target_raw)
    target_scale = stdev(target_raw)
    if target_scale <= 1e-6:
        return None
    target_std = [(x - target_mean) / target_scale for x in target_raw]
    control_means: list[float] = []
    control_scales: list[float] = []
    control_std: list[list[float]] = []
    for mid in controls:
        values = list(panel.values[mid])
        mean = statistics.fmean(values)
        scale = stdev(values)
        if scale <= 1e-6:
            return None
        control_means.append(mean)
        control_scales.append(scale)
        control_std.append([(x - mean) / scale for x in values])
    covariance = covariance_matrix(control_std)
    eigenpairs = top_eigenpairs(covariance, max_components=max_components)
    if not eigenpairs:
        return None
    total_variance = max(1e-12, sum(max(0.0, covariance[i][i]) for i in range(len(covariance))))
    chosen: list[tuple[float, tuple[float, ...]]] = []
    cumulative = 0.0
    threshold = clamp(explained_variance_threshold, 0.0, 1.0)
    for value, vector in eigenpairs:
        chosen.append((value, vector))
        cumulative += max(0.0, value)
        if cumulative / total_variance >= threshold:
            break
    factors_by_time: list[list[float]] = []
    for timestamp_index in range(len(panel.times)):
        current_controls = [control_std[i][timestamp_index] for i in range(len(controls))]
        factors_by_time.append([dot(vector, current_controls) for _value, vector in chosen])
    beta = ridge_coefficients(factors_by_time, target_std, ridge=ridge)
    if beta is None:
        return None
    residual = [target_std[i] - dot(beta, factors_by_time[i]) for i in range(len(panel.times))]
    phi, residual_mean, residual_sd, innovation_sd = ar1_fit(residual)
    if residual_sd <= 1e-8:
        return None
    return PcaTargetModel(
        target=target,
        controls=controls,
        target_mean=target_mean,
        target_scale=target_scale,
        control_means=tuple(control_means),
        control_scales=tuple(control_scales),
        eigenvalues=tuple(value for value, _vector in chosen),
        eigenvectors=tuple(vector for _value, vector in chosen),
        beta=beta,
        residual_mean=residual_mean,
        residual_sd=residual_sd,
        innovation_sd=max(1e-6, innovation_sd),
        phi=phi,
        adf_t=adf_t_stat(residual),
        residual_last=residual[-1],
        explained_variance=min(1.0, cumulative / total_variance),
        training_points=len(panel.times),
    )


def score_current(model: PcaTargetModel, current_logits: Mapping[str, float], horizon_steps: int) -> PcaScore | None:
    if model.target not in current_logits or any(mid not in current_logits for mid in model.controls):
        return None
    controls_std = [
        (current_logits[mid] - mean) / scale
        for mid, mean, scale in zip(model.controls, model.control_means, model.control_scales)
    ]
    factors = [dot(vector, controls_std) for vector in model.eigenvectors]
    target_std = (current_logits[model.target] - model.target_mean) / model.target_scale
    residual = target_std - dot(model.beta, factors)
    if not 0.0 < model.phi < 0.999:
        return None
    steps = max(1, int(horizon_steps))
    residual_move = (model.phi ** steps - 1.0) * (residual - model.residual_mean)
    predicted_logit_move = residual_move * model.target_scale
    variance_multiplier = sum(model.phi ** (2 * j) for j in range(steps))
    sigma_logit = model.innovation_sd * math.sqrt(max(1.0, variance_multiplier)) * model.target_scale
    return PcaScore(
        target=model.target,
        current_probability=logistic(current_logits[model.target]),
        current_residual=residual,
        residual_z=(residual - model.residual_mean) / model.residual_sd,
        predicted_logit_move=predicted_logit_move,
        sigma_logit=max(1e-6, sigma_logit),
        horizon_steps=steps,
    )


def _block_indices(count: int, block: int, rng: random.Random) -> list[int]:
    out: list[int] = []
    while len(out) < count:
        start = rng.randrange(count)
        out.extend((start + j) % count for j in range(block))
    return out[:count]


def null_panel_bootstrap(panel: RawPanel, rng: random.Random) -> RawPanel | None:
    points = len(panel.times)
    if points < 12:
        return None
    mids = sorted(panel.values)
    differences = {
        mid: [panel.values[mid][i] - panel.values[mid][i - 1] for i in range(1, points)]
        for mid in mids
    }
    drift = {mid: statistics.fmean(differences[mid]) for mid in mids}
    centered = {mid: [x - drift[mid] for x in differences[mid]] for mid in mids}
    block = max(2, min(points - 1, int(round(math.sqrt(points - 1)))))
    indices = _block_indices(points - 1, block, rng)
    levels = {mid: [panel.values[mid][0]] for mid in mids}
    for index in indices:
        for mid in mids:
            levels[mid].append(levels[mid][-1] + drift[mid] + centered[mid][index])
    return RawPanel(panel.times, {mid: tuple(values) for mid, values in levels.items()})


def target_bootstrap_pvalue(
    panel: RawPanel,
    target: str,
    reps: int = 300,
    seed: int = 20260826,
    max_components: int = 3,
    explained_variance_threshold: float = 0.80,
) -> tuple[PcaTargetModel, float] | None:
    observed = fit_target(panel, target, max_components, explained_variance_threshold)
    if observed is None:
        return None
    total = max(50, int(reps))
    rng = random.Random(seed)
    left = 0
    for _ in range(total):
        boot = null_panel_bootstrap(panel, rng)
        if boot is None:
            continue
        model = fit_target(boot, target, max_components, explained_variance_threshold)
        if model is not None and model.adf_t <= observed.adf_t:
            left += 1
    return observed, (left + 1.0) / (total + 1.0)


def bh_selected(pvalues: Mapping[str, float], q: float) -> set[str]:
    ordered = sorted((p, key) for key, p in pvalues.items() if math.isfinite(p))
    cutoff = 0.0
    count = len(ordered)
    for i, (pvalue, _key) in enumerate(ordered, start=1):
        if pvalue <= clamp(q, 1e-6, 0.5) * i / max(1, count):
            cutoff = pvalue
    return {key for pvalue, key in ordered if cutoff > 0.0 and pvalue <= cutoff}


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    if not 0.0 < price < 1.0 or rate <= 0.0:
        return 0.0
    return rate * (price * (1.0 - price)) ** max(0.0, exponent)


def executable_candidate(
    score: PcaScore,
    book: BookEconomics,
    horizon_seconds: int,
    now: int,
    slippage_bps: float,
    capital_cost_bps_per_hour: float,
    adverse_penalty_bps: float,
    max_book_age_seconds: int,
) -> SingleLegCandidate | None:
    if not book.authoritative_fee:
        return None
    if book.received_ts <= 0 or now - book.received_ts > max_book_age_seconds or book.received_ts > now + 5:
        return None
    if score.predicted_logit_move == 0.0:
        return None
    side = "YES" if score.predicted_logit_move > 0.0 else "NO"
    bid, ask = (book.yes_bid, book.yes_ask) if side == "YES" else (book.no_bid, book.no_ask)
    if not 0.0 < bid < ask < 1.0:
        return None
    predicted_yes = logistic(logit(score.current_probability) + score.predicted_logit_move)
    predicted_side = predicted_yes if side == "YES" else 1.0 - predicted_yes
    expected_exit_bid = clamp(predicted_side - 0.5 * (ask - bid), 1e-6, 1.0 - 1e-6)
    entry_fee = fee_per_share(ask, book.fee_rate, book.fee_exponent)
    exit_fee = fee_per_share(expected_exit_bid, book.fee_rate, book.fee_exponent)
    slippage = (ask + expected_exit_bid) * max(0.0, slippage_bps) / 10000.0
    capital = max(0.0, capital_cost_bps_per_hour) / 10000.0 * (horizon_seconds / 3600.0)
    adverse = max(0.0, adverse_penalty_bps) / 10000.0
    gross = expected_exit_bid - ask
    net = gross - entry_fee - exit_fee - slippage - capital - adverse
    upper = logistic(logit(score.current_probability) + score.predicted_logit_move + score.sigma_logit)
    lower = logistic(logit(score.current_probability) + score.predicted_logit_move - score.sigma_logit)
    sigma_probability = max(1e-5, 0.5 * abs(upper - lower))
    return SingleLegCandidate(
        market_id=score.target,
        event_id=book.event_id,
        side=side,
        horizon_seconds=horizon_seconds,
        entry_price=ask,
        expected_exit_bid=expected_exit_bid,
        predicted_logit_move=score.predicted_logit_move,
        predicted_yes_probability=predicted_yes,
        gross_markout=gross,
        fees=entry_fee + exit_fee,
        slippage=slippage,
        capital_cost=capital,
        adverse_penalty=adverse,
        net_edge=net,
        uncertainty_probability=sigma_probability,
        economic_score=net / sigma_probability / math.sqrt(max(1.0 / 12.0, horizon_seconds / 3600.0)),
    )
