#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Any

import hf_active_flow_maker_batched_probe as batched
import hf_active_flow_maker_core as core
import hf_active_flow_maker_probe as entrypoint

_ORIGINAL_DISCOVER = entrypoint.discover_markets_activity_prior
_ORIGINAL_FETCH = entrypoint.fetch_trades_batch
_SEED_DIAGNOSTICS: dict[str, Any] = {}
_FLOW_CONDITIONS: set[str] = set()
_PRIOR_CONDITIONS: set[str] = set()
_SEED_CONDITIONS: set[str] = set()
_SEED_ADDED_CONDITIONS: set[str] = set()
_SEED_SLUGS: dict[str, str] = {}
_SEED_ASSETS: dict[str, set[str]] = defaultdict(set)
_TOKEN_TO_CANONICAL_CONDITION: dict[str, str] = {}
_CANONICAL_CONDITION_BY_RAW: dict[str, str] = {}
_SEED_RESOLUTION_MODE: dict[str, str] = {}
_CONDITION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _global_seed_query(start_ts: int, end_ts: int, limit: int = 1000) -> str:
    return urllib.parse.urlencode({
        "limit": max(1, min(1000, int(limit))),
        "offset": 0,
        "takerOnly": "true",
        "start": max(0, int(start_ts)),
        "end": max(0, int(end_ts)),
    })


def _condition_market_query(condition_ids: list[str]) -> str:
    pairs: list[tuple[str, str]] = [
        ("active", "true"),
        ("closed", "false"),
        ("limit", "100"),
    ]
    pairs.extend(("condition_ids", condition) for condition in condition_ids if condition)
    return urllib.parse.urlencode(pairs)


def _token_market_query(token_ids: list[str]) -> str:
    pairs: list[tuple[str, str]] = [
        ("active", "true"),
        ("closed", "false"),
        ("limit", "100"),
    ]
    pairs.extend(("clob_token_ids", token) for token in token_ids if token)
    return urllib.parse.urlencode(pairs)


def _slug_market_query(slug: str) -> str:
    return urllib.parse.urlencode({
        "active": "true",
        "closed": "false",
        "limit": 10,
        "slug": slug,
    })


def _global_trade_rows(start_ts: int, end_ts: int) -> tuple[list[dict[str, object]], int]:
    raw, received_ms = core.request_json(
        f"{core.DATA_URL}/trades?{_global_seed_query(start_ts, end_ts)}")
    rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("unexpected global trade response")
    return entrypoint._valid_trade_rows(rows, start_ts, end_ts), received_ms


def _recent_condition_ids(start_ts: int, end_ts: int, max_conditions: int) -> tuple[list[str], int, int]:
    global _SEED_SLUGS, _SEED_ASSETS
    valid, received_ms = _global_trade_rows(start_ts, end_ts)
    newest_by_condition: dict[str, int] = {}
    _SEED_SLUGS = {}
    _SEED_ASSETS = defaultdict(set)
    for item in valid:
        condition = str(item.get("conditionId") or "")
        slug = str(item.get("slug") or "")
        token = str(item.get("asset") or "")
        ts = int(core.number(item.get("timestamp"), 0))
        if not condition:
            continue
        newest_by_condition[condition] = max(ts, newest_by_condition.get(condition, 0))
        if slug:
            _SEED_SLUGS[condition] = slug
        if token:
            _SEED_ASSETS[condition].add(token)
    ordered = sorted(newest_by_condition, key=lambda c: (newest_by_condition[c], c), reverse=True)
    return ordered[:max(1, int(max_conditions))], received_ms, len(valid)


def _rows(raw: Any) -> list[dict[str, Any]]:
    values = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def _register_seed_market(raw_condition: str, market: core.Market, seen: set[str],
                          out: list[core.Market], mode: str) -> None:
    if market.market_id not in seen:
        seen.add(market.market_id)
        out.append(market)
    _CANONICAL_CONDITION_BY_RAW[raw_condition] = market.condition_id
    _SEED_RESOLUTION_MODE[raw_condition] = mode
    for token in (market.yes_token, market.no_token):
        _TOKEN_TO_CANONICAL_CONDITION[token] = market.condition_id


