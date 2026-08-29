#!/usr/bin/env python3
"""Slow-path Polymarket reward/rebate market selector for V7 market making.

This module reads public, unauthenticated CLOB reward endpoints only.  It never
submits orders.  The fast quote path consumes the atomically published selection
snapshot; network/REST work is deliberately kept off the event-driven quote path.

Polymarket's liquidity-reward dollar allocation is relative to other makers, so
this selector records pool/configuration facts and a ranking score.  It does not
invent a guaranteed reward share when competition evidence is unavailable.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
from typing import Any, Callable


SELECTOR_STATUS_SCHEMA = "polymarket_v7_maker_selector_status_v1"
UNIVERSE_SCHEMA = "polymarket_v7_adaptive_universe_snapshot_v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def request_json(url: str, *, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-maker/1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class RewardPool:
    condition_id: str
    max_spread_cents: float
    min_size: float
    native_daily_rate: float
    sponsored_daily_rate: float
    total_daily_rate: float


@dataclass(frozen=True)
class RewardMarket:
    condition_id: str
    market_id: str
    event_id: str
    slug: str
    question: str
    yes_token: str
    no_token: str
    volume_24h: float
    market_competitiveness: float


@dataclass(frozen=True)
class MarketSelection:
    condition_id: str
    market_id: str
    event_id: str
    slug: str
    question: str
    yes_token: str
    no_token: str
    volume_24h: float
    market_competitiveness: float
    rewards_max_spread_cents: float
    rewards_min_size: float
    native_daily_rate: float
    sponsored_daily_rate: float
    total_daily_rate: float
    reward_intensity: float
    selection_score: float


def _pagination(root: Any) -> tuple[list[Any], str]:
    if not isinstance(root, dict):
        raise ValueError("reward_endpoint:not_object")
    data = root.get("data")
    if not isinstance(data, list):
        raise ValueError("reward_endpoint:missing_data")
    return data, str(root.get("next_cursor") or "")


def _bounded_timeout(deadline: float, request_timeout: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError("reward_selector_total_deadline_exceeded")
    return max(0.05, min(float(request_timeout), remaining))


def fetch_reward_pools(
    clob_url: str,
    *,
    deadline: float | None = None,
    request_timeout: float = 20.0,
    max_pages: int = 100,
    fetcher: Callable[..., Any] = request_json,
) -> dict[str, RewardPool]:
    out: dict[str, RewardPool] = {}
    cursor = ""
    seen: set[str] = set()
    deadline = time.monotonic() + 3600.0 if deadline is None else deadline
    for _ in range(max(1, int(max_pages))):
        url = clob_url.rstrip("/") + "/rewards/markets/current"
        if cursor:
            url += "?next_cursor=" + urllib.parse.quote(cursor, safe="")
        rows, next_cursor = _pagination(fetcher(url, timeout=_bounded_timeout(deadline, request_timeout)))
        for row in rows:
            if not isinstance(row, dict):
                continue
            condition = str(row.get("condition_id") or "")
            max_spread = finite(row.get("rewards_max_spread"))
            min_size = finite(row.get("rewards_min_size"))
            native = finite(row.get("native_daily_rate"), -1.0)
            sponsored = max(0.0, finite(row.get("sponsored_daily_rate"), 0.0))
            config_sum = 0.0
            cfg = row.get("rewards_config")
            if isinstance(cfg, list):
                for item in cfg:
                    if isinstance(item, dict):
                        config_sum += max(0.0, finite(item.get("rate_per_day")))
            if native < 0.0:
                native = config_sum
            total = finite(row.get("total_daily_rate"), -1.0)
            if total < 0.0:
                total = max(0.0, native) + sponsored
            if condition and max_spread > 0.0 and min_size > 0.0 and total > 0.0:
                out[condition] = RewardPool(
                    condition_id=condition,
                    max_spread_cents=max_spread,
                    min_size=min_size,
                    native_daily_rate=max(0.0, native),
                    sponsored_daily_rate=sponsored,
                    total_daily_rate=max(0.0, total),
                )
        if not next_cursor or next_cursor == "LTE=" or next_cursor == cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise RuntimeError("reward market pagination guard reached before exhaustion")
    return out


def fetch_reward_markets(
    clob_url: str,
    *,
    limit: int = 0,
    deadline: float | None = None,
    request_timeout: float = 20.0,
    max_pages: int = 200,
    fetcher: Callable[..., Any] = request_json,
) -> dict[str, RewardMarket]:
    """Fetch the full reward-eligible catalog; positive limit is test/diagnostic only."""
    out: dict[str, RewardMarket] = {}
    cursor = ""
    seen: set[str] = set()
    deadline = time.monotonic() + 3600.0 if deadline is None else deadline
    for _ in range(max(1, int(max_pages))):
        if limit > 0 and len(out) >= limit:
            break
        url = clob_url.rstrip("/") + "/rewards/markets/multi?limit=500"
        if cursor:
            url += "&next_cursor=" + urllib.parse.quote(cursor, safe="")
        rows, next_cursor = _pagination(fetcher(url, timeout=_bounded_timeout(deadline, request_timeout)))
        for row in rows:
            if not isinstance(row, dict):
                continue
            condition = str(row.get("condition_id") or "")
            yes_token = ""
            no_token = ""
            tokens = row.get("tokens")
            if isinstance(tokens, list):
                for token in tokens:
                    if not isinstance(token, dict):
                        continue
                    outcome = str(token.get("outcome") or "").strip().lower()
                    token_id = str(token.get("token_id") or "")
                    if outcome == "yes":
                        yes_token = token_id
                    elif outcome == "no":
                        no_token = token_id
            if condition and yes_token and no_token and condition not in out:
                out[condition] = RewardMarket(
                    condition_id=condition,
                    market_id=str(row.get("market_id") or ""),
                    event_id=str(row.get("event_id") or ""),
                    slug=str(row.get("market_slug") or row.get("slug") or ""),
                    question=str(row.get("question") or ""),
                    yes_token=yes_token,
                    no_token=no_token,
                    volume_24h=max(0.0, finite(row.get("volume_24hr"))),
                    market_competitiveness=max(0.0, finite(row.get("market_competitiveness"))),
                )
                if limit > 0 and len(out) >= limit:
                    break
        if not next_cursor or next_cursor == "LTE=" or next_cursor == cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise RuntimeError("reward catalog pagination guard reached before exhaustion")
    return out


def rank_markets(
    pools: dict[str, RewardPool],
    markets: dict[str, RewardMarket],
    *,
    max_active: int,
    min_volume_24h: float,
) -> list[MarketSelection]:
    rows: list[MarketSelection] = []
    for condition, pool in pools.items():
        market = markets.get(condition)
        if market is None or market.volume_24h < min_volume_24h:
            continue
        # Reward intensity is a useful slow-path prior, not a guaranteed return.
        # Competition is penalized smoothly and minimum qualifying size captures
        # capital tied up just to qualify for the program.
        competition = 1.0 + market.market_competitiveness
        reward_intensity = pool.total_daily_rate / (competition * max(1.0, pool.min_size))
        reward_component = math.log1p(pool.total_daily_rate)
        flow_component = math.log1p(market.volume_24h)
        size_penalty = math.log1p(pool.min_size)
        competition_penalty = math.log1p(market.market_competitiveness)
        score = 0.50 * reward_component + 0.35 * flow_component - 0.10 * size_penalty - 0.05 * competition_penalty
        rows.append(MarketSelection(
            condition_id=condition,
            market_id=market.market_id,
            event_id=market.event_id,
            slug=market.slug,
            question=market.question,
            yes_token=market.yes_token,
            no_token=market.no_token,
            volume_24h=market.volume_24h,
            market_competitiveness=market.market_competitiveness,
            rewards_max_spread_cents=pool.max_spread_cents,
            rewards_min_size=pool.min_size,
            native_daily_rate=pool.native_daily_rate,
            sponsored_daily_rate=pool.sponsored_daily_rate,
            total_daily_rate=pool.total_daily_rate,
            reward_intensity=reward_intensity,
            selection_score=score,
        ))
    rows.sort(key=lambda row: (row.selection_score, row.reward_intensity, row.volume_24h), reverse=True)
    return rows[: max(1, int(max_active))]


def _validated_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if cfg.get("paper_only") is not True or cfg.get("authenticated_execution") is not False or cfg.get("real_order_submission") is not False:
        raise ValueError("maker_selector_requires_paper_auth_disabled")
    selection_cfg = cfg.get("market_selection") or {}
    capacity_cfg = selection_cfg.get("resource_capacity") if isinstance(selection_cfg.get("resource_capacity"), dict) else {}
    resource_capacity = int(capacity_cfg.get("shard_count_budget", 0)) * int(capacity_cfg.get("markets_per_shard", 0))
    configured_capacity = int(selection_cfg.get("max_active_markets", 0))
    if resource_capacity <= 0 or configured_capacity != resource_capacity:
        raise ValueError("maker market capacity must equal declared shard resource capacity")
    return cfg, selection_cfg, capacity_cfg, resource_capacity


def _primary_snapshot(
    cfg: dict[str, Any],
    selection_cfg: dict[str, Any],
    capacity_cfg: dict[str, Any],
    resource_capacity: int,
    *,
    model_sha: str,
    deadline_seconds: float,
    request_timeout_seconds: float,
    max_pool_pages: int,
    max_market_pages: int,
) -> dict[str, Any]:
    clob_url = str(cfg.get("clob_url") or "https://clob.polymarket.com")
    deadline = time.monotonic() + max(0.1, float(deadline_seconds))
    pools = fetch_reward_pools(
        clob_url, deadline=deadline, request_timeout=request_timeout_seconds,
        max_pages=max_pool_pages,
    )
    markets = fetch_reward_markets(
        clob_url, deadline=deadline, request_timeout=request_timeout_seconds,
        max_pages=max_market_pages,
    )
    selected = rank_markets(
        pools,
        markets,
        max_active=resource_capacity,
        min_volume_24h=float(selection_cfg.get("min_volume_24h", 100.0)),
    )
    if not selected:
        raise RuntimeError("reward selector returned no eligible markets")
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "timestamp_ms": time.time_ns() // 1_000_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "source": "public_clob_rewards",
        "selection_mode": "REWARDED",
        "degraded": False,
        "reward_data_available": True,
        "reward_pool_count": len(pools),
        "reward_market_count": len(markets),
        "selected_count": len(selected),
        "resource_capacity_markets": resource_capacity,
        "resource_capacity": capacity_cfg,
        "markets": [asdict(row) for row in selected],
        "note": "Pool dollars are configuration facts; realized reward share remains competition-dependent and is not guaranteed.",
    }


def _fallback_snapshot(
    universe_path: Path,
    selection_cfg: dict[str, Any],
    capacity_cfg: dict[str, Any],
    resource_capacity: int,
    *,
    model_sha: str,
    primary_error: str,
    now_ms: int,
) -> dict[str, Any]:
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    if (
        universe.get("schema") != UNIVERSE_SCHEMA
        or universe.get("paper_only") is not True
        or universe.get("authenticated_execution") is not False
        or universe.get("real_order_submission") is not False
        or universe.get("execution_authority") is not False
        or universe.get("discovery_exhaustive") is not True
        or universe.get("pagination_loop_guard_hit") is not False
        or universe.get("model_sha") != model_sha
    ):
        raise ValueError("maker_fallback_universe_contract_invalid")
    maximum_age_ms = int(float(selection_cfg.get("fallback_universe_max_age_seconds", 180.0)) * 1000.0)
    age_ms = now_ms - int(universe.get("timestamp_ms") or 0)
    if age_ms < -5_000 or age_ms > maximum_age_ms:
        raise ValueError(f"maker_fallback_universe_stale:{age_ms}")
    minimum_volume = float(selection_cfg.get("min_volume_24h", 100.0))
    minimum_liquidity = float(selection_cfg.get("min_liquidity", 25.0))
    selected: list[dict[str, Any]] = []
    for raw in universe.get("markets") if isinstance(universe.get("markets"), list) else []:
        if not isinstance(raw, dict) or len(selected) >= resource_capacity:
            break
        tokens = raw.get("clob_token_ids") if isinstance(raw.get("clob_token_ids"), list) else []
        events = raw.get("event_ids") if isinstance(raw.get("event_ids"), list) else []
        if (
            raw.get("active") is not True
            or raw.get("closed") is True
            or raw.get("accepting_orders") is not True
            or len(tokens) < 2
            or finite(raw.get("liquidity")) < minimum_liquidity
            or finite(raw.get("volume_24h")) < minimum_volume
        ):
            continue
        condition_id = str(raw.get("condition_id") or "")
        market_id = str(raw.get("market_id") or "")
        yes_token, no_token = str(tokens[0] or ""), str(tokens[1] or "")
        if not condition_id or not market_id or not yes_token or not no_token or yes_token == no_token:
            continue
        selected.append({
            "condition_id": condition_id,
            "market_id": market_id,
            "event_id": str(events[0] if events else ""),
            "slug": str(raw.get("slug") or ""),
            "question": str(raw.get("question") or ""),
            "yes_token": yes_token,
            "no_token": no_token,
            "volume_24h": max(0.0, finite(raw.get("volume_24h"))),
            "market_competitiveness": 0.0,
            "rewards_max_spread_cents": 0.0,
            "rewards_min_size": 0.0,
            "native_daily_rate": 0.0,
            "sponsored_daily_rate": 0.0,
            "total_daily_rate": 0.0,
            "reward_intensity": 0.0,
            "selection_score": finite(raw.get("score")),
        })
    if not selected:
        raise ValueError("maker_fallback_universe_has_no_eligible_markets")
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "timestamp_ms": now_ms,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "source": "adaptive_universe_fallback",
        "selection_mode": "LIQUIDITY_FALLBACK",
        "degraded": True,
        "reward_data_available": False,
        "primary_error": primary_error,
        "reward_pool_count": 0,
        "reward_market_count": 0,
        "selected_count": len(selected),
        "resource_capacity_markets": resource_capacity,
        "resource_capacity": capacity_cfg,
        "universe_membership_sha256": str(universe.get("membership_sha256") or ""),
        "markets": selected,
        "note": "Reward REST was unavailable; PAPER maker remains operational on the fresh exhaustive liquidity universe with reward assumptions forced to zero.",
    }


def build_snapshot(
    config_path: Path,
    *,
    fallback_universe_path: Path | None = None,
    model_sha: str = "",
    deadline_seconds: float | None = None,
    request_timeout_seconds: float | None = None,
    max_pool_pages: int | None = None,
    max_market_pages: int | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    cfg, selection_cfg, capacity_cfg, resource_capacity = _validated_config(config_path)
    if model_sha and (len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha.lower())):
        raise ValueError("model_sha must be a 40-character hexadecimal SHA")
    model_sha = model_sha.lower()
    deadline_seconds = float(deadline_seconds if deadline_seconds is not None else selection_cfg.get("selector_deadline_seconds", 12.0))
    request_timeout_seconds = float(request_timeout_seconds if request_timeout_seconds is not None else selection_cfg.get("selector_request_timeout_seconds", 5.0))
    max_pool_pages = int(max_pool_pages if max_pool_pages is not None else selection_cfg.get("selector_max_pool_pages", 100))
    max_market_pages = int(max_market_pages if max_market_pages is not None else selection_cfg.get("selector_max_market_pages", 200))
    try:
        return _primary_snapshot(
            cfg, selection_cfg, capacity_cfg, resource_capacity,
            model_sha=model_sha, deadline_seconds=deadline_seconds,
            request_timeout_seconds=request_timeout_seconds,
            max_pool_pages=max_pool_pages, max_market_pages=max_market_pages,
        )
    except Exception as error:
        if fallback_universe_path is None:
            raise
        primary_error = f"{type(error).__name__}:{error}"[:500]
        return _fallback_snapshot(
            fallback_universe_path, selection_cfg, capacity_cfg, resource_capacity,
            model_sha=model_sha, primary_error=primary_error,
            now_ms=time.time_ns() // 1_000_000 if now_ms is None else int(now_ms),
        )


def selector_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SELECTOR_STATUS_SCHEMA,
        "timestamp_ms": snapshot.get("timestamp_ms"),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": snapshot.get("model_sha"),
        "state": "OPERATIONAL_FALLBACK" if snapshot.get("degraded") else "OPERATIONAL_REWARDED",
        "ready": True,
        "degraded": snapshot.get("degraded") is True,
        "source": snapshot.get("source"),
        "selected_count": snapshot.get("selected_count", 0),
        "reward_pool_count": snapshot.get("reward_pool_count", 0),
        "reward_market_count": snapshot.get("reward_market_count", 0),
        "primary_error": snapshot.get("primary_error", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_professional_market_maker.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--fallback-universe", type=Path)
    parser.add_argument("--model-sha", default="")
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--request-timeout-seconds", type=float)
    args = parser.parse_args()
    snapshot = build_snapshot(
        args.config,
        fallback_universe_path=args.fallback_universe,
        model_sha=args.model_sha,
        deadline_seconds=args.deadline_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    atomic_json(args.output, snapshot)
    if args.status is not None:
        atomic_json(args.status, selector_status(snapshot))
    print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
