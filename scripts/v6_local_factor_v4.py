#!/usr/bin/env python3
from __future__ import annotations

import math
import statistics
from typing import Any

try:
    import v6_local_factor_v3 as base
except ModuleNotFoundError:
    from scripts import v6_local_factor_v3 as base

BASE_BUILD_PAIR = base.build_pair


def _robust_scale(values: list[float]) -> tuple[float, float]:
    med = statistics.median(values)
    mad = statistics.median(abs(x - med) for x in values)
    scale = max(1e-6, 1.4826 * mad)
    return med, scale


def _causal_dynamic_residual(target: list[float], peers: list[list[float]], half_life: float = 48.0) -> tuple[list[float], list[float]]:
    """Leave-one-out factor with causal EW loading estimated from prior rows only."""
    n = len(target)
    factor = [statistics.median(peer[t] for peer in peers) for t in range(n)]
    lam = math.exp(-math.log(2.0) / max(4.0, half_life))
    sw = sx = sf = sff = sxf = 0.0
    residual: list[float] = []
    loadings: list[float] = []
    static_loading = 0.0
    for t in range(n):
        x = target[t]
        f = factor[t]
        if sw >= 8.0:
            cov = sxf - sx * sf / sw
            varf = sff - sf * sf / sw
            dynamic = cov / varf if varf > 1e-8 else static_loading
            shrink = min(1.0, sw / 32.0)
            loading = shrink * dynamic + (1.0 - shrink) * static_loading
        else:
            loading = static_loading
        residual.append(x - loading * f)
        loadings.append(loading)
        sw = lam * sw + 1.0
        sx = lam * sx + x
        sf = lam * sf + f
        sff = lam * sff + f * f
        sxf = lam * sxf + x * f
        if sw >= 4.0:
            cov = sxf - sx * sf / sw
            varf = sff - sf * sf / sw
            if varf > 1e-8:
                static_loading = cov / varf
    return residual, loadings


def local_candidates(key: str, markets: list[Any], series: dict[str, dict[int, float]], min_common: int, min_z: float) -> list[Any]:
    usable = [m for m in markets if m.market_id in series and len(series[m.market_id]) >= min_common]
    if len(usable) < 3:
        return []
    common = set(series[usable[0].market_id])
    for market in usable[1:]:
        common &= set(series[market.market_id])
    times = sorted(common)
    if len(times) < min_common:
        return []

    normalized: dict[str, list[float]] = {}
    scales: dict[str, float] = {}
    for market in usable:
        values = [series[market.market_id][ts] for ts in times]
        center, scale = _robust_scale(values)
        normalized[market.market_id] = [max(-8.0, min(8.0, (x - center) / scale)) for x in values]
        scales[market.market_id] = scale

    output = []
    for market in usable:
        peers = [normalized[other.market_id] for other in usable if other.market_id != market.market_id]
        residual, loading_path = _causal_dynamic_residual(normalized[market.market_id], peers)
        warm = min(16, max(0, len(residual) // 4))
        residual = residual[warm:]
        loading_path = loading_path[warm:]
        if len(residual) < max(30, min_common // 2):
            continue
        phi, rmu, rsd = base.ar_phi(residual)
        if not (0.02 < phi < 0.999 and rsd > 1e-8):
            continue
        recent_loading = statistics.median(loading_path[-min(12, len(loading_path)):])
        if abs(recent_loading) < 0.05:
            continue
        loading_dispersion = statistics.median(abs(x - recent_loading) for x in loading_path[-min(24, len(loading_path)):])
        if loading_dispersion > max(0.5, abs(recent_loading)):
            continue
        residual_z = (residual[-1] - rmu) / rsd
        if abs(residual_z) < min_z:
            continue
        pvalue, tstat = base.unit_root_block_pvalue(
            residual,
            20260825 + sum(ord(c) for c in "v4" + key + market.market_id),
        )
        output.append(
            base.base.Candidate(
                key,
                market,
                residual_z,
                phi,
                tstat,
                pvalue,
                recent_loading,
                scales[market.market_id],
                (phi - 1.0) * (residual[-1] - rmu),
                len(residual),
            )
        )
    return output


def build_pair(*args, **kwargs):
    rows, reason = BASE_BUILD_PAIR(*args, **kwargs)
    if not rows:
        return rows, reason
    gamma = str(kwargs.get("gamma") or "")
    now = int(kwargs.get("now") or 0)
    exit_buffer = int(kwargs.get("exit_buffer_seconds") or 0)
    cache = kwargs.get("cache") if isinstance(kwargs.get("cache"), dict) else {}
    expiries = []
    for row in rows:
        raw = base.raw_market(gamma, str(row.get("market_id") or ""), cache)
        if raw is None:
            return [], "ttr_market_missing"
        ts = base.market_end_ts(raw)
        if ts is None:
            return [], "ttr_missing"
        expiries.append(int(ts))
    hold_deadline = max(int(base.finite(row.get("hold_deadline_ts"), 0.0)) for row in rows)
    if hold_deadline <= now:
        return [], "ttr_invalid"
    if any(hold_deadline > ts - exit_buffer for ts in expiries):
        return [], "ttr_invalid"
    return rows, reason


def main() -> int:
    base.local_candidates = local_candidates
    base.build_pair = build_pair
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
