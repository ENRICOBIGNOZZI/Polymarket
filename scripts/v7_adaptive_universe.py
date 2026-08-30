#!/usr/bin/env python3
"""Exhaustive, resource-tiered active-market discovery for canonical V7 PAPER.

Gamma keyset pagination exhausts the declared liquid, recently traded domain.
HOT and WARM capacities are derived from declared resource budgets; COLD keeps
every remaining eligible market in that domain. This component owns metadata
discovery only and has no execution, capital, OMS, risk or ledger authority.
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

CONFIG_SCHEMA = "polymarket_v7_adaptive_universe_config_v1"
SNAPSHOT_SCHEMA = "polymarket_v7_adaptive_universe_snapshot_v1"
STATUS_SCHEMA = "polymarket_v7_adaptive_universe_status_v1"
CHANGE_SCHEMA = "polymarket_v7_adaptive_universe_change_v1"


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
    if config.get("schema") != CONFIG_SCHEMA or config.get("version") != 7:
        raise ValueError("invalid adaptive-universe schema/version")
    if config.get("paper_only") is not True:
        raise ValueError("adaptive universe must remain PAPER-only")
    if config.get("authenticated_execution") is not False or config.get("real_order_submission") is not False:
        raise ValueError("adaptive universe cannot have execution authority")
    source = config.get("source") if isinstance(config.get("source"), dict) else {}
    page_size = int(source.get("page_size", 0))
    guard = int(source.get("pagination_loop_guard_pages", 0))
    request_attempts = int(source.get("request_attempts_per_page", 0))
    if not str(source.get("gamma_url") or "").startswith("https://"):
        raise ValueError("source.gamma_url must use HTTPS")
    if not 1 <= page_size <= 100 or guard < 1 or request_attempts < 1:
        raise ValueError("invalid pagination controls")
    hot = ((config.get("resource_budget") or {}).get("hot") or {})
    warm = ((config.get("resource_budget") or {}).get("warm") or {})
    structural = ((config.get("resource_budget") or {}).get("structural") or {})
    positive = (
        hot.get("websocket_asset_capacity"), hot.get("assets_per_market"),
        hot.get("memory_budget_bytes"), hot.get("estimated_bytes_per_market"),
        hot.get("cpu_budget_micros_per_second"), hot.get("estimated_update_rate_hz_per_market"),
        hot.get("estimated_cpu_micros_per_update"), warm.get("scan_time_budget_millis"),
        warm.get("estimated_scan_millis_per_market"), warm.get("memory_budget_bytes"),
        warm.get("estimated_bytes_per_market"), structural.get("scan_time_budget_millis"),
        structural.get("estimated_event_scan_millis"),
    )
    if any(_finite(value) <= 0 for value in positive):
        raise ValueError("resource budgets and cost estimates must be positive")


def fetch_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-adaptive-universe/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    market_id = str(raw.get("id") or "").strip()
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    token_ids = [str(value).strip() for value in _array(raw.get("clobTokenIds")) if str(value).strip()]
    if not market_id:
        return None
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    first_event = next((row for row in events if isinstance(row, dict)), {})
    sports_market_type = str(
        raw.get("sportsMarketType") or first_event.get("sportsMarketType") or ""
    ).strip()
    game_start_time = str(
        raw.get("gameStartTime") or first_event.get("gameStartTime") or ""
    ).strip()
    seconds_delay = max(
        0, int(_finite(raw.get("secondsDelay"), _finite(first_event.get("secondsDelay"))))
    )
    # Gamma explicitly identifies timed sports through sportsMarketType and/or
    # gameStartTime. Preserve that venue fact in the canonical universe so a
    # generic CLOB-flow selector cannot accidentally make markets whose fair
    # value is driven by an unauthorised live score feed.
    timed_sports = bool(sports_market_type or game_start_time)
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
        "game_start_time": game_start_time,
        "sports_market_type": sports_market_type,
        "seconds_delay": seconds_delay,
        "timed_sports": timed_sports,
        "fee_schedule": raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else {},
        "fees_enabled": bool(raw.get("feesEnabled", False)),
        "liquidity": max(0.0, _finite(raw.get("liquidityNum"), _finite(raw.get("liquidity")))),
        "volume_24h": max(0.0, _finite(raw.get("volume24hr"), _finite(raw.get("volume24h")))),
        "created_at": str(raw.get("createdAt") or ""),
        "end_date": str(raw.get("endDate") or raw.get("end_date_iso") or ""),
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "accepting_orders": bool(raw.get("acceptingOrders", True)),
        "neg_risk": bool(raw.get("negRisk", False)),
    }


def discover_exhaustive(
    config: dict[str, Any], *, fetcher: Callable[[str, int], Any] = fetch_json
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = config["source"]
    page_size = int(source["page_size"])
    guard_pages = int(source["pagination_loop_guard_pages"])
    timeout = int(source.get("request_timeout_seconds", 20))
    request_attempts = max(1, int(source.get("request_attempts_per_page", 3)))
    gamma_url = str(source["gamma_url"]).rstrip("/")
    minimum_volume_24h = max(0.0, _finite(config["eligibility"].get("minimum_volume_24h_usd")))
    rows_by_id: dict[str, dict[str, Any]] = {}
    raw_rows = 0
    duplicate_rows = 0
    cursor = ""
    seen_cursors: set[str] = set()
    pages = 0
    request_retries = 0
    exhaustive = False
    started_ns = time.monotonic_ns()
    for _ in range(guard_pages):
        query_values = {
            "active": "true", "closed": "false", "limit": page_size,
            "order": "volume24hr", "ascending": "false",
            # The filter is identical to the local eligibility floor and only
            # removes rows that would be discarded after download. This keeps
            # the exact keyset walk bounded as the venue accumulates markets.
            "liquidity_num_min": _finite(config["eligibility"].get("minimum_liquidity_usd")),
        }
        if cursor:
            query_values["after_cursor"] = cursor
        query = urllib.parse.urlencode(query_values)
        for attempt in range(request_attempts):
            try:
                value = fetcher(gamma_url + "/markets/keyset?" + query, timeout)
                break
            except (OSError, TimeoutError):
                if attempt + 1 >= request_attempts:
                    raise
                request_retries += 1
                time.sleep(min(2.0, 0.25 * (2 ** attempt)))
        page_rows = value if isinstance(value, list) else value.get("markets", []) if isinstance(value, dict) else []
        pages += 1
        if not page_rows:
            exhaustive = True
            break
        raw_rows += len(page_rows)
        for raw in page_rows:
            if not isinstance(raw, dict):
                continue
            normalized = normalize_market(raw)
            if normalized is None:
                continue
            if normalized["market_id"] in rows_by_id:
                duplicate_rows += 1
            rows_by_id[normalized["market_id"]] = normalized
        # Keyset ordering is part of the cursor identity. Once an entire page
        # is below the declared 24h-flow eligibility floor, every subsequent
        # row is ineligible and the economically tradable domain is exhausted.
        if minimum_volume_24h > 0.0 and all(
            _finite(raw.get("volume24hr"), _finite(raw.get("volume24h"))) < minimum_volume_24h
            for raw in page_rows if isinstance(raw, dict)
        ):
            exhaustive = True
            break
        next_cursor = str(value.get("next_cursor") or "") if isinstance(value, dict) else ""
        if not next_cursor:
            exhaustive = True
            break
        if next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return list(rows_by_id.values()), {
        "discovery_exhaustive": exhaustive,
        "pages": pages,
        "raw_rows": raw_rows,
        "duplicate_rows": duplicate_rows,
        "request_retries": request_retries,
        "pagination_loop_guard_hit": not exhaustive,
        "scan_duration_ms": (time.monotonic_ns() - started_ns) / 1_000_000.0,
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
    structural = resources["structural"]
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
        "structural_scan_budget_events": max(1, int(
            _finite(structural["scan_time_budget_millis"]) // _finite(structural["estimated_event_scan_millis"])
        )),
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
        "source": "gamma_keyset_active_flow_eligible_exhaustive",
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
    markets, discovery = discover_exhaustive(config)
    snapshot = build_snapshot(markets, discovery, config, model_sha=model_sha, timestamp_ms=time.time_ns() // 1_000_000, previous=previous)
    if not snapshot["discovery_exhaustive"]:
        raise RuntimeError("Gamma pagination loop guard reached before exhaustion")
    persist(output_dir, snapshot, previous)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_adaptive_universe.json"))
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
