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
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def request_json(url: str, *, timeout: int = 20) -> Any:
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


def fetch_reward_pools(clob_url: str) -> dict[str, RewardPool]:
    out: dict[str, RewardPool] = {}
    cursor = ""
    seen: set[str] = set()
    for _ in range(100):
        url = clob_url.rstrip("/") + "/rewards/markets/current"
        if cursor:
            url += "?next_cursor=" + urllib.parse.quote(cursor, safe="")
        rows, next_cursor = _pagination(request_json(url))
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
    return out


def fetch_reward_markets(clob_url: str, *, limit: int = 1000) -> dict[str, RewardMarket]:
    out: dict[str, RewardMarket] = {}
    cursor = ""
    seen: set[str] = set()
    while len(out) < limit:
        url = clob_url.rstrip("/") + "/rewards/markets/multi?limit=500"
        if cursor:
            url += "&next_cursor=" + urllib.parse.quote(cursor, safe="")
        rows, next_cursor = _pagination(request_json(url))
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
                if len(out) >= limit:
                    break
        if not next_cursor or next_cursor == "LTE=" or next_cursor == cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
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


def build_snapshot(config_path: Path) -> dict[str, Any]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if cfg.get("paper_only") is not True or cfg.get("authenticated_execution") is not False or cfg.get("real_order_submission") is not False:
        raise ValueError("maker_selector_requires_paper_auth_disabled")
    selection_cfg = cfg.get("market_selection") or {}
    clob_url = str(cfg.get("clob_url") or "https://clob.polymarket.com")
    pools = fetch_reward_pools(clob_url)
    markets = fetch_reward_markets(clob_url, limit=max(1000, int(selection_cfg.get("max_active_markets", 40)) * 10))
    selected = rank_markets(
        pools,
        markets,
        max_active=int(selection_cfg.get("max_active_markets", 40)),
        min_volume_24h=float(selection_cfg.get("min_volume_24h", 100.0)),
    )
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "timestamp_ms": time.time_ns() // 1_000_000,
        "paper_only": True,
        "authenticated_execution": False,
        "source": "public_clob_rewards",
        "reward_pool_count": len(pools),
        "reward_market_count": len(markets),
        "selected_count": len(selected),
        "markets": [asdict(row) for row in selected],
        "note": "Pool dollars are configuration facts; realized reward share remains competition-dependent and is not guaranteed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_professional_market_maker.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = build_snapshot(args.config)
    atomic_json(args.output, snapshot)
    print(json.dumps(snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
