#!/usr/bin/env python3
"""Build the exact-SHA fee/reward evidence registry used by V7 PAPER.

Unknown fees make a market non-executable. Unknown rewards are valued at zero.
The registry is evidence and policy, never an execution or accounting writer.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
from typing import Any

try:
    from v7_pure_arb_economics import taker_tier
except ModuleNotFoundError:
    # Direct importlib-based tests do not add scripts/ to sys.path.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from v7_pure_arb_economics import taker_tier


SHA40 = re.compile(r"^[0-9a-f]{40}$")
EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
WALLET_REWARD_AUDIT_SCHEMA = "polymarket_v7_wallet_reward_audit_v1"


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



def _wallet_hash(user: str) -> str:
    return hashlib.sha256(user.lower().encode("ascii")).hexdigest()


def parse_wallet_reward_audit(payload: dict[str, Any], user: str, now_ms: int) -> dict[str, Any]:
    base = {
        "schema": WALLET_REWARD_AUDIT_SCHEMA,
        "configured": bool(user),
        "verified": False,
        "source": "polymarket_data_api_v2_user_pnl",
        "observed_at_ms": now_ms,
        "wallet_sha256": _wallet_hash(user) if EVM_ADDRESS.fullmatch(user) else None,
        "allocatable_to_market": False,
        "allocation_reason": "WALLET_LEVEL_NOT_MARKET_ATTRIBUTABLE",
        "maker_rebate_cumulative_pusd": 0.0,
        "reward_income_cumulative_pusd": 0.0,
        "sponsored_income_cumulative_pusd": 0.0,
        "maker_rebate_delta_pusd": 0.0,
        "reward_income_delta_pusd": 0.0,
        "sponsored_income_delta_pusd": 0.0,
        "delta_seconds": None,
    }
    if not EVM_ADDRESS.fullmatch(user):
        base["reason"] = "WALLET_UNCONFIGURED_OR_INVALID"
        return base
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or str(data.get("proxy_wallet") or "").lower() != user.lower():
        base["reason"] = "DATA_IDENTITY_MISMATCH"
        return base
    points = data.get("points")
    if not isinstance(points, list):
        base["reason"] = "POINTS_MISSING"
        return base

    clean: list[dict[str, float]] = []
    for row in points:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = int(row.get("timestamp") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp <= 0:
            continue
        maker = finite(row.get("maker_rebate"), 0.0)
        reward = finite(row.get("reward_income"), 0.0)
        sponsored = finite(row.get("sponsored_income"), 0.0)
        if not all(math.isfinite(x) for x in (maker, reward, sponsored)):
            continue
        clean.append({
            "timestamp": float(timestamp),
            "maker_rebate": maker,
            "reward_income": reward,
            "sponsored_income": sponsored,
        })
    clean.sort(key=lambda row: row["timestamp"])
    if not clean:
        base["reason"] = "NO_PNL_POINTS"
        return base

    latest = clean[-1]
    previous = clean[-2] if len(clean) >= 2 else latest
    delta_seconds = max(0, int(latest["timestamp"] - previous["timestamp"]))
    base.update({
        "verified": True,
        "reason": None,
        "source_fidelity": str(data.get("source_fidelity") or ""),
        "interval": str(data.get("interval") or ""),
        "fidelity": str(data.get("fidelity") or ""),
        "source_timestamp_s": int(latest["timestamp"]),
        "maker_rebate_cumulative_pusd": latest["maker_rebate"],
        "reward_income_cumulative_pusd": latest["reward_income"],
        "sponsored_income_cumulative_pusd": latest["sponsored_income"],
        "maker_rebate_delta_pusd": latest["maker_rebate"] - previous["maker_rebate"],
        "reward_income_delta_pusd": latest["reward_income"] - previous["reward_income"],
        "sponsored_income_delta_pusd": latest["sponsored_income"] - previous["sponsored_income"],
        "delta_seconds": delta_seconds,
    })
    return base


def fetch_wallet_reward_audit(
    base_url: str, user: str, *, now_ms: int, timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    if not EVM_ADDRESS.fullmatch(user):
        return parse_wallet_reward_audit({}, user, now_ms)
    query = urllib.parse.urlencode({"user": user, "interval": "1d", "fidelity": "1h"})
    url = base_url.rstrip("/") + "/v2/user-pnl?" + query
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "polymarket-v7-reward-audit/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            value = json.load(response)
        return parse_wallet_reward_audit(value if isinstance(value, dict) else {}, user, now_ms)
    except Exception as exc:
        result = parse_wallet_reward_audit({}, user, now_ms)
        result["reason"] = "DATA_API_" + type(exc).__name__.upper()
        return result


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
            ttl_ms: int, exchange_semantics: dict[str, Any] | None = None) -> dict[str, Any]:
    # Rewards/rebates are ancillary PnL and can never rescue a negative entry
    # edge. A reward contribution becomes allocatable only from an explicit,
    # market-scoped, fresh, verified realized-PnL rate snapshot.
    semantics=exchange_semantics if isinstance(exchange_semantics,dict) else {}
    rebate=semantics.get("maker_rebates") if isinstance(semantics.get("maker_rebates"),dict) else {}
    try: reference_fraction=float(rebate.get("crypto_reference_fraction"))
    except (TypeError,ValueError): reference_fraction=math.nan
    reference_verified=(
        semantics.get("schema")=="polymarket_v7_exchange_semantics_v1"
        and math.isfinite(reference_fraction) and 0<=reference_fraction<=1
        and rebate.get("use_in_entry_gate") is False
    )

    verified=False
    realized_rate=0.0
    source="unknown_reward_forced_zero"
    observed=now_ms
    expires=now_ms
    if (snapshot.get("schema")=="polymarket_v7_verified_maker_reward_snapshot_v1"
            and isinstance(row,dict) and row.get("verified") is True):
        try:
            observed=int(row.get("observed_at_ms") or 0)
            expires=int(row.get("expires_at_ms") or 0)
            realized_rate=float(row.get("realized_pnl_pusd_per_capital_second"))
        except (TypeError,ValueError):
            realized_rate=math.nan
        verified=(
            observed>0 and observed<=now_ms<=expires
            and math.isfinite(realized_rate) and realized_rate>=0.0
        )
        if verified:
            source="verified_realized_maker_reward_rate"
        else:
            realized_rate=0.0
            observed=now_ms
            expires=now_ms

    return {
        "verified": verified, "eligible": verified, "expected_value_usd": 0.0,
        "realized_pnl_pusd_per_capital_second": realized_rate if verified else 0.0,
        "maximum_spread_cents": None, "minimum_quote_shares": None,
        "pool_daily_rate_usd": 0.0, "source": source,
        "observed_at_ms": observed, "expires_at_ms": expires,
        "confidence": 1.0 if verified else 0.0, "scoring_formula": None,
        "payout_status": "REALIZED_RATE_VERIFIED" if verified else "NOT_ATTRIBUTED",
        "maker_rebate_reference_fraction": reference_fraction if reference_verified else None,
        "maker_rebate_reference_verified": reference_verified,
        "maker_rebate_used_in_entry_gate": False,
        "reference_semantics": "ANCILLARY_ONLY_NOT_ENTRY_EDGE",
    }


def build(universe: dict[str, Any], rewards: dict[str, Any], *, model_sha: str,
          now_ms: int, fee_ttl_seconds: int = 300,
          reward_ttl_seconds: int = 120,
          exchange_semantics: dict[str, Any] | None = None,
          taker_tier_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    if not SHA40.fullmatch(model_sha):
        raise ValueError("model_sha:not_exact")
    if universe.get("schema") != "polymarket_v7_crypto_universe_snapshot_v1":
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
        reward = _reward(
            reward_rows.get(condition), rewards, now_ms,
            max(1, reward_ttl_seconds) * 1000, exchange_semantics)
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
    semantics = exchange_semantics if isinstance(exchange_semantics, dict) else {}
    taker_semantics = semantics.get("taker_rebates") if isinstance(semantics.get("taker_rebates"), dict) else {}
    tier_snapshot = taker_tier_snapshot if isinstance(taker_tier_snapshot, dict) else {}
    taker = {
        "verified": False,
        "source": "UNVERIFIED_TIER_ZERO_EXPECTED_VALUE",
        "weighted_volume_30d": None,
        "tier": None,
        "rebate_fraction": 0.0,
        "used_in_entry_gate": False,
        "category": "CRYPTO",
        "category_weight": float(taker_semantics.get("category_weight") or 2.3),
        "program_live_since": str(taker_semantics.get("program_live_since") or ""),
        "tier_schedule": taker_semantics.get("tiers") if isinstance(taker_semantics.get("tiers"), list) else [],
    }
    if (
        tier_snapshot.get("schema") == "polymarket_v7_verified_taker_tier_snapshot_v1"
        and tier_snapshot.get("model_sha") == model_sha
        and tier_snapshot.get("paper_only") is True
        and tier_snapshot.get("authenticated_execution") is False
        and tier_snapshot.get("real_order_submission") is False
    ):
        try:
            weighted = float(tier_snapshot.get("weighted_volume_30d"))
            observed = int(tier_snapshot.get("observed_at_ms") or 0)
            expires = int(tier_snapshot.get("expires_at_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            weighted, observed, expires = math.nan, 0, 0
        derived = taker_tier(weighted) if math.isfinite(weighted) and weighted >= 0 else {}
        try:
            claimed_fraction = float(tier_snapshot.get("rebate_fraction"))
        except (TypeError, ValueError, OverflowError):
            claimed_fraction = math.nan
        if (
            observed > 0 and observed <= now_ms <= expires
            and derived
            and math.isfinite(claimed_fraction)
            and abs(claimed_fraction - float(derived["rebate_fraction"])) <= 1e-12
            and str(tier_snapshot.get("tier") or "").upper() == str(derived["tier"])
        ):
            taker.update({
                "verified": True,
                "source": "VERIFIED_DAILY_TIER_SNAPSHOT",
                "weighted_volume_30d": weighted,
                "tier": derived["tier"],
                "rebate_fraction": claimed_fraction,
                "observed_at_ms": observed,
                "expires_at_ms": expires,
            })

    return {
        "schema": "polymarket_v7_fee_reward_registry_v1", "version": 8,
        "timestamp": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        "timestamp_ms": now_ms, "model_sha": model_sha,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "unknown_fee_policy": "NON_EXECUTABLE",
        "unknown_reward_policy": "ZERO_EXPECTED_VALUE",
        "market_count": len(entries),
        "verified_fee_market_count": sum(1 for row in entries if row["fee"]["verified"]),
        "verified_reward_market_count": sum(1 for row in entries if row["reward"]["verified"]),
        "executable_market_count": sum(1 for row in entries if row["executable_under_registry"]),
        "taker_rebate": taker,
        "markets": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build V7 fee/reward evidence registry")
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--rewards", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--exchange-semantics", type=Path)
    parser.add_argument("--data-api-url", default="https://data-api.polymarket.com")
    parser.add_argument("--wallet-reward-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--taker-tier-snapshot", type=Path)
    parser.add_argument("--interval", type=float, default=0.0)
    args = parser.parse_args()
    while True:
        try:
            now_ms = int(time.time() * 1000)
            result = build(
                load(args.universe), load(args.rewards), model_sha=args.model_sha,
                now_ms=now_ms,
                exchange_semantics=load(args.exchange_semantics) if args.exchange_semantics else None,
                taker_tier_snapshot=load(args.taker_tier_snapshot) if args.taker_tier_snapshot else None)
            proxy_wallet = str(os.environ.get("POLYMARKET_PROXY_WALLET") or "").strip()
            result["wallet_reward_audit"] = fetch_wallet_reward_audit(
                args.data_api_url, proxy_wallet, now_ms=now_ms,
                timeout_seconds=max(0.25, min(10.0, args.wallet_reward_timeout_seconds)))
            atomic_json(args.output, result)
        except Exception as exc:
            atomic_json(args.output, {
                "schema": "polymarket_v7_fee_reward_registry_v1", "version": 8,
                "timestamp_ms": int(time.time() * 1000), "model_sha": args.model_sha,
                "paper_only": True, "authenticated_execution": False,
                "real_order_submission": False, "execution_authority": False,
                "unknown_fee_policy": "NON_EXECUTABLE", "unknown_reward_policy": "ZERO_EXPECTED_VALUE",
                "market_count": 0,
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
