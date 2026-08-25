#!/usr/bin/env python3
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def label_matured_samples(
    samples: list[dict[str, Any]],
    *,
    now: int,
    horizon_seconds: int,
    max_target_staleness_seconds: int,
) -> dict[str, Any]:
    """Label forecasts with the last already-observed midpoint at or before t+h.

    The decision clock is local observation time (`ts`).  A target observation
    must be strictly after the forecast origin and no later than the requested
    horizon.  We never borrow the first observation after t+h.  If the latest
    pre-horizon observation is too stale, the forecast remains unlabeled.
    """
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    if max_target_staleness_seconds < 0:
        raise ValueError("max_target_staleness_seconds must be nonnegative")

    by_market: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in samples:
        market_id = str(row.get("market_id") or "")
        ts = int(finite(row.get("ts"), 0.0))
        mid = finite(row.get("mid"))
        if market_id and ts > 0 and math.isfinite(mid):
            by_market[market_id].append((ts, mid))
    for values in by_market.values():
        values.sort(key=lambda x: x[0])

    labeled = 0
    missing_observation = 0
    stale_observation = 0
    lags: list[int] = []
    for row in samples:
        if row.get("y") is not None:
            continue
        origin_ts = int(finite(row.get("ts"), 0.0))
        market_id = str(row.get("market_id") or "")
        origin_mid = finite(row.get("mid"))
        if origin_ts <= 0 or not market_id or not math.isfinite(origin_mid):
            continue
        target_ts = origin_ts + horizon_seconds
        if now < target_ts:
            continue

        chosen: tuple[int, float] | None = None
        for obs_ts, obs_mid in by_market.get(market_id, []):
            if obs_ts <= origin_ts:
                continue
            if obs_ts > target_ts:
                break
            chosen = (obs_ts, obs_mid)
        if chosen is None:
            missing_observation += 1
            continue

        obs_ts, obs_mid = chosen
        lag = target_ts - obs_ts
        if lag > max_target_staleness_seconds:
            stale_observation += 1
            continue
        row["y"] = obs_mid - origin_mid
        row["target_observation_ts"] = obs_ts
        row["target_staleness_seconds"] = lag
        labeled += 1
        lags.append(lag)

    return {
        "newly_labeled": labeled,
        "missing_pre_horizon_observation": missing_observation,
        "stale_pre_horizon_observation": stale_observation,
        "max_target_staleness_seconds": max(lags) if lags else None,
        "mean_target_staleness_seconds": (sum(lags) / len(lags)) if lags else None,
    }
