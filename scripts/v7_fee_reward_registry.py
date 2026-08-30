#!/usr/bin/env python3
"""Build the exact-SHA fee/reward evidence registry used by V7 PAPER.

Unknown fees make a market non-executable. Unknown rewards are valued at zero.
The registry is evidence and policy, never an execution or accounting writer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any


SHA40 = re.compile(r"^[0-9a-f]{40}$")


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _fee(market: dict[str, Any], now_ms: int, ttl_ms: int) -> dict[str, Any]:
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    rate = finite(schedule.get("rate"))
    exponent = finite(schedule.get("exponent"), 1.0)
    if math.isfinite(rate) and rate >= 0.0 and math.isfinite(exponent) and exponent >= 0.0:
        return {
            "verified": True, "enabled": rate > 0.0, "rate": rate,
            "exponent": exponent, "taker_only": bool(schedule.get("takerOnly", True)),
            "source": "gamma:feeSchedule", "observed_at_ms": now_ms,
            "expires_at_ms": now_ms + ttl_ms, "confidence": 1.0,
            "formula": "rate*(price*(1-price))**exponent for taker fills",
        }
    if market.get("fees_enabled_explicit") is True and market.get("fees_enabled") is False:
        return {
            "verified": True, "enabled": False, "rate": 0.0, "exponent": 1.0,
            "taker_only": True, "source": "gamma:fees_disabled",
            "observed_at_ms": now_ms, "expires_at_ms": now_ms + ttl_ms,
            "confidence": 1.0, "formula": "zero: authoritative feesEnabled=false",
        }
    return {
        "verified": False, "enabled": None, "rate": None, "exponent": None,
        "taker_only": None, "source": "unverified_fee_schedule",
        "observed_at_ms": now_ms, "expires_at_ms": now_ms,
        "confidence": 0.0, "formula": None,
    }


def _reward(row: dict[str, Any] | None, snapshot: dict[str, Any], now_ms: int,
            ttl_ms: int) -> dict[str, Any]:
    public = snapshot.get("source") == "public_clob_rewards" and snapshot.get("reward_data_available") is True
    max_spread = finite((row or {}).get("rewards_max_spread_cents"))
    minimum = finite((row or {}).get("rewards_min_size"))
    daily = finite((row or {}).get("total_daily_rate"))
    verified = bool(public and row and max_spread > 0.0 and minimum > 0.0 and daily > 0.0)
    if not verified:
        return {
            "verified": False, "eligible": False, "expected_value_usd": 0.0,
            "maximum_spread_cents": None, "minimum_quote_shares": None,
            "pool_daily_rate_usd": 0.0, "source": "unknown_reward_forced_zero",
            "observed_at_ms": now_ms, "expires_at_ms": now_ms,
            "confidence": 0.0, "scoring_formula": None,
            "payout_status": "NOT_ATTRIBUTED",
        }
    return {
        "verified": True,
        "eligible": (row or {}).get("reward_touch_qualifies_at_selection") is True,
        "expected_value_usd": 0.0,
        "maximum_spread_cents": max_spread, "minimum_quote_shares": minimum,
        "pool_daily_rate_usd": daily, "source": "public_clob_rewards",
        "observed_at_ms": int(snapshot.get("timestamp_ms") or now_ms),
        "expires_at_ms": now_ms + ttl_ms, "confidence": 1.0,
        "scoring_formula": "relative liquidity score; pool dollars are not guaranteed maker payout",
        "payout_status": "UNREALIZED_COMPETITION_DEPENDENT",
    }


def build(universe: dict[str, Any], rewards: dict[str, Any], *, model_sha: str,
          now_ms: int, fee_ttl_seconds: int = 300,
          reward_ttl_seconds: int = 120) -> dict[str, Any]:
    if not SHA40.fullmatch(model_sha):
        raise ValueError("model_sha:not_exact")
    if universe.get("schema") != "polymarket_v7_adaptive_universe_snapshot_v1":
        raise ValueError("universe:schema")
    if universe.get("model_sha") != model_sha or universe.get("paper_only") is not True:
        raise ValueError("universe:identity_or_safety")
    if universe.get("authenticated_execution") is not False or universe.get("real_order_submission") is not False:
        raise ValueError("universe:execution_boundary")
    reward_rows = {
        str(row.get("condition_id") or ""): row
        for row in rewards.get("markets", []) if isinstance(row, dict)
    }
    entries: list[dict[str, Any]] = []
    for market in universe.get("markets", []):
        if not isinstance(market, dict):
            continue
        condition = str(market.get("condition_id") or "")
        market_id = str(market.get("market_id") or "")
        if not condition or not market_id:
            continue
        fee = _fee(market, now_ms, max(1, fee_ttl_seconds) * 1000)
        reward = _reward(reward_rows.get(condition), rewards, now_ms, max(1, reward_ttl_seconds) * 1000)
        active = market.get("active") is True and market.get("closed") is False and market.get("accepting_orders") is True
        entries.append({
            "market_id": market_id, "condition_id": condition,
            "token_ids": [str(value) for value in market.get("clob_token_ids", [])],
            "fee": fee, "reward": reward,
            "executable_under_registry": bool(active and fee["verified"]),
            "non_executable_reason": None if active and fee["verified"] else (
                "MARKET_INACTIVE" if not active else "UNKNOWN_FEE"
            ),
        })
    return {
        "schema": "polymarket_v7_fee_reward_registry_v1", "version": 7,
        "timestamp": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        "timestamp_ms": now_ms, "model_sha": model_sha,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "unknown_fee_policy": "NON_EXECUTABLE",
        "unknown_reward_policy": "ZERO_EXPECTED_VALUE",
        "automatic_promotion": False,
        "market_count": len(entries),
        "verified_fee_market_count": sum(1 for row in entries if row["fee"]["verified"]),
        "verified_reward_market_count": sum(1 for row in entries if row["reward"]["verified"]),
        "executable_market_count": sum(1 for row in entries if row["executable_under_registry"]),
        "markets": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build V7 fee/reward evidence registry")
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--rewards", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--interval", type=float, default=0.0)
    args = parser.parse_args()
    while True:
        try:
            result = build(load(args.universe), load(args.rewards), model_sha=args.model_sha,
                           now_ms=int(time.time() * 1000))
            atomic_json(args.output, result)
        except Exception as exc:
            atomic_json(args.output, {
                "schema": "polymarket_v7_fee_reward_registry_v1", "version": 7,
                "timestamp_ms": int(time.time() * 1000), "model_sha": args.model_sha,
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "execution_authority": False,
                "unknown_fee_policy": "NON_EXECUTABLE", "unknown_reward_policy": "ZERO_EXPECTED_VALUE",
                "automatic_promotion": False, "market_count": 0,
                "verified_fee_market_count": 0, "verified_reward_market_count": 0,
                "executable_market_count": 0, "markets": [], "error": str(exc),
            })
            if args.interval <= 0:
                raise
        if args.interval <= 0:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
