#!/usr/bin/env python3
"""Crypto-only active-market discovery for canonical V7 PAPER.

Only asset/horizon contexts registered in v7_crypto_settlement_markets.json are
queried. Exact rolling slugs replace the former whole-Polymarket keyset scan.
This component owns metadata discovery only and has no execution, capital, OMS,
risk or ledger authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

CONFIG_SCHEMA = "polymarket_v7_crypto_universe_config_v1"
SNAPSHOT_SCHEMA = "polymarket_v7_crypto_universe_snapshot_v1"
STATUS_SCHEMA = "polymarket_v7_crypto_universe_status_v1"
CHANGE_SCHEMA = "polymarket_v7_crypto_universe_change_v1"


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema") != CONFIG_SCHEMA or config.get("version") != 1:
        raise ValueError("invalid crypto-universe schema/version")
    if config.get("paper_only") is not True:
        raise ValueError("crypto universe must remain PAPER-only")
    if config.get("authenticated_execution") is not False or config.get("real_order_submission") is not False:
        raise ValueError("crypto universe cannot have execution authority")
    if config.get("market_registry") != "config/v7_crypto_settlement_markets.json":
        raise ValueError("crypto universe must bind the canonical crypto market registry")
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    attempts = int(source.get("request_attempts_per_market", 0))
    offsets = source.get("window_offsets")
    if not str(source.get("gamma_url") or "").startswith("https://"):
        raise ValueError("source.gamma_url must use HTTPS")
    if attempts < 1 or not isinstance(offsets, list) or not offsets or any(type(x) is not int for x in offsets):
        raise ValueError("invalid exact-market discovery controls")
    if 0 not in offsets or min(offsets) < -2 or max(offsets) > 2:
        raise ValueError("window_offsets must include current and remain tightly bounded")
    hot = ((config.get("resource_budget") or {}).get("hot") or {})
    warm = ((config.get("resource_budget") or {}).get("warm") or {})
    positive = (
        hot.get("websocket_asset_capacity"), hot.get("assets_per_market"),
        hot.get("memory_budget_bytes"), hot.get("estimated_bytes_per_market"),
        hot.get("cpu_budget_micros_per_second"), hot.get("estimated_update_rate_hz_per_market"),
        hot.get("estimated_cpu_micros_per_update"), warm.get("scan_time_budget_millis"),
        warm.get("estimated_scan_millis_per_market"), warm.get("memory_budget_bytes"),
        warm.get("estimated_bytes_per_market"),
    )
    if any(_finite(value) <= 0 for value in positive):
        raise ValueError("resource budgets and cost estimates must be positive")

def fetch_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-adaptive-universe/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


ET = ZoneInfo("America/New_York")
MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _iso_unix(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _slug_requests(context: dict[str, Any], now: int, offsets: list[int]) -> list[tuple[str, int]]:
    mapping = context["polymarket"]
    kind = str(mapping.get("slug_kind") or "")
    prefix = str(mapping.get("slug_prefix") or "")
    template = str(mapping.get("slug_template") or "")
    horizon = int(context["horizon_seconds"])
    horizon_slug = str(mapping.get("horizon_slug") or "")
    result: list[tuple[str, int]] = []

    if kind == "UNIX_WINDOW":
        boundary = (now // horizon) * horizon
        for offset in offsets:
            start = boundary + offset * horizon
            result.append((template.format(
                horizon_slug=horizon_slug, window_start_unix=start,
                slug_prefix=prefix,
            ), start))
        return result

    current_et = datetime.fromtimestamp(now, timezone.utc).astimezone(ET)
    if kind == "HOURLY_ET":
        boundary = current_et.replace(minute=0, second=0, microsecond=0)
        for offset in offsets:
            start_dt = boundary + timedelta(hours=offset)
            hour12 = start_dt.hour % 12 or 12
            result.append((template.format(
                slug_prefix=prefix, month=MONTHS[start_dt.month - 1],
                day=start_dt.day, year=start_dt.year, hour12=hour12,
                ampm="am" if start_dt.hour < 12 else "pm",
                horizon_slug=horizon_slug, window_start_unix=int(start_dt.timestamp()),
            ), int(start_dt.timestamp())))
        return result

    if kind == "DAILY_ET":
        today_noon = current_et.replace(hour=12, minute=0, second=0, microsecond=0)
        base_end = today_noon if current_et < today_noon else today_noon + timedelta(days=1)
        for offset in offsets:
            end_dt = base_end + timedelta(days=offset)
            start_dt = end_dt - timedelta(days=1)
            result.append((template.format(
                slug_prefix=prefix, month=MONTHS[end_dt.month - 1],
                day=end_dt.day, year=end_dt.year, hour12=12, ampm="pm",
                horizon_slug=horizon_slug, window_start_unix=int(start_dt.timestamp()),
            ), int(start_dt.timestamp())))
        return result

    raise ValueError(f"unsupported crypto slug kind:{kind}")


def normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    market_id = str(raw.get("id") or "").strip()
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    token_ids = [str(value).strip() for value in _array(raw.get("clobTokenIds")) if str(value).strip()]
    if not market_id:
        return None
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    first_event = next((row for row in events if isinstance(row, dict)), {})
    seconds_delay = max(
        0, int(_finite(raw.get("secondsDelay"), _finite(first_event.get("secondsDelay"))))
    )
    event_ids = sorted({str(row.get("id")).strip() for row in events if isinstance(row, dict) and str(row.get("id") or "").strip()})
    outcome_prices = [min(1.0, max(0.0, _finite(value))) for value in _array(raw.get("outcomePrices"))]
    best_bid = min(1.0, max(0.0, _finite(raw.get("bestBid"))))
    best_ask = min(1.0, max(0.0, _finite(raw.get("bestAsk"))))
    midpoint = (
        (best_bid + best_ask) / 2.0
        if best_bid > 0.0 and best_ask >= best_bid
        else outcome_prices[0] if outcome_prices else 0.0
    )
    return {
        "market_id": market_id,
        "condition_id": condition_id,
        "event_ids": event_ids,
        "question": str(raw.get("question") or ""),
        "description": str(raw.get("description") or ""),
        "slug": str(raw.get("slug") or ""),
        "clob_token_ids": token_ids,
        "outcomes": [str(value) for value in _array(raw.get("outcomes"))],
        "outcome_prices": outcome_prices,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "midpoint": midpoint,
        "spread": max(0.0, _finite(raw.get("spread"), best_ask - best_bid)),
        "last_trade_price": min(1.0, max(0.0, _finite(raw.get("lastTradePrice")))),
        "resolution_source": str(raw.get("resolutionSource") or ""),
        "event_start_time": str(raw.get("eventStartTime") or ""),
        "seconds_delay": seconds_delay,
        "fee_schedule": raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else {},
        "fees_enabled": bool(raw.get("feesEnabled", False)),
        "fees_enabled_explicit": "feesEnabled" in raw,
        "liquidity": max(0.0, _finite(raw.get("liquidityNum"), _finite(raw.get("liquidity")))),
        "volume_24h": max(0.0, _finite(raw.get("volume24hr"), _finite(raw.get("volume24h")))),
        "created_at": str(raw.get("createdAt") or ""),
        "end_date": str(raw.get("endDate") or raw.get("end_date_iso") or ""),
        "close_timestamp_unix": _iso_unix(raw.get("endDate") or raw.get("end_date_iso")),
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "accepting_orders": bool(raw.get("acceptingOrders", True)),
        "neg_risk": bool(raw.get("negRisk", False)),
        "asset": str((raw.get("_crypto_context") or {}).get("asset") or ""),
        "horizon": str((raw.get("_crypto_context") or {}).get("horizon") or ""),
        "horizon_seconds": int(_finite((raw.get("_crypto_context") or {}).get("horizon_seconds"))),
        "contract_family": str((raw.get("_crypto_context") or {}).get("contract_family") or ""),
        "settlement_semantic_hash": str((raw.get("_crypto_context") or {}).get("settlement_semantic_hash") or ""),
        "research_only": bool((raw.get("_crypto_context") or {}).get("research_only", False)),
        "authority": str((raw.get("_crypto_context") or {}).get("authority") or ""),
        "external_symbols": (raw.get("_crypto_context") or {}).get("external_symbols")
            if isinstance((raw.get("_crypto_context") or {}).get("external_symbols"), dict) else {},
        "window_start_unix": int(_finite((raw.get("_crypto_context") or {}).get("window_start_unix"))),
    }


def _registered_contexts(registry: dict[str, Any]) -> list[dict[str, Any]]:
    if (registry.get("schema") != "polymarket_v7_crypto_settlement_market_registry_v1"
            or registry.get("paper_only") is not True
            or registry.get("authenticated_execution") is not False
            or registry.get("real_order_submission") is not False):
        raise ValueError("invalid crypto market registry")
    rows = registry.get("contexts")
    if not isinstance(rows, list):
        raise ValueError("crypto market registry contexts missing")
    output=[]
    for row in rows:
        if not isinstance(row, dict) or row.get("enabled") is not True:
            continue
        mapping=row.get("polymarket") if isinstance(row.get("polymarket"),dict) else {}
        template=str(mapping.get("slug_template") or "")
        horizon_slug=str(mapping.get("horizon_slug") or "")
        horizon_seconds=int(row.get("horizon_seconds") or 0)
        semantic=str(row.get("settlement_semantic_hash") or "")
        slug_kind = str(mapping.get("slug_kind") or "")
        prefix = str(mapping.get("slug_prefix") or "")
        required_fields = {
            "UNIX_WINDOW": "{window_start_unix}",
            "HOURLY_ET": "{hour12}",
            "DAILY_ET": "{day}",
        }
        if (not template or slug_kind not in required_fields
                or required_fields[slug_kind] not in template or not prefix
                or not horizon_slug or horizon_seconds <= 0 or len(semantic) != 64):
            raise ValueError("invalid registered crypto context")
        output.append(row)
    if not output:
        raise ValueError("no enabled crypto contexts")
    return output


def discover_crypto(
    config: dict[str, Any], registry: dict[str, Any], *, now_s: int | None = None,
    fetcher: Callable[[str, int], Any] = fetch_json,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source=config["source"]; gamma_url=str(source["gamma_url"]).rstrip("/")
    timeout=int(source.get("request_timeout_seconds",4))
    attempts=max(1,int(source.get("request_attempts_per_market",3)))
    offsets=[int(x) for x in source.get("window_offsets",[-1,0,1])]
    now=int(time.time() if now_s is None else now_s)
    rows_by_id: dict[str,dict[str,Any]]={}
    request_retries=0; requests=0; missing=0; raw_rows=0
    started_ns=time.monotonic_ns()
    for context in _registered_contexts(registry):
        horizon=int(context["horizon_seconds"])
        for slug, window_start in _slug_requests(context, now, offsets):
            query=urllib.parse.urlencode({"slug":slug})
            value=None
            for attempt in range(attempts):
                try:
                    requests += 1
                    value=fetcher(gamma_url+"/markets?"+query,timeout)
                    break
                except (OSError,TimeoutError):
                    if attempt+1>=attempts:
                        raise
                    request_retries += 1
                    time.sleep(min(1.0,0.1*(2**attempt)))
            page=value if isinstance(value,list) else value.get("markets",[]) if isinstance(value,dict) else []
            if not page:
                missing += 1
                continue
            raw_rows += len(page)
            raw=next((x for x in page if isinstance(x,dict) and str(x.get("slug") or "")==slug),None)
            if raw is None:
                raw=next((x for x in page if isinstance(x,dict)),None)
            if raw is None:
                missing += 1
                continue
            raw=dict(raw)
            raw["_crypto_context"]={
                "asset":context.get("asset"), "horizon":context.get("horizon"),
                "horizon_seconds":horizon, "contract_family":context.get("contract_family"),
                "settlement_semantic_hash":context.get("settlement_semantic_hash"),
                "research_only":context.get("research_only") is True, "authority":context.get("authority"),
                "external_symbols":context.get("external_symbols") if isinstance(context.get("external_symbols"),dict) else {},
                "window_start_unix":window_start,
            }
            normalized=normalize_market(raw)
            if normalized is not None:
                rows_by_id[normalized["market_id"]]=normalized
    return list(rows_by_id.values()), {
        "discovery_exhaustive": True, "pagination_loop_guard_hit": False,
        "pages": requests, "candidate_requests": requests, "missing_markets": missing,
        "raw_rows": raw_rows, "duplicate_rows": max(0,raw_rows-len(rows_by_id)),
        "request_retries": request_retries,
        "scan_duration_ms": (time.monotonic_ns()-started_ns)/1_000_000.0,
    }

def _eligibility(market: dict[str, Any], config: dict[str, Any]) -> str | None:
    rules = config["eligibility"]
    if rules.get("active_required") is True and market.get("active") is not True:
        return "INACTIVE"
    if rules.get("closed_forbidden") is True and market.get("closed") is True:
        return "CLOSED"
    if rules.get("accepting_orders_required") is True and market.get("accepting_orders") is not True:
        return "NOT_ACCEPTING_ORDERS"
    if not market.get("condition_id"):
        return "MISSING_CONDITION_ID"
    if len(market.get("clob_token_ids") or []) < int(rules.get("minimum_clob_tokens", 2)):
        return "MISSING_CLOB_TOKENS"
    if _finite(market.get("liquidity")) + 1e-12 < _finite(rules.get("minimum_liquidity_usd")):
        return "BELOW_MINIMUM_LIQUIDITY"
    if _finite(market.get("volume_24h")) + 1e-12 < _finite(rules.get("minimum_volume_24h_usd")):
        return "BELOW_MINIMUM_VOLUME_24H"
    return None


def resource_capacities(config: dict[str, Any], eligible_count: int) -> dict[str, Any]:
    resources = config["resource_budget"]
    hot = resources["hot"]
    warm = resources["warm"]
    hot_limits = {
        "websocket_assets": int(_finite(hot["websocket_asset_capacity"]) // _finite(hot["assets_per_market"])),
        "memory": int(_finite(hot["memory_budget_bytes"]) // _finite(hot["estimated_bytes_per_market"])),
        "cpu": int(_finite(hot["cpu_budget_micros_per_second"]) // (
            _finite(hot["estimated_update_rate_hz_per_market"]) * _finite(hot["estimated_cpu_micros_per_update"])
        )),
    }
    hot_capacity = min([max(0, eligible_count), *hot_limits.values()])
    warm_limits = {
        "scan_time": int(_finite(warm["scan_time_budget_millis"]) // _finite(warm["estimated_scan_millis_per_market"])),
        "memory": int(_finite(warm["memory_budget_bytes"]) // _finite(warm["estimated_bytes_per_market"])),
    }
    warm_capacity = min(max(0, eligible_count - hot_capacity), *warm_limits.values())
    return {
        "hot_capacity": hot_capacity,
        "warm_capacity": warm_capacity,
        "cold_capacity": max(0, eligible_count - hot_capacity - warm_capacity),
        "hot_limits": hot_limits,
        "warm_limits": warm_limits,
        "hot_limiting_dimensions": sorted(key for key, value in hot_limits.items() if value == hot_capacity),
        "warm_limiting_dimensions": sorted(key for key, value in warm_limits.items() if value == warm_capacity),
    }


def _score(market: dict[str, Any], prior_tier: str, config: dict[str, Any]) -> float:
    tiering = config["tiering"]
    weights = tiering["hot_score_weights"]
    bonus = tiering["prior_tier_hysteresis_bonus"]
    liquidity = math.log1p(_finite(market.get("liquidity")))
    volume = math.log1p(_finite(market.get("volume_24h")))
    # Venue order already supplies a causal recency/activity signal when values
    # tie; this bounded term keeps the score transparent and deterministic.
    recency = 1.0 if market.get("accepting_orders") else 0.0
    return (
        _finite(weights.get("log_liquidity")) * liquidity
        + _finite(weights.get("log_volume_24h")) * volume
        + _finite(weights.get("recency")) * recency
        + _finite(bonus.get(prior_tier))
    )


def build_snapshot(
    markets: list[dict[str, Any]], discovery: dict[str, Any], config: dict[str, Any],
    *, model_sha: str, timestamp_ms: int, previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha.lower()):
        raise ValueError("model_sha must be a 40-character hexadecimal SHA")
    previous = previous or {}
    prior_tiers = {
        str(row.get("market_id")): str(row.get("tier"))
        for row in previous.get("markets", []) if isinstance(row, dict)
    }
    skip_counts: dict[str, int] = {}
    skipped: list[dict[str, str]] = []
    eligible: list[dict[str, Any]] = []
    for market in markets:
        reason = _eligibility(market, config)
        if reason:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            skipped.append({"market_id": str(market["market_id"]), "reason": reason})
            continue
        row = dict(market)
        row["score"] = _score(row, prior_tiers.get(str(row["market_id"]), "COLD"), config)
        eligible.append(row)
    eligible.sort(key=lambda row: (-_finite(row["score"]), str(row["market_id"])))
    capacities = resource_capacities(config, len(eligible))
    hot_end = capacities["hot_capacity"]
    warm_end = hot_end + capacities["warm_capacity"]
    for index, row in enumerate(eligible):
        row["tier"] = "HOT" if index < hot_end else "WARM" if index < warm_end else "COLD"
    tiers = {name: [row["market_id"] for row in eligible if row["tier"] == name] for name in ("HOT", "WARM", "COLD")}
    membership = json.dumps(eligible, sort_keys=True, separators=(",", ":"))
    return {
        "schema": SNAPSHOT_SCHEMA,
        "version": 7,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "model_sha": model_sha.lower(),
        "timestamp_ms": int(timestamp_ms),
        "source": "gamma_exact_slug_configured_crypto_contexts",
        "discovery_exhaustive": bool(discovery.get("discovery_exhaustive")),
        "pagination_loop_guard_hit": bool(discovery.get("pagination_loop_guard_hit")),
        "pages": int(discovery.get("pages", 0)),
        "scan_duration_ms": _finite(discovery.get("scan_duration_ms")),
        "raw_rows": int(discovery.get("raw_rows", 0)),
        "duplicate_rows": int(discovery.get("duplicate_rows", 0)),
        "request_retries": int(discovery.get("request_retries", 0)),
        "discovered_markets": len(markets),
        "eligible_markets": len(eligible),
        "skipped_markets": len(skipped),
        "skipped_by_reason": dict(sorted(skip_counts.items())),
        "resource_capacities": capacities,
        "tier_counts": {name: len(values) for name, values in tiers.items()},
        "tiers": tiers,
        "membership_sha256": hashlib.sha256(membership.encode("utf-8")).hexdigest(),
        "markets": eligible,
        "skipped": sorted(skipped, key=lambda row: (row["reason"], row["market_id"])),
    }



BOOK_SELECTION_SCHEMA = "polymarket_v7_multi_crypto_book_selection_v1"
BOOK_ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
BOOK_HORIZONS = ("M5", "M15", "H1", "H4", "D1")
BOOK_CONTEXTS = {f"{asset}:{horizon}" for asset in BOOK_ASSETS for horizon in BOOK_HORIZONS}


def build_book_selection(snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Build the exact 30-context zero-authority PM book subscription."""
    if (
        snapshot.get("schema") != SNAPSHOT_SCHEMA
        or snapshot.get("paper_only") is not True
        or snapshot.get("authenticated_execution") is not False
        or snapshot.get("real_order_submission") is not False
        or snapshot.get("execution_authority") is not False
    ):
        return None, "UNIVERSE_CONTRACT_INVALID"
    model_sha = str(snapshot.get("model_sha") or "")
    if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha):
        return None, "MODEL_SHA_INVALID"
    try:
        now_s = int(snapshot.get("timestamp_ms") or 0) // 1000
    except (TypeError, ValueError, OverflowError):
        return None, "TIMESTAMP_INVALID"
    if now_s <= 0:
        return None, "TIMESTAMP_INVALID"

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot.get("markets") or []:
        if not isinstance(row, dict) or row.get("research_only") is True:
            continue
        asset = str(row.get("asset") or "")
        horizon = str(row.get("horizon") or "")
        context = f"{asset}:{horizon}"
        if context not in BOOK_CONTEXTS:
            continue
        if row.get("active") is not True or row.get("closed") is True or row.get("accepting_orders") is not True:
            continue
        start = int(row.get("window_start_unix") or 0)
        close = int(row.get("close_timestamp_unix") or 0)
        if close <= 0:
            close = start + int(row.get("horizon_seconds") or 0)
        if start <= 0 or close <= start or not (start <= now_s < close):
            continue
        grouped.setdefault(context, []).append(row)

    if set(grouped) != BOOK_CONTEXTS:
        missing = sorted(BOOK_CONTEXTS - set(grouped))
        return None, "MISSING_CONTEXTS:" + ",".join(missing)
    ambiguous = sorted(context for context, rows in grouped.items() if len(rows) != 1)
    if ambiguous:
        return None, "AMBIGUOUS_CONTEXTS:" + ",".join(ambiguous)

    markets: list[dict[str, Any]] = []
    for context in sorted(BOOK_CONTEXTS):
        row = grouped[context][0]
        tokens = [str(x) for x in row.get("clob_token_ids") or []]
        outcomes = [str(x).strip().upper() for x in row.get("outcomes") or []]
        if len(tokens) != 2 or not all(tokens) or tokens[0] == tokens[1]:
            return None, f"{context}:TOKEN_MAPPING_INVALID"
        yes_index, no_index = 0, 1
        for index, outcome in enumerate(outcomes[:2]):
            if outcome in {"YES", "UP"}:
                yes_index = index
            if outcome in {"NO", "DOWN"}:
                no_index = index
        if yes_index == no_index or yes_index >= len(tokens) or no_index >= len(tokens):
            yes_index, no_index = 0, 1
        start_s = int(row.get("window_start_unix") or 0)
        close_s = int(row.get("close_timestamp_unix") or 0)
        if close_s <= 0:
            close_s = start_s + int(row.get("horizon_seconds") or 0)
        event_ids = [str(x) for x in row.get("event_ids") or [] if str(x)]
        market_id = str(row.get("market_id") or "")
        if not market_id:
            return None, f"{context}:MARKET_ID_INVALID"
        markets.append({
            "asset": str(row.get("asset") or ""),
            "horizon": str(row.get("horizon") or ""),
            "market_id": market_id,
            "event_id": event_ids[0] if event_ids else "",
            "yes_token": tokens[yes_index],
            "no_token": tokens[no_index],
            "start_timestamp_ms": start_s * 1000,
            "end_timestamp_ms": close_s * 1000,
            "fee_schedule": row.get("fee_schedule") if isinstance(row.get("fee_schedule"), dict) else {},
            "fees_enabled": row.get("fees_enabled") is True,
            "fees_enabled_explicit": row.get("fees_enabled_explicit") is True,
        })
    identity = json.dumps(markets, sort_keys=True, separators=(",", ":"))
    return {
        "schema": BOOK_SELECTION_SCHEMA,
        "version": 1,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "selection_only": True,
        "active_only": True,
        "generated_at_ms": int(snapshot["timestamp_ms"]),
        "generation_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "market_count": len(markets),
        "token_count": 2 * len(markets),
        "markets": markets,
    }, ""