def _markets_for_conditions(condition_ids: list[str], min_liquidity: float,
                            batch_size: int = 20) -> tuple[list[core.Market], list[str]]:
    """Resolve recent Data-API trades to canonical Gamma markets.

    The Data API's conditionId is not assumed canonical. The CLOB outcome token in
    ``asset`` is the primary identity because Gamma officially supports the
    ``clob_token_ids`` market filter. Slug is a secondary identity and a canonical
    0x+64 condition id is only the final fallback.
    """
    global _TOKEN_TO_CANONICAL_CONDITION, _CANONICAL_CONDITION_BY_RAW, _SEED_RESOLUTION_MODE
    out: list[core.Market] = []
    seen: set[str] = set()
    errors: list[str] = []
    unique = list(dict.fromkeys(x for x in condition_ids if x))
    _TOKEN_TO_CANONICAL_CONDITION = {}
    _CANONICAL_CONDITION_BY_RAW = {}
    _SEED_RESOLUTION_MODE = {}

    unresolved: list[str] = []
    for raw_condition in unique:
        matched = False
        seed_assets = sorted(_SEED_ASSETS.get(raw_condition, set()))
        if seed_assets:
            for lo in range(0, len(seed_assets), max(1, batch_size)):
                chunk = seed_assets[lo:lo + max(1, batch_size)]
                try:
                    raw, _ = core.request_json(f"{core.GAMMA_URL}/markets?{_token_market_query(chunk)}")
                except Exception as exc:
                    errors.append(
                        f"seed_gamma_token_batch={lo // max(1, batch_size)}:{type(exc).__name__}:{exc}")
                    continue
                for item in _rows(raw):
                    market = core.parse_market(item, min_liquidity)
                    if market is None:
                        continue
                    tokens = {market.yes_token, market.no_token}
                    if not tokens.intersection(chunk):
                        continue
                    _register_seed_market(raw_condition, market, seen, out, "clob_token_ids")
                    matched = True
                    break
                if matched:
                    break

        slug = _SEED_SLUGS.get(raw_condition, "")
        if not matched and slug:
            try:
                raw, _ = core.request_json(f"{core.GAMMA_URL}/markets?{_slug_market_query(slug)}")
            except Exception as exc:
                errors.append(f"seed_gamma_slug={slug}:{type(exc).__name__}:{exc}")
            else:
                seed_assets = _SEED_ASSETS.get(raw_condition, set())
                for item in _rows(raw):
                    market = core.parse_market(item, min_liquidity)
                    if market is None or market.slug != slug:
                        continue
                    tokens = {market.yes_token, market.no_token}
                    if seed_assets and not tokens.intersection(seed_assets):
                        continue
                    _register_seed_market(raw_condition, market, seen, out, "slug_token")
                    matched = True
                    break
        if not matched:
            unresolved.append(raw_condition)

    valid_unresolved = [condition for condition in unresolved if _CONDITION_RE.fullmatch(condition)]
    for lo in range(0, len(valid_unresolved), max(1, batch_size)):
        chunk = valid_unresolved[lo:lo + max(1, batch_size)]
        try:
            raw, _ = core.request_json(f"{core.GAMMA_URL}/markets?{_condition_market_query(chunk)}")
        except Exception as exc:
            errors.append(f"seed_gamma_condition_batch={lo // max(1, batch_size)}:{type(exc).__name__}:{exc}")
            continue
        for item in _rows(raw):
            market = core.parse_market(item, min_liquidity)
            if market is None or market.condition_id not in chunk:
                continue
            _register_seed_market(market.condition_id, market, seen, out, "condition_id")

    rank = {
        _CANONICAL_CONDITION_BY_RAW.get(raw_condition, raw_condition): i
        for i, raw_condition in enumerate(unique)
    }
    out.sort(key=lambda market: (rank.get(market.condition_id, len(rank)), -market.volume24h, -market.liquidity))
    return out, errors


def merge_seeded_universe(volume_prior: list[core.Market], seed_markets: list[core.Market],
                          limit: int) -> list[core.Market]:
    target = max(1, int(limit))
    merged: list[core.Market] = []
    seen: set[str] = set()
    for market in [*seed_markets, *volume_prior]:
        if market.market_id in seen:
            continue
        seen.add(market.market_id)
        merged.append(market)
        if len(merged) >= target:
            break
    return merged


