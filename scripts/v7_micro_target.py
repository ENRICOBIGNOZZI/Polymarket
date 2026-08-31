#!/usr/bin/env python3
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


TARGET_SEMANTICS_VERSION = "executable_round_trip_net_edge_v1"


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _vwap(levels: Any, shares: float, *, buy: bool) -> float | None:
    if not isinstance(levels, list) or shares <= 0.0:
        return None
    parsed: list[tuple[float, float]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            continue
        price, size = finite(level[0]), finite(level[1], 0.0)
        if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
            parsed.append((price, size))
    parsed.sort(key=lambda row: row[0], reverse=not buy)
    remaining, notional = shares, 0.0
    for price, size in parsed:
        quantity = min(remaining, size)
        notional += quantity * price
        remaining -= quantity
        if remaining <= 1e-12:
            return notional / shares
    return None


def _fee_per_share(price: float, fee: Any) -> float | None:
    if not isinstance(fee, dict) or fee.get("authoritative") is not True:
        return None
    rate = finite(fee.get("rate"))
    exponent = finite(fee.get("exponent"))
    if (
        not 0.0 < price < 1.0 or not math.isfinite(rate)
        or not math.isfinite(exponent) or rate < 0.0 or exponent < 0.0
    ):
        return None
    return rate * (price * (1.0 - price)) ** exponent


def _side_label(
    origin: dict[str, Any], target: dict[str, Any], side: str,
) -> dict[str, float] | None:
    shares = finite(origin.get("label_probe_shares"), 0.0)
    entry = _vwap(origin.get(f"{side.lower()}_asks"), shares, buy=True)
    exit_price = _vwap(target.get(f"{side.lower()}_bids"), shares, buy=False)
    if entry is None or exit_price is None:
        return None
    fee = origin.get("fee")
    entry_fee, exit_fee = _fee_per_share(entry, fee), _fee_per_share(exit_price, fee)
    if entry_fee is None or exit_fee is None:
        return None
    slip = max(0.0, finite(origin.get("round_trip_slippage_bps"), 0.0)) / 10_000.0
    adverse = max(0.0, finite(origin.get("adverse_markout_bps"), 0.0)) / 10_000.0
    capital_bps_hour = max(
        0.0, finite(origin.get("capital_cost_bps_per_hour"), 0.0)) / 10_000.0
    horizon = max(0.0, finite(origin.get("label_horizon_seconds"), 0.0))
    capital = entry + entry_fee
    modeled_cost = (2.0 * slip + adverse) * capital \
        + capital_bps_hour * horizon / 3600.0 * capital
    pnl = exit_price - entry - entry_fee - exit_fee - modeled_cost
    explicit_cost = entry_fee + exit_fee + modeled_cost
    stressed_pnl = pnl - explicit_cost
    return {
        "entry_vwap": entry,
        "exit_vwap": exit_price,
        "entry_fee_per_share": entry_fee,
        "exit_fee_per_share": exit_fee,
        "modeled_cost_per_share": modeled_cost,
        "explicit_cost_per_share": explicit_cost,
        "net_pnl_per_share": pnl,
        "net_edge": pnl / max(capital, 1e-12),
        "cost_stress_2x_net_pnl_per_share": stressed_pnl,
        "cost_stress_2x_net_edge": stressed_pnl / max(capital, 1e-12),
    }


def label_matured_samples(
    samples: list[dict[str, Any]],
    *,
    now: int,
    horizon_seconds: int,
    max_target_staleness_seconds: int,
) -> dict[str, Any]:
    """Label the best executable post-cost action using only state by t+h."""
    if horizon_seconds <= 0:
        raise ValueError("horizon_seconds must be positive")
    if max_target_staleness_seconds < 0:
        raise ValueError("max_target_staleness_seconds must be nonnegative")

    by_market: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for row in samples:
        market_id = str(row.get("market_id") or "")
        ts = int(finite(row.get("ts"), 0.0))
        if (
            market_id and ts > 0
            and row.get("target_semantics_version") == TARGET_SEMANTICS_VERSION
        ):
            by_market[market_id].append((ts, row))
    for values in by_market.values():
        values.sort(key=lambda item: item[0])

    labeled = 0
    missing_observation = 0
    stale_observation = 0
    incompatible_target_semantics = 0
    unexecutable_round_trip = 0
    lags: list[int] = []
    for row in samples:
        if row.get("y") is not None:
            continue
        origin_ts = int(finite(row.get("ts"), 0.0))
        market_id = str(row.get("market_id") or "")
        if origin_ts <= 0 or not market_id:
            continue
        if row.get("target_semantics_version") != TARGET_SEMANTICS_VERSION:
            incompatible_target_semantics += 1
            continue
        target_ts = origin_ts + horizon_seconds
        if now < target_ts:
            continue

        chosen: tuple[int, dict[str, Any]] | None = None
        for obs_ts, observation in by_market.get(market_id, []):
            if obs_ts <= origin_ts:
                continue
            if obs_ts > target_ts:
                break
            chosen = (obs_ts, observation)
        if chosen is None:
            missing_observation += 1
            continue

        obs_ts, target = chosen
        lag = target_ts - obs_ts
        if lag > max_target_staleness_seconds:
            stale_observation += 1
            continue
        yes = _side_label(row, target, "YES")
        no = _side_label(row, target, "NO")
        if yes is None or no is None:
            unexecutable_round_trip += 1
            continue
        yes_edge, no_edge = yes["net_edge"], no["net_edge"]
        if yes_edge > 0.0 and yes_edge >= no_edge:
            target_value, action = yes_edge, "BUY_YES"
        elif no_edge > 0.0:
            target_value, action = -no_edge, "BUY_NO"
        else:
            target_value, action = 0.0, "NOTHING"
        row["y"] = target_value
        row["target_action"] = action
        row["yes_executable_net_edge"] = yes_edge
        row["no_executable_net_edge"] = no_edge
        row["yes_executable_net_pnl_per_share"] = yes["net_pnl_per_share"]
        row["no_executable_net_pnl_per_share"] = no["net_pnl_per_share"]
        row["yes_cost_stress_2x_net_edge"] = yes["cost_stress_2x_net_edge"]
        row["no_cost_stress_2x_net_edge"] = no["cost_stress_2x_net_edge"]
        row["target_execution"] = {"YES": yes, "NO": no}
        row["target_observation_ts"] = obs_ts
        row["target_staleness_seconds"] = lag
        labeled += 1
        lags.append(lag)

    return {
        "newly_labeled": labeled,
        "missing_pre_horizon_observation": missing_observation,
        "stale_pre_horizon_observation": stale_observation,
        "incompatible_target_semantics": incompatible_target_semantics,
        "unexecutable_round_trip": unexecutable_round_trip,
        "target_semantics_version": TARGET_SEMANTICS_VERSION,
        "max_target_staleness_seconds": max(lags) if lags else None,
        "mean_target_staleness_seconds": (sum(lags) / len(lags)) if lags else None,
    }


def label_matured_horizon_probes(
    samples: list[dict[str, Any]], *, now: int, horizons_seconds: tuple[int, ...],
    max_target_staleness_seconds: int,
) -> dict[str, Any]:
    """Attach causal, execution-complete research labels at multiple horizons.

    These labels never authorize trading and never alter the frozen 30-second
    champion target. They reveal whether the missing Micro Taker economics are
    horizon-specific before a new model specification is selected.
    """
    horizons = tuple(sorted({int(value) for value in horizons_seconds if int(value) > 0}))
    if not horizons:
        return {"horizons": {}, "target_semantics_version": TARGET_SEMANTICS_VERSION}
    if max_target_staleness_seconds < 0:
        raise ValueError("max_target_staleness_seconds must be nonnegative")

    by_market: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for row in samples:
        market_id = str(row.get("market_id") or "")
        ts = int(finite(row.get("ts"), 0.0))
        if market_id and ts > 0 and row.get(
            "target_semantics_version") == TARGET_SEMANTICS_VERSION:
            by_market[market_id].append((ts, row))
    for values in by_market.values():
        values.sort(key=lambda item: item[0])

    diagnostics = {
        str(horizon): {
            "newly_labeled": 0,
            "primary_target_reused": 0,
            "missing_pre_horizon_observation": 0,
            "stale_pre_horizon_observation": 0,
            "unexecutable_round_trip": 0,
        }
        for horizon in horizons
    }
    for row in samples:
        origin_ts = int(finite(row.get("ts"), 0.0))
        market_id = str(row.get("market_id") or "")
        if (
            origin_ts <= 0 or not market_id
            or row.get("target_semantics_version") != TARGET_SEMANTICS_VERSION
        ):
            continue
        targets = row.get("research_horizon_targets")
        if not isinstance(targets, dict):
            targets = {}
        for horizon in horizons:
            key = str(horizon)
            if isinstance(targets.get(key), dict):
                continue
            primary = row.get("target_execution")
            if (
                horizon == int(finite(row.get("label_horizon_seconds"), 0.0))
                and isinstance(primary, dict)
                and isinstance(primary.get("YES"), dict)
                and isinstance(primary.get("NO"), dict)
            ):
                # The primary 30-second target already contains the identical
                # execution payload. Count it in diagnostics without copying
                # tens of thousands of dictionaries back into durable state.
                diagnostics[key]["primary_target_reused"] += 1
                continue
            if now < origin_ts + horizon:
                continue
            target_ts = origin_ts + horizon
            chosen: tuple[int, dict[str, Any]] | None = None
            for obs_ts, observation in by_market.get(market_id, []):
                if obs_ts <= origin_ts:
                    continue
                if obs_ts > target_ts:
                    break
                chosen = (obs_ts, observation)
            if chosen is None:
                diagnostics[key]["missing_pre_horizon_observation"] += 1
                continue
            obs_ts, target = chosen
            staleness = target_ts - obs_ts
            if staleness > max_target_staleness_seconds:
                diagnostics[key]["stale_pre_horizon_observation"] += 1
                continue
            yes = _side_label(row, target, "YES")
            no = _side_label(row, target, "NO")
            if yes is None or no is None:
                diagnostics[key]["unexecutable_round_trip"] += 1
                continue
            yes_edge, no_edge = yes["net_edge"], no["net_edge"]
            action = (
                "BUY_YES" if yes_edge > 0.0 and yes_edge >= no_edge else
                "BUY_NO" if no_edge > 0.0 else "NOTHING"
            )
            targets[key] = {
                "horizon_seconds": horizon,
                "target_action": action,
                "target_observation_ts": obs_ts,
                "target_staleness_seconds": staleness,
                "YES": yes,
                "NO": no,
            }
            row["research_horizon_targets"] = targets
            diagnostics[key]["newly_labeled"] += 1
    return {
        "horizons": diagnostics,
        "target_semantics_version": TARGET_SEMANTICS_VERSION,
        "execution_authority": "RESEARCH_ONLY_ZERO_AUTHORITY",
    }
