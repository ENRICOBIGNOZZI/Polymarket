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
from datetime import datetime, timezone
import csv
import hashlib
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


def selection_membership_sha256(snapshot: dict[str, Any]) -> str:
    markets = snapshot.get("markets") if isinstance(snapshot.get("markets"), list) else []
    membership = sorted(
        (
            str(row.get("condition_id") or ""),
            str(row.get("market_id") or ""),
            str(row.get("yes_token") or ""),
            str(row.get("no_token") or ""),
        )
        for row in markets
        if isinstance(row, dict)
    )
    payload = json.dumps(membership, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    yes_price: float = 0.5
    no_price: float = 0.5
    spread: float = 0.0


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
    reward_qualification_notional_usd: float
    reward_touch_qualifies_at_selection: bool


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


def _reward_pool(row: Any) -> RewardPool | None:
    if not isinstance(row, dict):
        return None
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
    if not condition or max_spread <= 0.0 or min_size <= 0.0 or total <= 0.0:
        return None
    return RewardPool(
        condition_id=condition,
        max_spread_cents=max_spread,
        min_size=min_size,
        native_daily_rate=max(0.0, native),
        sponsored_daily_rate=sponsored,
        total_daily_rate=max(0.0, total),
    )


def _reward_market(row: Any) -> RewardMarket | None:
    if not isinstance(row, dict):
        return None
    condition = str(row.get("condition_id") or "")
    yes_token = ""
    no_token = ""
    yes_price = 0.5
    no_price = 0.5
    tokens = row.get("tokens")
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            outcome = str(token.get("outcome") or "").strip().lower()
            token_id = str(token.get("token_id") or "")
            if outcome == "yes":
                yes_token = token_id
                yes_price = min(1.0, max(0.0, finite(token.get("price"), 0.5)))
            elif outcome == "no":
                no_token = token_id
                no_price = min(1.0, max(0.0, finite(token.get("price"), 0.5)))
    if not condition or not yes_token or not no_token:
        return None
    return RewardMarket(
        condition_id=condition,
        market_id=str(row.get("market_id") or ""),
        event_id=str(row.get("event_id") or ""),
        slug=str(row.get("market_slug") or row.get("slug") or ""),
        question=str(row.get("question") or ""),
        yes_token=yes_token,
        no_token=no_token,
        volume_24h=max(0.0, finite(row.get("volume_24hr"))),
        market_competitiveness=max(0.0, finite(row.get("market_competitiveness"))),
        yes_price=yes_price,
        no_price=no_price,
        spread=max(0.0, finite(row.get("spread"))),
    )


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
            pool = _reward_pool(row)
            if pool is not None:
                out[pool.condition_id] = pool
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
        url = clob_url.rstrip("/") + "/rewards/markets/multi?page_size=500"
        if cursor:
            url += "&next_cursor=" + urllib.parse.quote(cursor, safe="")
        rows, next_cursor = _pagination(fetcher(url, timeout=_bounded_timeout(deadline, request_timeout)))
        for row in rows:
            market = _reward_market(row)
            if market is not None and market.condition_id not in out:
                out[market.condition_id] = market
                if limit > 0 and len(out) >= limit:
                    break
        if not next_cursor or next_cursor == "LTE=" or next_cursor == cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise RuntimeError("reward catalog pagination guard reached before exhaustion")
    return out


def fetch_reward_catalog(
    clob_url: str,
    *,
    min_volume_24h: float,
    deadline: float,
    request_timeout: float = 20.0,
    max_pages: int = 200,
    fetcher: Callable[..., Any] = request_json,
) -> tuple[dict[str, RewardPool], dict[str, RewardMarket]]:
    """Fetch the exhaustive eligible catalog once, deriving pool and market facts together."""
    pools: dict[str, RewardPool] = {}
    markets: dict[str, RewardMarket] = {}
    cursor = ""
    seen: set[str] = set()
    query = {
        "page_size": "500",
        "min_volume_24hr": f"{max(0.0, float(min_volume_24h)):.12g}",
        "order_by": "volume_24hr",
        "position": "DESC",
    }
    for _ in range(max(1, int(max_pages))):
        if cursor:
            query["next_cursor"] = cursor
        url = clob_url.rstrip("/") + "/rewards/markets/multi?" + urllib.parse.urlencode(query)
        rows, next_cursor = _pagination(
            fetcher(url, timeout=_bounded_timeout(deadline, request_timeout))
        )
        for row in rows:
            pool = _reward_pool(row)
            market = _reward_market(row)
            if pool is not None:
                pools[pool.condition_id] = pool
            if market is not None:
                markets[market.condition_id] = market
        if not next_cursor or next_cursor == "LTE=" or next_cursor == cursor or next_cursor in seen:
            break
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise RuntimeError("reward catalog pagination guard reached before exhaustion")
    return pools, markets


def rank_markets(
    pools: dict[str, RewardPool],
    markets: dict[str, RewardMarket],
    *,
    max_active: int,
    min_volume_24h: float,
    max_order_notional_usd: float = math.inf,
    max_quote_shares: float = 100.0,
) -> list[MarketSelection]:
    rows: list[MarketSelection] = []
    for condition, pool in pools.items():
        market = markets.get(condition)
        if market is None or market.volume_24h < min_volume_24h:
            continue
        qualification_notional = pool.min_size * max(market.yes_price, market.no_price)
        touch_distance_cents = 50.0 * market.spread
        if (
            pool.min_size > max_quote_shares + 1e-12
            or qualification_notional > max_order_notional_usd + 1e-12
            or touch_distance_cents > pool.max_spread_cents + 1e-12
        ):
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
            reward_qualification_notional_usd=qualification_notional,
            reward_touch_qualifies_at_selection=True,
        ))
    rows.sort(key=lambda row: (row.selection_score, row.reward_intensity, row.volume_24h), reverse=True)
    return rows[: max(1, int(max_active))]


def _iso_timestamp_ms(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000.0)
    except (ValueError, OverflowError):
        return 0


def _generic_maker_market_allowed(raw: dict[str, Any], selection_cfg: dict[str, Any]) -> bool:
    """Keep unmodelled live-event information risk outside generic Maker.

    Timed sports require a verified game-to-contract mapping and a causal sports
    feed.  Until that sleeve is promoted, recent public CLOB flow is evidence of
    pick-off risk rather than a fair-value signal for the generic maker.
    """
    if selection_cfg.get("exclude_timed_sports_without_verified_mapping") is not True:
        raise ValueError("maker_timed_sports_exclusion_not_fail_closed")
    return raw.get("timed_sports") is not True


LIVE_FLOW_SCHEMA = "polymarket_v7_live_trade_flow_v1"