def discover_markets_seeded(limit: int, min_liquidity: float) -> list[core.Market]:
    global _SEED_DIAGNOSTICS, _PRIOR_CONDITIONS, _SEED_CONDITIONS, _SEED_ADDED_CONDITIONS
    volume_prior = _ORIGINAL_DISCOVER(limit, min_liquidity)
    _PRIOR_CONDITIONS = {market.condition_id for market in volume_prior}
    end_ts = core.now_s()
    lookback = max(30, min(900, int(os.environ.get("HF_SEED_LOOKBACK_SECONDS", "180"))))
    max_conditions = max(1, min(100, int(os.environ.get("HF_SEED_MAX_CONDITIONS", "50"))))
    seed_ids: list[str] = []
    seed_received_ms = 0
    raw_recent_rows = 0
    seed_errors: list[str] = []
    try:
        seed_ids, seed_received_ms, raw_recent_rows = _recent_condition_ids(
            end_ts - lookback, end_ts, max_conditions)
    except Exception as exc:
        seed_errors.append(f"seed_global:{type(exc).__name__}:{exc}")

    seed_markets: list[core.Market] = []
    if seed_ids:
        seed_markets, mapping_errors = _markets_for_conditions(seed_ids, min_liquidity)
        seed_errors.extend(mapping_errors)

    _SEED_CONDITIONS = {market.condition_id for market in seed_markets}
    _SEED_ADDED_CONDITIONS = _SEED_CONDITIONS.difference(_PRIOR_CONDITIONS)
    merged = merge_seeded_universe(volume_prior, seed_markets, limit)
    raw_lengths: dict[str, int] = {}
    for condition in seed_ids:
        key = str(max(0, len(condition) - 2) if condition.startswith("0x") else len(condition))
        raw_lengths[key] = raw_lengths.get(key, 0) + 1
    all_seed_assets = {token for condition in seed_ids for token in _SEED_ASSETS.get(condition, set())}
    _SEED_DIAGNOSTICS = {
        "seed_strategy": "recent_global_trade_clob_token_identity_then_slug_or_condition_plus_volume24h_prior",
        "seed_lookback_seconds": lookback,
        "seed_max_conditions": max_conditions,
        "seed_global_recent_rows": raw_recent_rows,
        "seed_global_condition_ids": seed_ids,
        "seed_global_condition_count": len(seed_ids),
        "seed_global_asset_count": len(all_seed_assets),
        "seed_global_slug_count": len({slug for slug in _SEED_SLUGS.values() if slug}),
        "seed_raw_condition_hex_length_counts": raw_lengths,
        "seed_noncanonical_raw_condition_count": sum(not _CONDITION_RE.fullmatch(x) for x in seed_ids),
        "seed_gamma_market_count": len(seed_markets),
        "seed_canonical_condition_count": len(_SEED_CONDITIONS),
        "seed_resolved_by_token_count": sum(mode == "clob_token_ids" for mode in _SEED_RESOLUTION_MODE.values()),
        "seed_resolved_by_slug_count": sum(mode == "slug_token" for mode in _SEED_RESOLUTION_MODE.values()),
        "seed_resolved_by_condition_count": sum(mode == "condition_id" for mode in _SEED_RESOLUTION_MODE.values()),
        "seed_resolution_mode": dict(_SEED_RESOLUTION_MODE),
        "seed_overlap_with_volume_prior": len(_SEED_CONDITIONS.intersection(_PRIOR_CONDITIONS)),
        "seed_added_market_count": len(_SEED_ADDED_CONDITIONS),
        "seed_received_ms": seed_received_ms,
        "seed_errors": seed_errors[:20],
        "volume_prior_market_count": len(volume_prior),
        "merged_market_count": len(merged),
        "authorized_market_cap_respected": len(merged) <= max(1, int(limit)),
    }
    return merged


def _merge_global_token_flows(flows: dict[str, list[core.Trade]], start_ts: int,
                              end_ts: int) -> tuple[int, list[str]]:
    if not _TOKEN_TO_CANONICAL_CONDITION:
        return 0, []
    try:
        rows, received_ms = _global_trade_rows(start_ts, end_ts)
    except Exception as exc:
        return 0, [f"seed_global_flow:{type(exc).__name__}:{exc}"]
    seen: set[str] = {
        trade.trade_id
        for trades in flows.values()
        for trade in trades
    }
    for item in rows:
        token = str(item.get("asset") or "")
        canonical = _TOKEN_TO_CANONICAL_CONDITION.get(token)
        if not canonical:
            continue
        side = str(item.get("side") or "").upper()
        ts = int(core.number(item.get("timestamp"), 0))
        price = core.number(item.get("price"), -1.0)
        size = core.number(item.get("size"), 0.0)
        key = ":".join([
            str(item.get("transactionHash") or ""), token, str(ts), side,
            f"{price:.12g}", f"{size:.12g}",
        ])
        if key in seen:
            continue
        seen.add(key)
        flows.setdefault(canonical, []).append(core.Trade(key, token, side, price, size, ts))
    for condition in flows:
        flows[condition].sort(key=lambda trade: (trade.ts, trade.trade_id))
    return max(0, received_ms), []


