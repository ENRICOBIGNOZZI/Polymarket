#!/usr/bin/env python3
"""Read-only Gamma discovery for V7 multi-crypto up/down contracts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from v7_contract_registry import ContractRegistry, contract_from_market_v2, normalize_rules

SCHEMA = "polymarket_v7_multi_crypto_market_registry_snapshot_v1"
HORIZONS = {"M5": (300, "5m"), "M15": (900, "15m")}
ASSET_ALIASES = {
    "BTC": ("btc", "bitcoin"),
    "ETH": ("eth", "ethereum"),
    "SOL": ("sol", "solana"),
    "XRP": ("xrp",),
    "DOGE": ("doge", "dogecoin"),
    "BNB": ("bnb", "binance coin"),
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON object required")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def fetch_json(url: str, timeout: int) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "polymarket-v7-multi-crypto-discovery/1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def candidate_asset(raw: dict[str, Any], allowed: set[str]) -> str | None:
    text = normalize_rules(" ".join(str(raw.get(k) or "") for k in ("question", "title", "slug")))
    if "up or down" not in text and "updown" not in text:
        return None
    matches = [asset for asset in sorted(allowed)
               if any(alias in text for alias in ASSET_ALIASES[asset])]
    return matches[0] if len(matches) == 1 else None


def explicit_token_mapping(raw: dict[str, Any]) -> dict[str, str] | None:
    tokens = raw.get("clobTokenIds") or raw.get("clob_token_ids") or raw.get("tokens")
    outcomes = raw.get("outcomes")
    if isinstance(tokens, str):
        try: tokens = json.loads(tokens)
        except json.JSONDecodeError: return None
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except json.JSONDecodeError: return None
    if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict):
        mapping: dict[str, str] = {}
        for row in tokens:
            if not isinstance(row, dict):
                continue
            outcome = normalize_rules(row.get("outcome"))
            token = str(row.get("token_id") or row.get("tokenId") or row.get("id") or "").strip()
            if outcome in {"yes", "up"} and token: mapping["YES"] = token
            elif outcome in {"no", "down"} and token: mapping["NO"] = token
        return mapping if set(mapping) == {"YES", "NO"} else None
    if isinstance(tokens, list) and isinstance(outcomes, list) and len(tokens) >= 2 and len(outcomes) >= 2:
        mapping: dict[str, str] = {}
        for token, outcome in zip(tokens, outcomes):
            label = normalize_rules(outcome)
            if label in {"yes", "up"}: mapping["YES"] = str(token)
            elif label in {"no", "down"}: mapping["NO"] = str(token)
        return mapping if set(mapping) == {"YES", "NO"} else None
    return None


def discover_window_hints(
    config: dict[str, Any], gamma_url: str, *, now_s: int, windows: int,
    timeout: int, fetcher: Callable[[str, int], Any] = fetch_json,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Discover current/next markets from deterministic slug hints, then verify raw rules."""
    if windows < 1 or windows > 12:
        raise ValueError("windows must be in [1, 12]")
    asset_rows = config.get("assets") if isinstance(config.get("assets"), list) else []
    assets = [str(row.get("asset") or "") for row in asset_rows if isinstance(row, dict)]
    supported_horizons = [str(x) for x in config.get("supported_horizons") or []]
    rows_by_id: dict[str, dict[str, Any]] = {}
    requested: list[dict[str, Any]] = []
    for asset in assets:
        if asset not in ASSET_ALIASES:
            continue
        for horizon in supported_horizons:
            if horizon not in HORIZONS:
                continue
            seconds, slug_horizon = HORIZONS[horizon]
            current_start = (int(now_s) // seconds) * seconds
            for offset in range(windows):
                start = current_start + offset * seconds
                slug = f"{asset.lower()}-updown-{slug_horizon}-{start}"
                url = gamma_url.rstrip("/") + "/markets?" + urllib.parse.urlencode({"slug": slug})
                value = fetcher(url, timeout)
                rows = value if isinstance(value, list) else []
                hits = 0
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    market_id = str(raw.get("id") or "").strip()
                    if not market_id:
                        continue
                    rows_by_id[market_id] = raw
                    hits += 1
                requested.append({
                    "asset": asset,
                    "horizon": horizon,
                    "window_start_unix": start,
                    "slug_hint": slug,
                    "hits": hits,
                })
    return list(rows_by_id.values()), {
        "method": "CURRENT_NEXT_SLUG_HINTS_WITH_FULL_RULE_VERIFICATION",
        "queries": len(requested),
        "hits": sum(row["hits"] for row in requested),
        "unique_markets": len(rows_by_id),
        "requested_windows": requested,
        "discovery_exhaustive": False,
        "pagination_loop_guard_hit": False,
    }


def discover_raw(
    gamma_url: str, *, page_size: int, max_pages: int, timeout: int,
    fetcher: Callable[[str, int], Any] = fetch_json,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not gamma_url.startswith("https://"):
        raise ValueError("Gamma URL must use HTTPS")
    if not 1 <= page_size <= 100 or max_pages < 1:
        raise ValueError("invalid pagination controls")
    cursor = ""
    seen_cursors: set[str] = set()
    rows_by_id: dict[str, dict[str, Any]] = {}
    pages = 0
    duplicates = 0
    exhaustive = False
    started = time.monotonic_ns()
    for _ in range(max_pages):
        query = {"active": "true", "closed": "false", "limit": page_size,
                 "order": "endDate", "ascending": "true"}
        if cursor:
            query["after_cursor"] = cursor
        value = fetcher(gamma_url.rstrip("/") + "/markets/keyset?" + urllib.parse.urlencode(query), timeout)
        if not isinstance(value, dict):
            raise ValueError("Gamma keyset response must be an object")
        rows = value.get("markets")
        if not isinstance(rows, list):
            raise ValueError("Gamma keyset response missing markets array")
        pages += 1
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            market_id = str(raw.get("id") or "").strip()
            if not market_id:
                continue
            if market_id in rows_by_id:
                duplicates += 1
            rows_by_id[market_id] = raw
        next_cursor = str(value.get("next_cursor") or "")
        if not rows or not next_cursor:
            exhaustive = True
            break
        if next_cursor == cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return list(rows_by_id.values()), {
        "pages": pages,
        "discovered_rows": len(rows_by_id),
        "duplicate_rows": duplicates,
        "discovery_exhaustive": exhaustive,
        "pagination_loop_guard_hit": not exhaustive,
        "scan_duration_ms": (time.monotonic_ns() - started) / 1_000_000.0,
    }


def build_snapshot(
    raw_markets: list[dict[str, Any]], *, config: dict[str, Any],
    approved_rule_hashes: set[str], fetched_at_ms: int, discovery: dict[str, Any],
    rules_dir: Path | None = None,
) -> dict[str, Any]:
    if config.get("paper_only") is not True or config.get("authenticated_execution") is not False \
            or config.get("real_order_submission") is not False:
        raise ValueError("multi-crypto config must remain fail-closed PAPER")
    asset_rows = config.get("assets") if isinstance(config.get("assets"), list) else []
    allowed = {str(row.get("asset") or "") for row in asset_rows if isinstance(row, dict)}
    if allowed != set(ASSET_ALIASES):
        raise ValueError("asset registry must explicitly contain the six configured assets")

    records: list[dict[str, Any]] = []
    rejected_candidates = 0
    for raw in raw_markets:
        hint = candidate_asset(raw, allowed)
        if hint is None:
            continue
        spec = contract_from_market_v2(raw, approved_rule_hashes=approved_rule_hashes)
        token_mapping = explicit_token_mapping(raw)
        verified = bool(spec.verified_template and spec.asset == hint and token_mapping)
        if not verified:
            rejected_candidates += 1
        state = "RULES_VERIFIED" if verified else "INVALID_RULES"
        if verified and spec.rules_hash_recognized:
            state = "RULES_VERIFIED_APPROVED_HASH"
        rule_payload = {
            "market_id": spec.market_id,
            "asset_hint": hint,
            "question": str(raw.get("question") or ""),
            "raw_rules": str(raw.get("rules") or raw.get("description") or ""),
            "resolution_source": str(raw.get("resolutionSource") or ""),
            "fetched_at_ms": int(fetched_at_ms),
            "normalized_rules_hash": spec.normalized_rules_hash,
            "parser_version": spec.parser_version,
        }
        rule_snapshot_hash = hashlib.sha256(
            json.dumps(rule_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if rules_dir is not None:
            atomic_json(rules_dir / f"{spec.market_id}.json", {**rule_payload, "snapshot_sha256": rule_snapshot_hash})
        record = {
            "state": state,
            "asset_hint": hint,
            "market_id": spec.market_id,
            "event_id": spec.event_id,
            "condition_id": spec.condition_id,
            "slug": spec.slug,
            "contract_family": spec.contract_family,
            "start_timestamp": spec.start_timestamp,
            "end_timestamp": spec.end_timestamp,
            "settlement_provider": spec.settlement_provider,
            "oracle_symbol": spec.oracle_symbol,
            "oracle_feed_id": spec.oracle_feed_id,
            "oracle_window_seconds": spec.oracle_window_seconds,
            "comparator": spec.comparator,
            "parser_version": spec.parser_version,
            "normalized_rules_hash": spec.normalized_rules_hash,
            "rules_hash_recognized": spec.rules_hash_recognized,
            "verified_template": verified,
            "informed_trading_authorized": bool(verified and spec.informed_trading_authorized),
            "token_mapping": token_mapping,
            "fees_enabled": bool(raw.get("feesEnabled", False)),
            "fee_schedule": raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else {},
            "accepting_orders": bool(raw.get("acceptingOrders", True)),
            "closed": bool(raw.get("closed", False)),
            "rule_snapshot_sha256": rule_snapshot_hash,
            "verification_reasons": list(spec.verification_reasons),
        }
        records.append(record)

    records.sort(key=lambda r: (r["asset_hint"], r["start_timestamp"], r["market_id"]))
    return {
        "schema": SCHEMA,
        "version": 1,
        "fetched_at_ms": int(fetched_at_ms),
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "discovery": discovery,
        "candidate_markets": len(records),
        "rejected_candidates": rejected_candidates,
        "rules_verified": sum(r["verified_template"] for r in records),
        "approved_rule_hashes": sum(r["rules_hash_recognized"] for r in records),
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config/v7_multi_crypto_assets.json"))
    parser.add_argument("--contract-registry", type=Path,
                        default=Path("runs/paper_v7_live/external_fair/contract_registry_v2.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--mode", choices=("windowed", "exhaustive"), default="windowed")
    parser.add_argument("--windows", type=int, default=2,
                        help="Current plus next N-1 contract windows in windowed mode.")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--now-unix", type=int, default=0,
                        help="Testing override; defaults to current wall time.")
    args = parser.parse_args()

    config = load_json(args.config)
    registry = ContractRegistry(args.contract_registry)
    if args.mode == "windowed":
        raw, discovery = discover_window_hints(
            config, args.gamma_url, now_s=args.now_unix or int(time.time()),
            windows=args.windows, timeout=args.timeout,
        )
    else:
        raw, discovery = discover_raw(
            args.gamma_url, page_size=args.page_size, max_pages=args.max_pages, timeout=args.timeout
        )
    fetched = time.time_ns() // 1_000_000
    snapshot = build_snapshot(
        raw, config=config, approved_rule_hashes=registry.approved_rule_hashes,
        fetched_at_ms=fetched, discovery=discovery, rules_dir=args.output_dir / "rules",
    )
    atomic_json(args.output_dir / "current.json", snapshot)
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    if args.mode == "windowed":
        return 0
    return 0 if discovery.get("discovery_exhaustive") else 2


if __name__ == "__main__":
    raise SystemExit(main())
