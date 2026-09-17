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
    if not str(source.get("clob_url") or "").startswith("https://"):
        raise ValueError("source.clob_url must use HTTPS")
    if int(source.get("clob_max_pages", 0)) < 1:
        raise ValueError("source.clob_max_pages must be positive")
    if int(source.get("metadata_cache_max_age_seconds", 0)) < 1:
        raise ValueError("source.metadata_cache_max_age_seconds must be positive")
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


def normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    market_id = str(raw.get("id") or raw.get("market_id") or condition_id).strip()
    token_ids = [str(value).strip() for value in _array(raw.get("clobTokenIds")) if str(value).strip()]
    if not token_ids and isinstance(raw.get("tokens"), list):
        token_ids = [str(row.get("token_id") or "").strip() for row in raw["tokens"] if isinstance(row, dict) and str(row.get("token_id") or "").strip()]
    if not market_id:
        return None
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    first_event = next((row for row in events if isinstance(row, dict)), {})
    seconds_delay = max(
        0, int(_finite(raw.get("secondsDelay"), _finite(first_event.get("secondsDelay"))))
    )
    event_ids = sorted({str(row.get("id")).strip() for row in events if isinstance(row, dict) and str(row.get("id") or "").strip()})
    outcomes = [str(value) for value in _array(raw.get("outcomes"))]
    outcome_prices = [min(1.0, max(0.0, _finite(value))) for value in _array(raw.get("outcomePrices"))]
    if isinstance(raw.get("tokens"), list):
        token_rows = [row for row in raw["tokens"] if isinstance(row, dict)]
        if not outcomes: outcomes = [str(row.get("outcome") or "") for row in token_rows]
        if not outcome_prices: outcome_prices = [min(1.0, max(0.0, _finite(row.get("price")))) for row in token_rows]
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
        "slug": str(raw.get("slug") or raw.get("market_slug") or ""),
        "clob_token_ids": token_ids,
        "outcomes": outcomes,
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
        "liquidity_known": any(key in raw for key in ("liquidityNum", "liquidity")),
        "volume_24h_known": any(key in raw for key in ("volume24hr", "volume24h")),
        "created_at": str(raw.get("createdAt") or ""),
        "end_date": str(raw.get("endDate") or raw.get("end_date_iso") or ""),
        "minimum_order_size": max(0.0, _finite(raw.get("minimum_order_size"), _finite(raw.get("minOrderSize")))),
        "minimum_tick_size": max(0.0, _finite(raw.get("minimum_tick_size"), _finite(raw.get("tickSize")))),
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "accepting_orders": bool(raw.get("acceptingOrders", raw.get("accepting_orders", True))),
        "neg_risk": bool(raw.get("negRisk", False)),
        "asset": str((raw.get("_crypto_context") or {}).get("asset") or ""),
        "horizon": str((raw.get("_crypto_context") or {}).get("horizon") or ""),
        "horizon_seconds": int(_finite((raw.get("_crypto_context") or {}).get("horizon_seconds"))),
        "contract_family": str((raw.get("_crypto_context") or {}).get("contract_family") or ""),
        "settlement_semantic_hash": str((raw.get("_crypto_context") or {}).get("settlement_semantic_hash") or ""),
        "research_only": bool((raw.get("_crypto_context") or {}).get("research_only", False)),
        "authority": str((raw.get("_crypto_context") or {}).get("authority") or ""),
        "window_start_unix": int(_finite((raw.get("_crypto_context") or {}).get("window_start_unix"))),
        "metadata_source": str(raw.get("_metadata_source") or "UNKNOWN"),
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
        if (not template or "{window_start_unix}" not in template or not horizon_slug
                or horizon_seconds <= 0 or len(semantic) != 64):
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
        boundary=(now // horizon) * horizon
        mapping=context["polymarket"]; template=str(mapping["slug_template"]); horizon_slug=str(mapping["horizon_slug"])
        for offset in offsets:
            window_start=boundary + offset*horizon
            slug=template.format(horizon_slug=horizon_slug,window_start_unix=window_start)
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


def _expected_crypto_slugs(
    config: dict[str, Any], registry: dict[str, Any], *, now_s: int,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    offsets=[int(x) for x in config["source"].get("window_offsets",[-1,0,1])]
    for context in _registered_contexts(registry):
        horizon=int(context["horizon_seconds"])
        boundary=(now_s // horizon) * horizon
        mapping=context["polymarket"]
        template=str(mapping["slug_template"]); horizon_slug=str(mapping["horizon_slug"])
        for offset in offsets:
            window_start=boundary + offset*horizon
            slug=template.format(horizon_slug=horizon_slug,window_start_unix=window_start)
            settlement = context.get("settlement") if isinstance(context.get("settlement"), dict) else {}
            output[slug]={
                "asset":context.get("asset"), "horizon":context.get("horizon"),
                "horizon_seconds":horizon, "contract_family":context.get("contract_family"),
                "settlement_semantic_hash":context.get("settlement_semantic_hash"),
                "research_only":context.get("research_only") is True, "authority":context.get("authority"),
                "window_start_unix":window_start,
                "resolution_source": str(settlement.get("stream_url") or "")
                    if context.get("settlement_mapping_verified") is True else "",
            }
    return output


def _clob_raw_market(raw: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    value=dict(raw)
    value["id"] = str(raw.get("id") or raw.get("condition_id") or "")
    value["slug"] = str(raw.get("market_slug") or raw.get("slug") or "")
    value["conditionId"] = str(raw.get("condition_id") or raw.get("conditionId") or "")
    value["clobTokenIds"] = [
        str(row.get("token_id") or "") for row in raw.get("tokens", [])
        if isinstance(row,dict) and str(row.get("token_id") or "")
    ]
    value["outcomes"] = [
        str(row.get("outcome") or "") for row in raw.get("tokens", []) if isinstance(row,dict)
    ]
    value["outcomePrices"] = [
        row.get("price") for row in raw.get("tokens", []) if isinstance(row,dict)
    ]
    value["endDate"] = raw.get("end_date_iso") or raw.get("endDate") or ""
    value["acceptingOrders"] = raw.get("accepting_orders", raw.get("acceptingOrders", False))
    value["minOrderSize"] = raw.get("minimum_order_size")
    value["tickSize"] = raw.get("minimum_tick_size")
    if context.get("resolution_source"):
        value["resolutionSource"] = context["resolution_source"]
    value["_crypto_context"] = dict(context)
    value["_metadata_source"] = "CLOB_MARKETS_PLUS_VERIFIED_REGISTRY"
    return value


def discover_crypto_resilient(
    config: dict[str, Any], registry: dict[str, Any], *, now_s: int | None = None,
    fetcher: Callable[[str, int], Any] = fetch_json,
    previous: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve configured crypto windows without making Gamma availability critical.

    Source order: public CLOB market index -> exact-slug Gamma fallback -> bounded
    last-good cache. Gamma enriches/discovers but no longer gates the live market
    identity. Missing liquidity/volume metadata never becomes an artificial zero;
    executable depth remains an admission-time CLOB-book concern.
    """
    source=config["source"]
    timeout=int(source.get("request_timeout_seconds",4))
    clob_url=str(source["clob_url"]).rstrip("/")
    gamma_url=str(source["gamma_url"]).rstrip("/")
    max_pages=max(1,int(source.get("clob_max_pages",64)))
    max_cache_age=max(1,int(source.get("metadata_cache_max_age_seconds",3600)))
    now=int(time.time() if now_s is None else now_s)
    expected=_expected_crypto_slugs(config,registry,now_s=now)
    rows_by_slug: dict[str,dict[str,Any]]={}
    clob_pages=0; gamma_requests=0; gamma_errors=0; cache_fallback=0
    pagination_loop_guard_hit=False; clob_error=""
    started_ns=time.monotonic_ns()

    cursor="MA=="; seen_cursors:set[str]=set(); clob_complete=False
    try:
        for _ in range(max_pages):
            if cursor in seen_cursors:
                pagination_loop_guard_hit=True
                break
            seen_cursors.add(cursor)
            query=urllib.parse.urlencode({"next_cursor":cursor})
            value=fetcher(clob_url+"/markets?"+query,timeout)
            clob_pages += 1
            page=value.get("data",[]) if isinstance(value,dict) else []
            for raw in page:
                if not isinstance(raw,dict): continue
                slug=str(raw.get("market_slug") or raw.get("slug") or "")
                context=expected.get(slug)
                if context is None: continue
                normalized=normalize_market(_clob_raw_market(raw,context))
                if normalized is not None: rows_by_slug[slug]=normalized
            next_cursor=str(value.get("next_cursor") or "") if isinstance(value,dict) else ""
            if next_cursor in ("", "LTE="):
                clob_complete=True
                break
            cursor=next_cursor
        else:
            pagination_loop_guard_hit=True
    except (OSError,TimeoutError,ValueError) as error:
        clob_error=f"{type(error).__name__}:{error}"

    # Exact-slug Gamma is now fallback only. One failed metadata host cannot
    # invalidate CLOB-resolved token identity or stop a running hot path.
    missing=[slug for slug in expected if slug not in rows_by_slug]
    for slug in missing:
        try:
            gamma_requests += 1
            query=urllib.parse.urlencode({"slug":slug})
            value=fetcher(gamma_url+"/markets?"+query,timeout)
            page=value if isinstance(value,list) else value.get("markets",[]) if isinstance(value,dict) else []
            raw=next((row for row in page if isinstance(row,dict) and str(row.get("slug") or "")==slug),None)
            if raw is None: continue
            raw=dict(raw); raw["_crypto_context"]=dict(expected[slug]); raw["_metadata_source"]="GAMMA_FALLBACK"
            normalized=normalize_market(raw)
            if normalized is not None: rows_by_slug[slug]=normalized
        except (OSError,TimeoutError,ValueError):
            gamma_errors += 1

    previous = previous or {}
    previous_age_s=max(0.0,(time.time_ns()//1_000_000-int(previous.get("timestamp_ms") or 0))/1000.0)
    if previous_age_s <= max_cache_age:
        previous_rows = previous.get("metadata_markets")
        if not isinstance(previous_rows, list):
            previous_rows = previous.get("markets", [])
        for row in previous_rows:
            if not isinstance(row,dict): continue
            slug=str(row.get("slug") or "")
            if slug in expected and slug not in rows_by_slug:
                cached=dict(row); cached["metadata_source"]="LAST_GOOD_CACHE"
                rows_by_slug[slug]=cached; cache_fallback += 1

    rows=list(rows_by_slug.values())
    missing_count=max(0,len(expected)-len(rows_by_slug))
    if clob_complete:
        mode="CLOB_PRIMARY"
    elif rows and cache_fallback:
        mode="DEGRADED_CACHE"
    elif rows:
        mode="GAMMA_FALLBACK"
    else:
        mode="UNAVAILABLE"
    exhaustive=clob_complete or (gamma_errors==0 and gamma_requests==len(missing))
    return rows, {
        "source_mode":mode,
        "discovery_exhaustive":bool(exhaustive and not pagination_loop_guard_hit),
        "pagination_loop_guard_hit":pagination_loop_guard_hit,
        "pages":clob_pages + gamma_requests,
        "candidate_requests":clob_pages + gamma_requests,
        "missing_markets":missing_count,
        "raw_rows":len(rows),
        "duplicate_rows":0,
        "request_retries":0,
        "clob_pages":clob_pages,
        "clob_complete":clob_complete,
        "clob_error":clob_error,
        "gamma_fallback_requests":gamma_requests,
        "gamma_fallback_errors":gamma_errors,
        "cache_fallback_markets":cache_fallback,
        "scan_duration_ms":(time.monotonic_ns()-started_ns)/1_000_000.0,
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
    if market.get("liquidity_known", True) and _finite(market.get("liquidity")) + 1e-12 < _finite(rules.get("minimum_liquidity_usd")):
        return "BELOW_MINIMUM_LIQUIDITY"
    if market.get("volume_24h_known", True) and _finite(market.get("volume_24h")) + 1e-12 < _finite(rules.get("minimum_volume_24h_usd")):
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
    metadata_markets = sorted(
        [dict(row) for row in markets if isinstance(row, dict)],
        key=lambda row: (str(row.get("slug") or ""), str(row.get("market_id") or "")),
    )
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
        "source": str(discovery.get("source_mode") or "UNKNOWN"),
        "discovery_exhaustive": bool(discovery.get("discovery_exhaustive")),
        "pagination_loop_guard_hit": bool(discovery.get("pagination_loop_guard_hit")),
        "pages": int(discovery.get("pages", 0)),
        "scan_duration_ms": _finite(discovery.get("scan_duration_ms")),
        "raw_rows": int(discovery.get("raw_rows", 0)),
        "duplicate_rows": int(discovery.get("duplicate_rows", 0)),
        "request_retries": int(discovery.get("request_retries", 0)),
        "missing_markets": int(discovery.get("missing_markets", 0)),
        "clob_pages": int(discovery.get("clob_pages", 0)),
        "clob_complete": bool(discovery.get("clob_complete", False)),
        "clob_error_present": bool(discovery.get("clob_error")),
        "gamma_fallback_requests": int(discovery.get("gamma_fallback_requests", 0)),
        "gamma_fallback_errors": int(discovery.get("gamma_fallback_errors", 0)),
        "cache_fallback_markets": int(discovery.get("cache_fallback_markets", 0)),
        "discovered_markets": len(markets),
        "eligible_markets": len(eligible),
        "skipped_markets": len(skipped),
        "skipped_by_reason": dict(sorted(skip_counts.items())),
        "resource_capacities": capacities,
        "tier_counts": {name: len(values) for name, values in tiers.items()},
        "tiers": tiers,
        "membership_sha256": hashlib.sha256(membership.encode("utf-8")).hexdigest(),
        "metadata_markets": metadata_markets,
        "markets": eligible,
        "skipped": sorted(skipped, key=lambda row: (row["reason"], row["market_id"])),
    }


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
        "missing_markets": snapshot.get("missing_markets", 0),
        "clob_pages": snapshot.get("clob_pages", 0),
        "clob_complete": snapshot.get("clob_complete", False),
        "clob_error_present": snapshot.get("clob_error_present", False),
        "gamma_fallback_requests": snapshot.get("gamma_fallback_requests", 0),
        "gamma_fallback_errors": snapshot.get("gamma_fallback_errors", 0),
        "cache_fallback_markets": snapshot.get("cache_fallback_markets", 0),
        "scan_duration_ms": snapshot.get("scan_duration_ms", 0.0),
        "membership_sha256": snapshot.get("membership_sha256", ""),
        "source": snapshot.get("source", "UNKNOWN"),
    }


def persist(output_dir: Path, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> None:
    previous = previous or {}
    changed = previous.get("membership_sha256") != snapshot.get("membership_sha256")
    _atomic_json(output_dir / "current.json", snapshot)
    _atomic_json(output_dir / "status.json", status_from_snapshot(snapshot))
    if changed:
        output_dir.mkdir(parents=True, exist_ok=True)
        change = {
            "schema": CHANGE_SCHEMA, "timestamp_ms": snapshot["timestamp_ms"],
            "model_sha": snapshot["model_sha"], "previous_membership_sha256": previous.get("membership_sha256", ""),
            "membership_sha256": snapshot["membership_sha256"], "tier_counts": snapshot["tier_counts"],
            "discovered_markets": snapshot["discovered_markets"], "eligible_markets": snapshot["eligible_markets"],
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        }
        with (output_dir / "changes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(change, sort_keys=True, separators=(",", ":")) + "\n")


def collect_once(config: dict[str, Any], output_dir: Path, model_sha: str) -> dict[str, Any]:
    previous = _load_json(output_dir / "current.json")
    registry = _load_json(Path(config["market_registry"]))
    markets, discovery = discover_crypto_resilient(config, registry, previous=previous)
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