def fetch_trades_seeded(condition_ids: list[str], start_ts: int, end_ts: int,
                        batch_size: int = 5) -> tuple[dict[str, list[core.Trade]], int, list[str]]:
    global _FLOW_CONDITIONS
    flows, received_ms, errors = _ORIGINAL_FETCH(condition_ids, start_ts, end_ts, batch_size)
    flows = dict(flows)
    global_received_ms, global_errors = _merge_global_token_flows(flows, start_ts, end_ts)
    errors.extend(global_errors)
    received_ms = max(received_ms, global_received_ms)
    _FLOW_CONDITIONS = set(flows)
    return flows, received_ms, errors


def seeded_activity_data_healthy(result: dict[str, Any]) -> bool:
    if not entrypoint.activity_data_healthy(result):
        return False
    universe = result.get("universe")
    if not isinstance(universe, dict):
        return False
    active = int(universe.get("active_markets_evaluated") or 0)
    seed_errors = universe.get("seed_errors")
    if active == 0 and isinstance(seed_errors, list) and seed_errors:
        return False
    if int(universe.get("seed_global_condition_count") or 0) > 0 and int(universe.get("seed_gamma_market_count") or 0) == 0:
        return False
    if int(universe.get("seed_global_recent_rows") or 0) > 0 and int(universe.get("seed_global_asset_count") or 0) > 0 and int(universe.get("seed_resolved_by_token_count") or 0) == 0:
        return False
    return True


def _deterministic_checks() -> None:
    query = urllib.parse.parse_qs(_condition_market_query(["0xa", "0xb"]))
    assert query.get("condition_ids") == ["0xa", "0xb"]
    assert query.get("active") == ["true"]
    token_query = urllib.parse.parse_qs(_token_market_query(["token-a", "token-b"]))
    assert token_query.get("clob_token_ids") == ["token-a", "token-b"]
    assert token_query.get("active") == ["true"]
    slug_query = urllib.parse.parse_qs(_slug_market_query("will-x-happen"))
    assert slug_query.get("slug") == ["will-x-happen"]
    global_query = urllib.parse.parse_qs(_global_seed_query(100, 200))
    assert global_query.get("start") == ["100"]
    assert global_query.get("end") == ["200"]
    assert global_query.get("takerOnly") == ["true"]


def main() -> int:
    _deterministic_checks()
    entrypoint._deterministic_contract_checks()
    core.discover_markets = discover_markets_seeded
    batched.fetch_trades_batch = fetch_trades_seeded

    args = core.parse_args()
    args.trade_batch_size = 5
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = batched.run_batched(args)
    universe = result.setdefault("universe", {})
    if isinstance(universe, dict):
        universe.update(entrypoint._ACTIVITY_DIAGNOSTICS)
        universe.update(_SEED_DIAGNOSTICS)
        universe["trade_batch_size"] = args.trade_batch_size
        universe["trade_page_limit"] = entrypoint._PAGE_LIMIT
        universe["flow_condition_count"] = len(_FLOW_CONDITIONS)
        universe["volume_prior_active_condition_count"] = len(_FLOW_CONDITIONS.intersection(_PRIOR_CONDITIONS))
        universe["seed_active_condition_count"] = len(_FLOW_CONDITIONS.intersection(_SEED_CONDITIONS))
        universe["seed_added_active_condition_count"] = len(_FLOW_CONDITIONS.intersection(_SEED_ADDED_CONDITIONS))
    healthy = seeded_activity_data_healthy(result)
    if isinstance(universe, dict):
        universe["activity_data_healthy"] = healthy

    result_path = out / "result.json"
    markdown_path = out / "result.md"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = batched.markdown(result)
    if isinstance(universe, dict):
        report += (
            f"- recent-trade seed conditions: {universe.get('seed_global_condition_count', 0)}\n"
            f"- recent-trade seed assets: {universe.get('seed_global_asset_count', 0)}\n"
            f"- noncanonical raw condition IDs: {universe.get('seed_noncanonical_raw_condition_count', 0)}\n"
            f"- seed markets mapped by CLOB token id: {universe.get('seed_resolved_by_token_count', 0)}\n"
            f"- seed markets mapped by slug/token fallback: {universe.get('seed_resolved_by_slug_count', 0)}\n"
            f"- seed markets mapped total: {universe.get('seed_gamma_market_count', 0)}\n"
            f"- seed-only markets added: {universe.get('seed_added_market_count', 0)}\n"
            f"- seed conditions with causal scoped/global-token flow: {universe.get('seed_active_condition_count', 0)}\n"
            f"- seed-only conditions with causal flow: {universe.get('seed_added_active_condition_count', 0)}\n"
        )
    markdown_path.write_text(report, encoding="utf-8")
    print(report, end="")
    if not healthy:
        state = universe.get("global_trade_tape_state") if isinstance(universe, dict) else None
        print(f"HF seeded activity discovery is unhealthy: state={state}; refusing zero-activity evidence.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())