def status_from_snapshot(snapshot: dict[str, Any], *, state: str = "OPERATIONAL", blocker: str = "") -> dict[str, Any]:
    return {
        "schema": STATUS_SCHEMA,
        "version": 7,
        "timestamp_ms": snapshot.get("timestamp_ms"),
        "model_sha": snapshot.get("model_sha"),
        "state": state,
        "blocker": blocker,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "discovery_exhaustive": snapshot.get("discovery_exhaustive", False),
        "pagination_loop_guard_hit": snapshot.get("pagination_loop_guard_hit", False),
        "discovered_markets": snapshot.get("discovered_markets", 0),
        "eligible_markets": snapshot.get("eligible_markets", 0),
        "skipped_markets": snapshot.get("skipped_markets", 0),
        "skipped_by_reason": snapshot.get("skipped_by_reason", {}),
        "tier_counts": snapshot.get("tier_counts", {}),
        "resource_capacities": snapshot.get("resource_capacities", {}),
        "pages": snapshot.get("pages", 0),
        "request_retries": snapshot.get("request_retries", 0),
        "scan_duration_ms": snapshot.get("scan_duration_ms", 0.0),
        "membership_sha256": snapshot.get("membership_sha256", ""),
    }


def persist(output_dir: Path, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    previous = previous or {}
    changed = previous.get("membership_sha256") != snapshot.get("membership_sha256")
    _atomic_json(output_dir / "current.json", snapshot)

    selection, selection_blocker = build_book_selection(snapshot)
    selection_path = output_dir / "book_selection.json"
    prior_selection = _load_json(selection_path)
    if selection is not None:
        selection_unchanged = (
            prior_selection.get("schema") == BOOK_SELECTION_SCHEMA
            and prior_selection.get("model_sha") == selection.get("model_sha")
            and prior_selection.get("generation_sha256") == selection.get("generation_sha256")
            and prior_selection.get("paper_only") is True
            and prior_selection.get("execution_authority") is False
            and prior_selection.get("selection_only") is True
        )
        if not selection_unchanged:
            _atomic_json(selection_path, selection)
        selection_state = "READY"
        selection_contexts = int(selection["market_count"])
        selection_tokens = int(selection["token_count"])
    else:
        prior_safe = (
            prior_selection.get("schema") == BOOK_SELECTION_SCHEMA
            and prior_selection.get("model_sha") == snapshot.get("model_sha")
            and prior_selection.get("paper_only") is True
            and prior_selection.get("execution_authority") is False
            and prior_selection.get("selection_only") is True
        )
        selection_state = "STALE_PRESERVED" if prior_safe else "NOT_READY"
        selection_contexts = int(prior_selection.get("market_count") or 0) if prior_safe else 0
        selection_tokens = int(prior_selection.get("token_count") or 0) if prior_safe else 0

    status = status_from_snapshot(snapshot)
    status.update({
        "book_selection_state": selection_state,
        "book_selection_contexts": selection_contexts,
        "book_selection_tokens": selection_tokens,
        "book_selection_blocker": selection_blocker,
    })
    _atomic_json(output_dir / "status.json", status)
    if changed:
        output_dir.mkdir(parents=True, exist_ok=True)
        change = {
            "schema": CHANGE_SCHEMA, "timestamp_ms": snapshot["timestamp_ms"],
            "model_sha": snapshot["model_sha"], "previous_membership_sha256": previous.get("membership_sha256", ""),
            "membership_sha256": snapshot["membership_sha256"], "tier_counts": snapshot["tier_counts"],
            "discovered_markets": snapshot["discovered_markets"], "eligible_markets": snapshot["eligible_markets"],
            "book_selection_state": selection_state, "book_selection_contexts": selection_contexts,
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        }
        with (output_dir / "changes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(change, sort_keys=True, separators=(",", ":")) + "\n")


def collect_once(config: dict[str, Any], output_dir: Path, model_sha: str) -> dict[str, Any]:
    previous = _load_json(output_dir / "current.json")
    registry = _load_json(Path(config["market_registry"]))
    markets, discovery = discover_crypto(config, registry)
    snapshot = build_snapshot(markets, discovery, config, model_sha=model_sha, timestamp_ms=time.time_ns() // 1_000_000, previous=previous)
    persist(output_dir, snapshot, previous)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_crypto_universe.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    config = _load_json(args.config)
    validate_config(config)
    refresh = max(1, int(config["source"].get("refresh_seconds", 60)))
    initial = max(1.0, _finite(config["source"].get("retry_initial_seconds"), 1.0))
    maximum = max(initial, _finite(config["source"].get("retry_max_seconds"), 30.0))
    delay = initial
    while True:
        try:
            snapshot = collect_once(config, args.output_dir, args.model_sha)
            print(json.dumps(status_from_snapshot(snapshot), sort_keys=True), flush=True)
            delay = initial
            if not args.loop or args.once:
                return 0
            time.sleep(refresh)
        except Exception as error:
            now_ms = time.time_ns() // 1_000_000
            previous = _load_json(args.output_dir / "current.json")
            failure = status_from_snapshot(previous, state="BLOCKED_DISCOVERY", blocker=f"{type(error).__name__}:{error}")
            failure["timestamp_ms"] = now_ms
            failure["discovery_exhaustive"] = False
            _atomic_json(args.output_dir / "status.json", failure)
            print(json.dumps(failure, sort_keys=True), flush=True)
            if not args.loop or args.once:
                return 1
            time.sleep(delay + random.random() * min(1.0, delay / 4.0))
            delay = min(maximum, delay * 2.0)


if __name__ == "__main__":
    raise SystemExit(main())