def _canonical_live_flow_aggregates(
    live_flow_path: Path,
    *,
    model_sha: str,
    now_ms: int,
    maximum_age_ms: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Load full-universe causal prints published by the C++ WS owner."""
    if not live_flow_path.is_file():
        raise ValueError("maker_live_flow_missing")
    payload = json.loads(live_flow_path.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != LIVE_FLOW_SCHEMA
        or payload.get("producer") != "FAST_STRUCTURAL_CPP_WEBSOCKET"
        or payload.get("paper_only") is not True
        or payload.get("authenticated_execution") is not False
        or payload.get("real_order_submission") is not False
        or payload.get("model_sha") != model_sha
    ):
        raise ValueError("maker_live_flow_contract_invalid")
    published_ms = int(payload.get("timestamp_ms") or 0)
    publish_age_ms = now_ms - published_ms
    if publish_age_ms < -5_000 or publish_age_ms > maximum_age_ms:
        raise ValueError(f"maker_live_flow_publish_stale:{publish_age_ms}")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("maker_live_flow_rows_invalid")
    aggregates: dict[str, dict[str, Any]] = {}
    latest_receive_ms = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        condition_id = str(raw.get("condition_id") or "")
        receive_ms = int(finite(raw.get("last_receive_ts_ms"), 0.0))
        if not condition_id or receive_ms <= 0 or receive_ms > now_ms + 5_000:
            continue
        latest_receive_ms = max(latest_receive_ms, receive_ms)
        item = aggregates.setdefault(condition_id, {
            "prints": 0, "shares": 0.0, "notional": 0.0,
            "buy_prints_5s": 0, "buy_prints_30s": 0,
            "buy_prints_2m": 0, "buy_prints_10m": 0,
            "buy_shares_2m": 0.0, "buy_shares_10m": 0.0,
            "buy_notional_10m": 0.0,
            "last_buy_receive_ms": 0,
            "sell_prints_5s": 0, "sell_prints_30s": 0,
            "sell_prints_2m": 0, "sell_prints_10m": 0,
            "sell_shares_2m": 0.0, "sell_shares_10m": 0.0,
            "sell_notional_10m": 0.0,
            "last_receive_ms": 0, "last_sell_receive_ms": 0,
            "transactions": set(),
            "token_flow": {},
        })
        token_id = str(raw.get("token_id") or "")
        token_flow = item["token_flow"].setdefault(token_id, {
            "buy_prints_5s": 0, "buy_prints_30s": 0,
            "buy_prints_2m": 0, "buy_prints_10m": 0,
            "buy_shares_2m": 0.0, "buy_shares_10m": 0.0,
            "buy_notional_10m": 0.0,
            "last_buy_receive_ms": 0,
            "sell_prints_5s": 0, "sell_prints_30s": 0,
            "sell_prints_2m": 0, "sell_prints_10m": 0,
            "sell_shares_2m": 0.0, "sell_shares_10m": 0.0,
            "sell_notional_10m": 0.0,
            "last_sell_receive_ms": 0,
            "tick_size": 0.0, "best_bid": 0.0, "best_ask": 0.0,
            "best_bid_depth": 0.0, "best_ask_depth": 0.0,
            "book_evidence_valid": False,
        })
        tick_size = max(0.0, finite(raw.get("tick_size"), 0.0))
        best_bid = max(0.0, finite(raw.get("best_bid"), 0.0))
        best_ask = min(1.0, finite(raw.get("best_ask"), 1.0))
        best_bid_depth = max(0.0, finite(raw.get("best_bid_depth"), 0.0))
        best_ask_depth = max(0.0, finite(raw.get("best_ask_depth"), 0.0))
        book_valid = (
            tick_size > 0.0 and 0.0 < best_bid < best_ask < 1.0
            and best_bid_depth > 0.0 and best_ask_depth > 0.0
        )
        if book_valid:
            token_flow.update({
                "tick_size": tick_size,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "best_bid_depth": best_bid_depth,
                "best_ask_depth": best_ask_depth,
                "book_evidence_valid": True,
            })
        for side in ("buy", "sell"):
            for source_suffix, target_suffix in (("5s", "5s"), ("30s", "30s"),
                                                  ("120s", "2m"), ("600s", "10m")):
                key = f"{side}_prints_{target_suffix}"
                count = int(finite(raw.get(f"{side}_prints_{source_suffix}"), 0.0))
                item[key] += count
                token_flow[key] += count
            shares = max(0.0, finite(raw.get(f"{side}_shares_600s"), 0.0))
            shares_2m = max(0.0, finite(raw.get(f"{side}_shares_120s"), 0.0))
            notional = max(0.0, finite(raw.get(f"{side}_notional_600s"), 0.0))
            item[f"{side}_shares_10m"] += shares
            item[f"{side}_shares_2m"] += shares_2m
            item[f"{side}_notional_10m"] += notional
            token_flow[f"{side}_shares_10m"] += shares
            token_flow[f"{side}_shares_2m"] += shares_2m
            token_flow[f"{side}_notional_10m"] += notional
            side_receive_ms = int(finite(
                raw.get(f"last_{side}_receive_ts_ms_600s"), 0.0))
            item[f"last_{side}_receive_ms"] = max(
                int(item[f"last_{side}_receive_ms"]),
                side_receive_ms,
            )
            token_flow[f"last_{side}_receive_ms"] = max(
                int(token_flow[f"last_{side}_receive_ms"]), side_receive_ms)
        item["prints"] = int(item["buy_prints_10m"]) + int(item["sell_prints_10m"])
        item["shares"] = float(item["buy_shares_10m"]) + float(item["sell_shares_10m"])
        item["notional"] = float(item["buy_notional_10m"]) + float(item["sell_notional_10m"])
        item["last_receive_ms"] = max(int(item["last_receive_ms"]), receive_ms)
    if latest_receive_ms <= 0 or now_ms - latest_receive_ms > maximum_age_ms:
        raise ValueError(f"maker_live_flow_trade_stale:{now_ms - latest_receive_ms}")
    return aggregates, latest_receive_ms


def _recent_flow_snapshot(
    universe_path: Path,
    trade_tape_path: Path,
    selection_cfg: dict[str, Any],
    capacity_cfg: dict[str, Any],
    resource_capacity: int,
    *,
    model_sha: str,
    now_ms: int,
    live_flow_path: Path | None = None,
) -> dict[str, Any]:
    """Rank PAPER markets by causal recent public prints, not reward-pool size."""
    flow_cfg = selection_cfg.get("recent_flow")
    if not isinstance(flow_cfg, dict) or flow_cfg.get("enabled") is not True:
        raise ValueError("maker_recent_flow_disabled")
    if live_flow_path is None and not trade_tape_path.is_file():
        raise ValueError("maker_recent_flow_tape_missing")
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
        raise ValueError("maker_recent_flow_universe_contract_invalid")
    maximum_universe_age_ms = int(
        float(selection_cfg.get("fallback_universe_max_age_seconds", 180.0)) * 1000.0
    )
    universe_age_ms = now_ms - int(universe.get("timestamp_ms") or 0)
    if universe_age_ms < -5_000 or universe_age_ms > maximum_universe_age_ms:
        raise ValueError(f"maker_recent_flow_universe_stale:{universe_age_ms}")

    lookback_ms = max(1_000, int(float(flow_cfg.get("lookback_seconds", 180.0)) * 1000.0))
    maximum_tape_age_ms = max(
        1_000, int(float(flow_cfg.get("maximum_tape_age_seconds", 30.0)) * 1000.0)
    )
    aggregates: dict[str, dict[str, Any]] = {}
    latest_receive_ms = 0
    flow_source = "REST_TRADE_TAPE_COMPATIBILITY"
    if live_flow_path is not None:
        aggregates, latest_receive_ms = _canonical_live_flow_aggregates(
            live_flow_path, model_sha=model_sha, now_ms=now_ms,
            maximum_age_ms=maximum_tape_age_ms,
        )
        flow_source = "FULL_UNIVERSE_CPP_WEBSOCKET"
        raw_rows: list[dict[str, Any]] = []
    else:
        seen_prints: set[tuple[str, str, str, str, str, str]] = set()
        with trade_tape_path.open(newline="", encoding="utf-8") as handle:
            raw_rows = list(csv.DictReader(handle))
    for raw in raw_rows:
            condition_id = str(raw.get("condition_id") or "")
            receive_ms = int(finite(raw.get("received_ms"), 0.0))
            latest_receive_ms = max(latest_receive_ms, receive_ms)
            if not condition_id or receive_ms < now_ms - lookback_ms or receive_ms > now_ms + 5_000:
                continue
            identity = (
                condition_id,
                str(raw.get("transaction_hash") or ""),
                str(raw.get("asset_id") or ""),
                str(raw.get("timestamp") or ""),
                str(raw.get("price") or ""),
                str(raw.get("size") or ""),
            )
            if identity in seen_prints:
                continue
            seen_prints.add(identity)
            size = max(0.0, finite(raw.get("size")))
            price = min(1.0, max(0.0, finite(raw.get("price"))))
            item = aggregates.setdefault(condition_id, {
                "prints": 0,
                "shares": 0.0,
                "notional": 0.0,
                "buy_prints_5s": 0,
                "buy_prints_30s": 0,
                "buy_prints_2m": 0,
                "buy_prints_10m": 0,
                "buy_shares_10m": 0.0,
                "buy_notional_10m": 0.0,
                "last_buy_receive_ms": 0,
                "sell_prints_5s": 0,
                "sell_prints_30s": 0,
                "sell_prints_2m": 0,
                "sell_prints_10m": 0,
                "sell_shares_10m": 0.0,
                "sell_notional_10m": 0.0,
                "last_receive_ms": 0,
                "last_sell_receive_ms": 0,
                "transactions": set(),
            })
            item["prints"] += 1
            item["shares"] += size
            item["notional"] += size * price
            item["last_receive_ms"] = max(int(item["last_receive_ms"]), receive_ms)
            trade_side = str(raw.get("side") or "").upper()
            if trade_side in {"BUY", "SELL"}:
                age_ms = max(0, now_ms - receive_ms)
                prefix = "buy" if trade_side == "BUY" else "sell"
                if age_ms <= 5_000:
                    item[f"{prefix}_prints_5s"] += 1
                if age_ms <= 30_000:
                    item[f"{prefix}_prints_30s"] += 1
                if age_ms <= 120_000:
                    item[f"{prefix}_prints_2m"] += 1
                if age_ms <= 600_000:
                    item[f"{prefix}_prints_10m"] += 1
                    item[f"{prefix}_shares_10m"] += size
                    item[f"{prefix}_notional_10m"] += size * price
                    key = f"last_{prefix}_receive_ms"
                    item[key] = max(int(item[key]), receive_ms)
            tx_hash = str(raw.get("transaction_hash") or "")
            if tx_hash:
                item["transactions"].add(tx_hash)
    if latest_receive_ms <= 0 or now_ms - latest_receive_ms > maximum_tape_age_ms:
        raise ValueError(f"maker_recent_flow_tape_stale:{now_ms - latest_receive_ms}")

    minimum_prints = max(1, int(flow_cfg.get("minimum_prints", 2)))
    minimum_side_prints_2m = max(1, int(flow_cfg.get(
        "minimum_side_prints_2m", flow_cfg.get("minimum_sell_prints_2m", 1))))
    minimum_side_prints_10m = max(1, int(flow_cfg.get(
        "minimum_side_prints_10m", flow_cfg.get("minimum_sell_prints_10m", 1))))
    maximum_last_side_age_ms = max(
        1_000,
        int(float(flow_cfg.get(
            "maximum_last_side_age_seconds",
            flow_cfg.get("maximum_last_sell_age_seconds", 120.0))) * 1_000.0),
    )
    maximum_market_flow_age_ms = max(
        1_000,
        int(float(flow_cfg.get("maximum_market_flow_age_seconds", 120.0)) * 1_000.0),
    )
    minimum_markets = max(1, int(flow_cfg.get("minimum_markets", 5)))
    minimum_tte_ms = max(
        0, int(float(flow_cfg.get("minimum_time_to_end_seconds", 900.0)) * 1000.0)
    )
    minimum_volume = float(selection_cfg.get("min_volume_24h", 100.0))
    minimum_liquidity = float(selection_cfg.get("min_liquidity", 25.0))
    minimum_mid = float(flow_cfg.get("minimum_mid", selection_cfg.get("min_mid", 0.02)))
    maximum_mid = float(flow_cfg.get("maximum_mid", selection_cfg.get("max_mid", 0.98)))
    maximum_spread = max(0.0, float(flow_cfg.get("maximum_spread", 0.10)))
    selection_quote_horizon_seconds = max(
        0.1, float(flow_cfg.get("selection_quote_horizon_seconds", 5.0)))
    selection_quote_shares = max(
        1e-6, float(flow_cfg.get("selection_quote_shares", 5.0)))
    weights = flow_cfg.get("score_weights") if isinstance(flow_cfg.get("score_weights"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for raw in universe.get("markets") if isinstance(universe.get("markets"), list) else []:
        if not isinstance(raw, dict):
            continue
        condition_id = str(raw.get("condition_id") or "")
        flow = aggregates.get(condition_id)
        tokens = raw.get("clob_token_ids") if isinstance(raw.get("clob_token_ids"), list) else []
        events = raw.get("event_ids") if isinstance(raw.get("event_ids"), list) else []
        liquidity = max(0.0, finite(raw.get("liquidity")))
        volume_24h = max(0.0, finite(raw.get("volume_24h")))
        midpoint = finite(raw.get("midpoint"), -1.0)
        spread = max(0.0, finite(raw.get("spread")))
        end_ms = _iso_timestamp_ms(raw.get("end_date"))
        if (
            flow is None
            or int(flow["prints"]) < minimum_prints
            or (flow_source == "FULL_UNIVERSE_CPP_WEBSOCKET"
                and now_ms - int(flow["last_receive_ms"]) > maximum_market_flow_age_ms)
            or not _generic_maker_market_allowed(raw, selection_cfg)
            or raw.get("active") is not True
            or raw.get("closed") is True
            or raw.get("accepting_orders") is not True
            or len(tokens) != 2
            or liquidity < minimum_liquidity
            or volume_24h < minimum_volume
            or midpoint < minimum_mid
            or midpoint > maximum_mid
            or spread <= 0.0
            or spread > maximum_spread
            or (minimum_tte_ms > 0 and (end_ms <= 0 or end_ms - now_ms < minimum_tte_ms))
        ):
            continue
        market_id = str(raw.get("market_id") or "")
        yes_token, no_token = str(tokens[0] or ""), str(tokens[1] or "")
        if not condition_id or not market_id or not yes_token or not no_token or yes_token == no_token:
            continue
        recent_flow_to_liquidity = float(flow["shares"]) / max(liquidity, minimum_liquidity)
        mid_balance = max(0.0, 1.0 - abs(midpoint - 0.5) / 0.5)
        buy_fresh = (
            int(flow["buy_prints_2m"]) >= minimum_side_prints_2m
            and int(flow["buy_prints_10m"]) >= minimum_side_prints_10m
            and now_ms - int(flow["last_buy_receive_ms"]) <= maximum_last_side_age_ms
        )
        sell_fresh = (
            int(flow["sell_prints_2m"]) >= minimum_side_prints_2m
            and int(flow["sell_prints_10m"]) >= minimum_side_prints_10m
            and now_ms - int(flow["last_sell_receive_ms"]) <= maximum_last_side_age_ms
        )
        common_score = (
            finite(weights.get("log_prints"), 3.0) * math.log1p(int(flow["prints"]))
            + finite(weights.get("log_notional"), 1.0) * math.log1p(float(flow["notional"]))
            + finite(weights.get("log_flow_to_liquidity"), 2.0)
              * math.log1p(recent_flow_to_liquidity * 1_000.0)
            + finite(weights.get("mid_balance"), 0.25) * mid_balance
            + finite(weights.get("log_spread_cents"), 0.10) * math.log1p(spread * 100.0)
        )
        bid_opportunity_score = common_score + (
            finite(weights.get("log_sell_prints_30s"), 4.0)
              * math.log1p(int(flow["sell_prints_30s"]))
            + finite(weights.get("log_sell_prints_2m"), 3.0)
              * math.log1p(int(flow["sell_prints_2m"]))
            + finite(weights.get("log_sell_prints_10m"), 2.0)
              * math.log1p(int(flow["sell_prints_10m"]))
            + finite(weights.get("log_sell_notional_10m"), 1.0)
              * math.log1p(float(flow["sell_notional_10m"]))
        )
        ask_opportunity_score = common_score + (
            finite(weights.get("log_buy_prints_30s"), 4.0)
              * math.log1p(int(flow["buy_prints_30s"]))
            + finite(weights.get("log_buy_prints_2m"), 3.0)
              * math.log1p(int(flow["buy_prints_2m"]))
            + finite(weights.get("log_buy_prints_10m"), 2.0)
              * math.log1p(int(flow["buy_prints_10m"]))
            + finite(weights.get("log_buy_notional_10m"), 1.0)
              * math.log1p(float(flow["buy_notional_10m"]))
        )
        bilateral_score = (
            0.5 * (bid_opportunity_score + ask_opportunity_score)
            + (2.0 if buy_fresh and sell_fresh else 0.0)
        )
        complete_set_cycle_score = bilateral_score + math.log1p(spread * 100.0)
        reward_capture_score = 0.0
        score = max(bid_opportunity_score, ask_opportunity_score, bilateral_score,
                    complete_set_cycle_score)
        side_mode = (
            "BILATERAL" if buy_fresh and sell_fresh
            else "INVENTORY_BACKED_ASK" if buy_fresh
            else "COLLATERAL_BACKED_BID" if sell_fresh
            else "STABLE_SPREAD_EXPLORATION"
        )
        token_flow = flow.get("token_flow") if isinstance(flow.get("token_flow"), dict) else {}
        quote_opportunities = []
        for outcome, token in (("YES", yes_token), ("NO", no_token)):
            token_stats = token_flow.get(token) if isinstance(token_flow.get(token), dict) else {}
            for quote_side, aggressor_prefix, side_score in (
                ("BUY", "sell", bid_opportunity_score),
                ("SELL", "buy", ask_opportunity_score),
            ):
                opposite_shares_2m = float(token_stats.get(
                    f"{aggressor_prefix}_shares_2m", 0.0))
                expected_opposite_shares = (
                    opposite_shares_2m / 120.0 * selection_quote_horizon_seconds)
                flow_reach_probability = (
                    1.0 - math.exp(-expected_opposite_shares / selection_quote_shares)
                    if expected_opposite_shares > 0.0 else 0.0)
                queue_ahead = float(token_stats.get(
                    "best_bid_depth" if quote_side == "BUY" else "best_ask_depth", 0.0))
                join_queue_depletion_probability = (
                    min(1.0, expected_opposite_shares
                        / max(1e-9, queue_ahead + selection_quote_shares))
                    if token_stats.get("book_evidence_valid") is True else 0.0)
                projected_join_fill_probability = (
                    flow_reach_probability * join_queue_depletion_probability)
                tick_size = float(token_stats.get("tick_size", 0.0))
                token_spread = max(
                    0.0, float(token_stats.get("best_ask", 0.0))
                    - float(token_stats.get("best_bid", 0.0)))
                inside_ticks = max(
                    0, int(math.floor(token_spread / tick_size + 1e-9)) - 1
                ) if tick_size > 0.0 else 0
                improve1_available = inside_ticks >= 1
                projected_improve1_fill_probability = (
                    flow_reach_probability if improve1_available else 0.0)
                projected_best_fill_probability = max(
                    projected_join_fill_probability,
                    projected_improve1_fill_probability,
                )
                quote_opportunities.append({
                    "outcome": outcome,
                    "token_id": token,
                    "quote_side": quote_side,
                    "required_aggressor_side": aggressor_prefix.upper(),
                    "opposite_prints_30s": int(token_stats.get(
                        f"{aggressor_prefix}_prints_30s", 0)),
                    "opposite_prints_2m": int(token_stats.get(
                        f"{aggressor_prefix}_prints_2m", 0)),
                    "opposite_prints_10m": int(token_stats.get(
                        f"{aggressor_prefix}_prints_10m", 0)),
                    "opposite_shares_10m": float(token_stats.get(
                        f"{aggressor_prefix}_shares_10m", 0.0)),
                    "opposite_shares_2m": opposite_shares_2m,
                    "last_opposite_flow_age_ms": (
                        now_ms - int(token_stats.get(
                            f"last_{aggressor_prefix}_receive_ms", 0))
                        if int(token_stats.get(
                            f"last_{aggressor_prefix}_receive_ms", 0)) > 0 else -1
                    ),
                    "market_side_score": side_score,
                    "book_evidence_valid": token_stats.get("book_evidence_valid") is True,
                    "tick_size": tick_size,
                    "best_bid": float(token_stats.get("best_bid", 0.0)),
                    "best_ask": float(token_stats.get("best_ask", 0.0)),
                    "queue_ahead_shares": queue_ahead,
                    "inside_ticks": inside_ticks,
                    "improve1_available": improve1_available,
                    "projected_flow_reach_probability": flow_reach_probability,
                    "projected_join_queue_depletion_probability": (
                        join_queue_depletion_probability),
                    "projected_join_fill_probability": projected_join_fill_probability,
                    "projected_improve1_fill_probability": (
                        projected_improve1_fill_probability),
                    "projected_best_fill_probability": projected_best_fill_probability,
                })
        quote_opportunities.sort(key=lambda row: (
            -float(row["projected_best_fill_probability"]),
            -int(row["opposite_prints_30s"]),
            -int(row["opposite_prints_2m"]),
            -float(row["opposite_shares_10m"]),
            str(row["token_id"]), str(row["quote_side"]),
        ))
        best_projected_fill_probability = max(
            (float(row["projected_best_fill_probability"])
             for row in quote_opportunities), default=0.0)
        score += finite(weights.get("log_projected_fillability"), 8.0) * math.log1p(
            1_000.0 * best_projected_fill_probability)
        candidates.append({
            "condition_id": condition_id,
            "market_id": market_id,
            "event_id": str(events[0] if events else ""),
            "slug": str(raw.get("slug") or ""),
            "question": str(raw.get("question") or ""),
            "yes_token": yes_token,
            "no_token": no_token,
            "volume_24h": volume_24h,
            "liquidity": liquidity,
            "midpoint": midpoint,
            "spread": spread,
            "market_competitiveness": 0.0,
            "rewards_max_spread_cents": 0.0,
            "rewards_min_size": 0.0,
            "native_daily_rate": 0.0,
            "sponsored_daily_rate": 0.0,
            "total_daily_rate": 0.0,
            "reward_intensity": 0.0,
            "selection_score": score,
            "best_projected_fill_probability": best_projected_fill_probability,
            "bid_opportunity_score": bid_opportunity_score,
            "ask_opportunity_score": ask_opportunity_score,
            "bilateral_market_making_score": bilateral_score,
            "complete_set_cycle_score": complete_set_cycle_score,
            "reward_capture_score": reward_capture_score,
            "side_mode": side_mode,
            "quote_opportunities": quote_opportunities,
            "recent_prints": int(flow["prints"]),
            "recent_unique_transactions": len(flow["transactions"]),
            "recent_share_volume": float(flow["shares"]),
            "recent_notional_usd": float(flow["notional"]),
            "recent_flow_to_liquidity": recent_flow_to_liquidity,
            "recent_last_trade_age_ms": now_ms - int(flow["last_receive_ms"]),
            "recent_buy_prints_5s": int(flow["buy_prints_5s"]),
            "recent_buy_prints_30s": int(flow["buy_prints_30s"]),
            "recent_buy_prints_2m": int(flow["buy_prints_2m"]),
            "recent_buy_prints_10m": int(flow["buy_prints_10m"]),
            "recent_buy_share_volume_10m": float(flow["buy_shares_10m"]),
            "recent_buy_notional_usd_10m": float(flow["buy_notional_10m"]),
            "recent_last_buy_age_ms": (
                now_ms - int(flow["last_buy_receive_ms"])
                if int(flow["last_buy_receive_ms"]) > 0 else -1
            ),
            "recent_sell_prints_5s": int(flow["sell_prints_5s"]),
            "recent_sell_prints_30s": int(flow["sell_prints_30s"]),
            "recent_sell_prints_2m": int(flow["sell_prints_2m"]),
            "recent_sell_prints_10m": int(flow["sell_prints_10m"]),
            "recent_sell_share_volume_10m": float(flow["sell_shares_10m"]),
            "recent_sell_notional_usd_10m": float(flow["sell_notional_10m"]),
            "recent_last_sell_age_ms": (
                now_ms - int(flow["last_sell_receive_ms"])
                if int(flow["last_sell_receive_ms"]) > 0 else -1
            ),
        })
    candidates.sort(key=lambda row: (
        -finite(row.get("selection_score")),
        -int(row.get("recent_prints") or 0),
        -finite(row.get("recent_notional_usd")),
        str(row.get("market_id") or ""),
    ))
    selected: list[dict[str, Any]] = []
    selected_events: set[str] = set()
    for row in candidates:
        event_key = str(row.get("event_id") or f"market:{row.get('market_id')}")
        if event_key in selected_events:
            continue
        selected_events.add(event_key)
        selected.append(row)
        if len(selected) >= resource_capacity:
            break
    # ``resource_capacity`` is a ceiling, not a market-count objective.  A
    # zero-flow fallback consumes inventory seed, WebSocket/decision capacity
    # and exploration budget without providing a realistic label.  Keep an
    # optional explicitly bounded reserve for controlled experiments, but the
    # production policy sets it to zero and leaves unused capacity available to
    # the next fresh-flow generation.
    operational_floor = min(
        resource_capacity,
        max(minimum_markets, int(flow_cfg.get(
            "minimum_operational_markets", minimum_markets))),
    )
    maximum_zero_flow_reserve = min(
        resource_capacity,
        max(0, int(flow_cfg.get("maximum_zero_flow_reserve_markets", 0))),
    )
    stable_reserve_added = 0
    reserve_target = min(
        operational_floor, len(selected) + maximum_zero_flow_reserve)
    if len(selected) < reserve_target:
        reserve = _fallback_snapshot(
            universe_path, selection_cfg, capacity_cfg, resource_capacity,
            model_sha=model_sha, primary_error="recent_flow_reserve",
            now_ms=now_ms, maximum_markets=resource_capacity,
        )
        for fallback_row in reserve["markets"]:
            event_key = str(
                fallback_row.get("event_id")
                or f"market:{fallback_row.get('market_id')}"
            )
            if event_key in selected_events:
                continue
            row = dict(fallback_row)
            row.update({
                "side_mode": "STABLE_SPREAD_EXPLORATION",
                "recent_prints": 0,
                "recent_unique_transactions": 0,
                "recent_share_volume": 0.0,
                "recent_notional_usd": 0.0,
                "recent_flow_to_liquidity": 0.0,
                "recent_last_trade_age_ms": -1,
                "recent_buy_prints_5s": 0,
                "recent_buy_prints_30s": 0,
                "recent_buy_prints_2m": 0,
                "recent_buy_prints_10m": 0,
                "recent_buy_share_volume_10m": 0.0,
                "recent_buy_notional_usd_10m": 0.0,
                "recent_last_buy_age_ms": -1,
                "recent_sell_prints_5s": 0,
                "recent_sell_prints_30s": 0,
                "recent_sell_prints_2m": 0,
                "recent_sell_prints_10m": 0,
                "recent_sell_share_volume_10m": 0.0,
                "recent_sell_notional_usd_10m": 0.0,
                "recent_last_sell_age_ms": -1,
                "quote_opportunities": [],
            })
            selected_events.add(event_key)
            selected.append(row)
            stable_reserve_added += 1
            if len(selected) >= reserve_target:
                break
    if len(selected) < minimum_markets:
        raise ValueError(f"maker_recent_flow_insufficient_markets:{len(selected)}")
    return {
        "schema": "polymarket_v7_maker_reward_selection_v1",
        "timestamp_ms": now_ms,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": model_sha,
        "source": "adaptive_universe_recent_flow",
        "selection_mode": "BILATERAL_AGGRESSOR_FLOW",
        "degraded": False,
        "reward_data_available": False,
        "reward_pool_count": 0,
        "reward_market_count": 0,
        "selected_count": len(selected),
        "resource_capacity_markets": resource_capacity,
        "resource_capacity": capacity_cfg,
        "universe_membership_sha256": str(universe.get("membership_sha256") or ""),
        "recent_flow_lookback_ms": lookback_ms,
        "recent_flow_latest_receive_ms": latest_receive_ms,
        "recent_flow_source": flow_source,
        "minimum_operational_markets": operational_floor,
        "maximum_zero_flow_reserve_markets": maximum_zero_flow_reserve,
        "stable_reserve_added": stable_reserve_added,
        "unused_resource_capacity_markets": max(0, resource_capacity - len(selected)),
        "minimum_side_prints_2m": minimum_side_prints_2m,
        "maximum_last_side_age_ms": maximum_last_side_age_ms,
        "markets": selected,
        "note": "PAPER maker ranks market-side cells from causal BUY/SELL aggressor flow. Resource capacity is a ceiling; zero-flow reserve is explicitly bounded and disabled in production. Rewards remain zero unless verified.",
    }


def _validated_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if cfg.get("paper_only") is not True or cfg.get("authenticated_execution") is not False or cfg.get("real_order_submission") is not False:
        raise ValueError("maker_selector_requires_paper_auth_disabled")
    selection_cfg = cfg.get("market_selection") or {}
    if selection_cfg.get("exclude_timed_sports_without_verified_mapping") is not True:
        raise ValueError("maker_timed_sports_exclusion_not_fail_closed")
    capacity_cfg = selection_cfg.get("resource_capacity") if isinstance(selection_cfg.get("resource_capacity"), dict) else {}
    resource_capacity = int(capacity_cfg.get("shard_count_budget", 0)) * int(capacity_cfg.get("markets_per_shard", 0))
    configured_capacity = int(selection_cfg.get("max_active_markets", 0))
    if resource_capacity <= 0 or configured_capacity != resource_capacity:
        raise ValueError("maker market capacity must equal declared shard resource capacity")
    return cfg, selection_cfg, capacity_cfg, resource_capacity


def _validated_sleeve_capital(allocation_path: Path) -> float:
    allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
    v7 = allocation.get("v7") if isinstance(allocation.get("v7"), dict) else {}
    scope = allocation.get("capital_scope") if isinstance(allocation.get("capital_scope"), dict) else {}
    strategy_budgets = scope.get("strategy_budgets") if isinstance(scope.get("strategy_budgets"), dict) else {}
    if (
        allocation.get("paper_only") is not True
        or v7.get("authenticated_execution") is not False
        or v7.get("real_order_submission") is not False
        or scope.get("sleeve") != "micro_maker"
        or scope.get("double_counting_forbidden") is not True
        or set(strategy_budgets) != {"professional_maker"}
    ):
        raise ValueError("maker_reward_allocation_contract_invalid")
    capital = finite(allocation.get("starting_capital"), -1.0)
    declared = finite(scope.get("sleeve_starting_capital"), -2.0)
    strategy_budget = finite(strategy_budgets.get("professional_maker"), -3.0)
    strategy_sum = finite(scope.get("strategy_budget_sum"), -4.0)
    if capital <= 0.0 or any(
        abs(value - capital) > 1e-9
        for value in (declared, strategy_budget, strategy_sum)
    ):
        raise ValueError("maker_reward_allocation_capital_mismatch")
    return capital


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
    max_order_notional_usd: float,
    max_quote_shares: float,
) -> dict[str, Any]:
    clob_url = str(cfg.get("clob_url") or "https://clob.polymarket.com")
    deadline = time.monotonic() + max(0.1, float(deadline_seconds))
    pools, markets = fetch_reward_catalog(
        clob_url,
        min_volume_24h=float(selection_cfg.get("min_volume_24h", 100.0)),
        deadline=deadline,
        request_timeout=request_timeout_seconds,
        max_pages=min(max_pool_pages, max_market_pages),
    )
    selected = rank_markets(
        pools,
        markets,
        max_active=resource_capacity,
        min_volume_24h=float(selection_cfg.get("min_volume_24h", 100.0)),
        max_order_notional_usd=max_order_notional_usd,
        max_quote_shares=max_quote_shares,
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
        "reward_qualification_max_order_notional_usd": max_order_notional_usd,
        "reward_qualification_max_quote_shares": max_quote_shares,
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
    maximum_markets: int | None = None,
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
    minimum_mid = float(selection_cfg.get("min_mid", 0.02))
    maximum_mid = float(selection_cfg.get("max_mid", 0.98))
    maximum_spread = max(0.0, float(selection_cfg.get("fallback_maximum_spread", 0.10)))
    weights = selection_cfg.get("fallback_score_weights") if isinstance(selection_cfg.get("fallback_score_weights"), dict) else {}
    candidates: list[dict[str, Any]] = []
    for raw in universe.get("markets") if isinstance(universe.get("markets"), list) else []:
        if not isinstance(raw, dict):
            continue
        tokens = raw.get("clob_token_ids") if isinstance(raw.get("clob_token_ids"), list) else []
        events = raw.get("event_ids") if isinstance(raw.get("event_ids"), list) else []
        liquidity = max(0.0, finite(raw.get("liquidity")))
        volume_24h = max(0.0, finite(raw.get("volume_24h")))
        midpoint = finite(raw.get("midpoint"), -1.0)
        spread = max(0.0, finite(raw.get("spread")))
        if (
            not _generic_maker_market_allowed(raw, selection_cfg)
            or raw.get("active") is not True
            or raw.get("closed") is True
            or raw.get("accepting_orders") is not True
            or len(tokens) < 2
            or liquidity < minimum_liquidity
            or volume_24h < minimum_volume
            or midpoint < minimum_mid
            or midpoint > maximum_mid
            or spread <= 0.0
            or spread > maximum_spread
        ):
            continue
        condition_id = str(raw.get("condition_id") or "")
        market_id = str(raw.get("market_id") or "")
        yes_token, no_token = str(tokens[0] or ""), str(tokens[1] or "")
        if not condition_id or not market_id or not yes_token or not no_token or yes_token == no_token:
            continue
        flow_to_depth = volume_24h / max(liquidity, minimum_liquidity)
        mid_balance = max(0.0, 1.0 - abs(midpoint - 0.5) / 0.5)
        score = (
            finite(weights.get("log_volume_24h"), 1.0) * math.log1p(volume_24h)
            + finite(weights.get("log_flow_to_depth"), 1.0) * math.log1p(flow_to_depth)
            + finite(weights.get("mid_balance"), 0.25) * mid_balance
            + finite(weights.get("log_spread_cents"), 0.1) * math.log1p(spread * 100.0)
        )
        candidates.append({
            "condition_id": condition_id,
            "market_id": market_id,
            "event_id": str(events[0] if events else ""),
            "slug": str(raw.get("slug") or ""),
            "question": str(raw.get("question") or ""),
            "yes_token": yes_token,
            "no_token": no_token,
            "volume_24h": volume_24h,
            "liquidity": liquidity,
            "midpoint": midpoint,
            "spread": spread,
            "flow_to_depth_24h": flow_to_depth,
            "market_competitiveness": 0.0,
            "rewards_max_spread_cents": 0.0,
            "rewards_min_size": 0.0,
            "native_daily_rate": 0.0,
            "sponsored_daily_rate": 0.0,
            "total_daily_rate": 0.0,
            "reward_intensity": 0.0,
            "selection_score": score,
        })
    candidates.sort(key=lambda row: (
        -finite(row.get("selection_score")),
        -finite(row.get("volume_24h")),
        finite(row.get("liquidity")),
        str(row.get("market_id") or ""),
    ))
    # This path has no causal aggressor-flow authority.  It exists only so a
    # fresh process can collect one tightly-budgeted positive-point-EV
    # exploration cell while the canonical WS flow plane warms up.  Treating
    # the shard capacity as a target here used to seed and quote 40 unrelated
    # markets, reproducing the exact no-fill dilution that the flow selector is
    # meant to remove.
    cold_start_maximum = min(
        resource_capacity,
        max(1, int(
            selection_cfg.get("cold_start_maximum_markets", 1)
            if maximum_markets is None else maximum_markets
        )),
    )
    selected: list[dict[str, Any]] = []
    selected_events: set[str] = set()
    for row in candidates:
        event_key = str(row.get("event_id") or f"market:{row.get('market_id')}")
        if event_key in selected_events:
            continue
        selected_events.add(event_key)
        selected.append({
            **row,
            "side_mode": "STABLE_SPREAD_EXPLORATION",
            "recent_prints": 0,
            "recent_unique_transactions": 0,
            "recent_share_volume": 0.0,
            "recent_notional_usd": 0.0,
            "recent_flow_to_liquidity": 0.0,
            "recent_last_trade_age_ms": -1,
            "recent_buy_prints_5s": 0,
            "recent_buy_prints_30s": 0,
            "recent_buy_prints_2m": 0,
            "recent_buy_prints_10m": 0,
            "recent_buy_share_volume_10m": 0.0,
            "recent_buy_notional_usd_10m": 0.0,
            "recent_last_buy_age_ms": -1,
            "recent_sell_prints_5s": 0,
            "recent_sell_prints_30s": 0,
            "recent_sell_prints_2m": 0,
            "recent_sell_prints_10m": 0,
            "recent_sell_share_volume_10m": 0.0,
            "recent_sell_notional_usd_10m": 0.0,
            "recent_last_sell_age_ms": -1,
            "quote_opportunities": [],
        })
        if len(selected) >= cold_start_maximum:
            break
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
        "selection_mode": "FLOW_FILLABILITY_FALLBACK",
        "degraded": True,
        "reward_data_available": False,
        "primary_error": primary_error,
        "reward_pool_count": 0,
        "reward_market_count": 0,
        "selected_count": len(selected),
        "resource_capacity_markets": resource_capacity,
        "cold_start_maximum_markets": cold_start_maximum,
        "unused_resource_capacity_markets": max(0, resource_capacity - len(selected)),
        "resource_capacity": capacity_cfg,
        "universe_membership_sha256": str(universe.get("membership_sha256") or ""),
        "markets": selected,
        "note": "Causal aggressor flow is unavailable; PAPER maker cold-start is bounded to explicit positive-point-EV exploration cells with reward assumptions forced to zero.",
    }


def build_snapshot(
    config_path: Path,
    *,
    fallback_universe_path: Path | None = None,
    trade_tape_path: Path | None = None,
    live_flow_path: Path | None = None,
    model_sha: str = "",
    deadline_seconds: float | None = None,
    request_timeout_seconds: float | None = None,
    max_pool_pages: int | None = None,
    max_market_pages: int | None = None,
    now_ms: int | None = None,
    sleeve_capital: float | None = None,
    allocation_path: Path | None = None,
) -> dict[str, Any]:
    cfg, selection_cfg, capacity_cfg, resource_capacity = _validated_config(config_path)
    if model_sha and (len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha.lower())):
        raise ValueError("model_sha must be a 40-character hexadecimal SHA")
    model_sha = model_sha.lower()
    deadline_seconds = float(deadline_seconds if deadline_seconds is not None else selection_cfg.get("selector_deadline_seconds", 12.0))
    request_timeout_seconds = float(request_timeout_seconds if request_timeout_seconds is not None else selection_cfg.get("selector_request_timeout_seconds", 5.0))
    max_pool_pages = int(max_pool_pages if max_pool_pages is not None else selection_cfg.get("selector_max_pool_pages", 100))
    max_market_pages = int(max_market_pages if max_market_pages is not None else selection_cfg.get("selector_max_market_pages", 200))
    if sleeve_capital is not None and allocation_path is not None:
        raise ValueError("maker_reward_capital_source_ambiguous")
    if allocation_path is not None:
        sleeve_capital = _validated_sleeve_capital(allocation_path)
    elif sleeve_capital is None:
        sleeve_capital = finite(selection_cfg.get("reward_sleeve_capital_usd"), 0.0)
    configured_sleeve_capital = finite(selection_cfg.get("reward_sleeve_capital_usd"), 0.0)
    if configured_sleeve_capital <= 0.0 or abs(float(sleeve_capital) - configured_sleeve_capital) > 1e-9:
        raise ValueError("maker_reward_sleeve_capital_policy_mismatch")
    max_order_notional_usd = max(0.0, float(sleeve_capital)) * max(
        0.0, float((cfg.get("risk") or {}).get("max_order_fraction_of_sleeve", 0.0))
    )
    max_quote_shares = max(0.0, float(selection_cfg.get("reward_max_quote_shares", 100.0)))
    if max_order_notional_usd <= 0.0 or max_quote_shares <= 0.0:
        raise ValueError("maker_reward_qualification_budget_invalid")
    flow_cfg = selection_cfg.get("recent_flow") if isinstance(selection_cfg.get("recent_flow"), dict) else {}
    if (
        flow_cfg.get("enabled") is True
        and fallback_universe_path is not None
        and (live_flow_path is not None or trade_tape_path is not None)
    ):
        flow_wait_seconds = max(0.0, float(flow_cfg.get("initial_wait_seconds", 0.0)))
        flow_deadline = time.monotonic() + flow_wait_seconds
        flow_error = "maker_recent_flow_unavailable"
        while True:
            try:
                return _recent_flow_snapshot(
                    fallback_universe_path, trade_tape_path, selection_cfg, capacity_cfg,
                    resource_capacity, model_sha=model_sha,
                    now_ms=time.time_ns() // 1_000_000 if now_ms is None else int(now_ms),
                    live_flow_path=live_flow_path,
                )
            except Exception as error:
                flow_error = f"{type(error).__name__}:{error}"[:500]
                if time.monotonic() >= flow_deadline:
                    break
                time.sleep(min(1.0, max(0.0, flow_deadline - time.monotonic())))
        # The reward catalog does not carry Gamma's timed-sports authority
        # fields.  Falling through to it here would silently reintroduce live
        # sports pick-off risk, so use the same fresh, exact-SHA universe with
        # the fail-closed generic-maker filter instead.
        return _fallback_snapshot(
            fallback_universe_path, selection_cfg, capacity_cfg, resource_capacity,
            model_sha=model_sha, primary_error=flow_error,
            now_ms=time.time_ns() // 1_000_000 if now_ms is None else int(now_ms),
        )
    try:
        return _primary_snapshot(
            cfg, selection_cfg, capacity_cfg, resource_capacity,
            model_sha=model_sha, deadline_seconds=deadline_seconds,
            request_timeout_seconds=request_timeout_seconds,
            max_pool_pages=max_pool_pages, max_market_pages=max_market_pages,
            max_order_notional_usd=max_order_notional_usd,
            max_quote_shares=max_quote_shares,
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


def _validated_pinned_selection(path: Path, *, model_sha: str) -> dict[str, Any]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    markets = snapshot.get("markets") if isinstance(snapshot.get("markets"), list) else []
    if (
        snapshot.get("schema") != "polymarket_v7_maker_reward_selection_v1"
        or snapshot.get("paper_only") is not True
        or snapshot.get("authenticated_execution") is not False
        or snapshot.get("real_order_submission") is not False
        or snapshot.get("model_sha") != model_sha
        or snapshot.get("source") not in {
            "public_clob_rewards", "adaptive_universe_fallback", "adaptive_universe_recent_flow",
        }
        or not markets
        or int(snapshot.get("selected_count") or 0) != len(markets)
        or len(markets) > int(snapshot.get("resource_capacity_markets") or 0)
        or any(
            not all(str(row.get(key) or "") for key in (
                "condition_id", "market_id", "yes_token", "no_token",
            ))
            or str(row.get("yes_token")) == str(row.get("no_token"))
            for row in markets
            if isinstance(row, dict)
        )
        or any(not isinstance(row, dict) for row in markets)
    ):
        raise ValueError("maker_pinned_runtime_selection_invalid")
    return snapshot


def publish_runtime_selection(
    candidate: dict[str, Any],
    output_path: Path,
    *,
    pin_runtime_selection: bool,
    candidate_output_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    if candidate_output_path is not None:
        atomic_json(candidate_output_path, candidate)
    pinned = pin_runtime_selection and output_path.is_file()
    if pinned:
        runtime = _validated_pinned_selection(
            output_path, model_sha=str(candidate.get("model_sha") or "")
        )
    else:
        atomic_json(output_path, candidate)
        runtime = candidate
    return runtime, pinned


def selector_status(
    snapshot: dict[str, Any],
    *,
    candidate_snapshot: dict[str, Any] | None = None,
    runtime_selection_pinned: bool = False,
) -> dict[str, Any]:
    candidate = snapshot if candidate_snapshot is None else candidate_snapshot
    runtime_membership = selection_membership_sha256(snapshot)
    candidate_membership = selection_membership_sha256(candidate)
    candidate_markets = candidate.get("markets") if isinstance(candidate.get("markets"), list) else []
    candidate_flow_eligible = (
        candidate.get("source") == "adaptive_universe_recent_flow"
        and candidate.get("degraded") is not True
    )
    candidate_rotation_suppressed = bool(
        runtime_selection_pinned
        and candidate.get("degraded") is True
        and runtime_membership != candidate_membership
    )
    candidate_last_sell_ages = [
        max(0.0, finite(row.get("recent_last_sell_age_ms")) / 1_000.0)
        for row in candidate_markets
        if isinstance(row, dict) and finite(row.get("recent_last_sell_age_ms"), -1.0) >= 0.0
    ]
    return {
        "schema": SELECTOR_STATUS_SCHEMA,
        "timestamp_ms": candidate.get("timestamp_ms"),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "model_sha": snapshot.get("model_sha"),
        "state": (
            "OPERATIONAL_FALLBACK" if candidate.get("degraded")
            else "OPERATIONAL_BILATERAL_FLOW"
            if candidate.get("source") == "adaptive_universe_recent_flow"
            else "OPERATIONAL_REWARDED"
        ),
        "ready": True,
        "degraded": candidate.get("degraded") is True,
        "source": candidate.get("source"),
        "selected_count": snapshot.get("selected_count", 0),
        "reward_pool_count": candidate.get("reward_pool_count", 0),
        "reward_market_count": candidate.get("reward_market_count", 0),
        "primary_error": candidate.get("primary_error", ""),
        "runtime_selection_pinned": runtime_selection_pinned,
        "runtime_membership_sha256": runtime_membership,
        "candidate_membership_sha256": candidate_membership,
        "candidate_rotation_pending": (
            not candidate_rotation_suppressed
            and runtime_membership != candidate_membership
        ),
        "candidate_rotation_suppressed_no_fresh_flow": candidate_rotation_suppressed,
        "candidate_source": candidate.get("source"),
        "candidate_degraded": candidate.get("degraded") is True,
        "candidate_selected_count": candidate.get("selected_count", 0),
        "candidate_fresh_flow_eligible": candidate_flow_eligible,
        "candidate_selected_bilateral": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "BILATERAL"
        ),
        "candidate_selected_inventory_backed_ask": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "INVENTORY_BACKED_ASK"
        ),
        "candidate_selected_collateral_backed_bid": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "COLLATERAL_BACKED_BID"
        ),
        "candidate_selected_stable_spread_exploration": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and row.get("side_mode") == "STABLE_SPREAD_EXPLORATION"
        ),
        "candidate_selected_with_buy_flow_30s": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_buy_prints_30s") or 0) > 0
        ),
        "candidate_selected_with_buy_flow_2m": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_buy_prints_2m") or 0) > 0
        ),
        "candidate_selected_with_sell_flow_30s": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_sell_prints_30s") or 0) > 0
        ),
        "candidate_selected_with_sell_flow_2m": sum(
            1 for row in candidate_markets
            if isinstance(row, dict) and int(row.get("recent_sell_prints_2m") or 0) > 0
        ),
        "candidate_max_last_sell_age_seconds": (
            max(candidate_last_sell_ages) if candidate_last_sell_ages else -1.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_professional_market_maker.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path)
    parser.add_argument("--pin-runtime-selection", action="store_true")
    parser.add_argument("--status", type=Path)
    parser.add_argument("--fallback-universe", type=Path)
    parser.add_argument("--trade-tape", type=Path)
    parser.add_argument("--live-flow", type=Path)
    parser.add_argument("--allocation", type=Path)
    parser.add_argument("--model-sha", default="")
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--request-timeout-seconds", type=float)
    args = parser.parse_args()
    snapshot = build_snapshot(
        args.config,
        fallback_universe_path=args.fallback_universe,
        trade_tape_path=args.trade_tape,
        live_flow_path=args.live_flow,
        allocation_path=args.allocation,
        model_sha=args.model_sha,
        deadline_seconds=args.deadline_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    runtime_snapshot, pinned = publish_runtime_selection(
        snapshot,
        args.output,
        pin_runtime_selection=args.pin_runtime_selection,
        candidate_output_path=args.candidate_output,
    )
    if args.status is not None:
        atomic_json(args.status, selector_status(
            runtime_snapshot,
            candidate_snapshot=snapshot,
            runtime_selection_pinned=pinned,
        ))
    print(json.dumps(runtime_snapshot, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
