#!/usr/bin/env python3
"""Read-only Limitless public-market collector for the V7 external fabric.

The documented public market, orderbook and finalized-trade endpoints need no
credential.  In particular this module deliberately has no order, account,
wallet, or token-authentication path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from v7_cross_platform_collector import PersistentJsonClient, atomic_json, append_record, git_head


STATUS_SCHEMA = "polymarket_v7_limitless_public_status_v1"
STATE_SCHEMA = "polymarket_v7_limitless_public_state_v1"


class LimitlessCollectorError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _state(path: Path) -> dict[str, Any]:
    value = _load(path)
    if value.get("schema") not in (None, STATE_SCHEMA):
        raise LimitlessCollectorError("limitless_state_invalid")
    return {"schema": STATE_SCHEMA, "last_hash": str(value.get("last_hash") or "0" * 64),
            "metadata_hashes": value.get("metadata_hashes") if isinstance(value.get("metadata_hashes"), dict) else {},
            "trade_hashes": value.get("trade_hashes") if isinstance(value.get("trade_hashes"), dict) else {}}


def _number(value: Any) -> float:
    if isinstance(value, bool): raise LimitlessCollectorError("limitless_invalid_number")
    try: result = float(value)
    except (TypeError, ValueError, OverflowError) as exc: raise LimitlessCollectorError("limitless_invalid_number") from exc
    if not math.isfinite(result) or result < 0.0: raise LimitlessCollectorError("limitless_invalid_number")
    return result


def _book(value: Any) -> dict[str, list[list[float]]]:
    if not isinstance(value, dict): raise LimitlessCollectorError("limitless_orderbook_invalid")
    result: dict[str, list[list[float]]] = {}
    for side in ("bids", "asks"):
        rows = value.get(side)
        if not isinstance(rows, list): raise LimitlessCollectorError("limitless_orderbook_side_missing")
        levels: list[list[float]] = []
        for row in rows:
            if not isinstance(row, dict): raise LimitlessCollectorError("limitless_orderbook_level_invalid")
            price, size = _number(row.get("price")), _number(row.get("size"))
            if not 0.0 <= price <= 1.0 or size <= 0.0: raise LimitlessCollectorError("limitless_orderbook_level_invalid")
            levels.append([price, size])
        result[side] = sorted(levels, key=lambda x: x[0], reverse=side == "bids")
    if result["bids"] and result["asks"] and result["bids"][0][0] >= result["asks"][0][0]:
        raise LimitlessCollectorError("limitless_orderbook_crossed")
    return result


def _markets(value: Any) -> list[dict[str, Any]]:
    rows = value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list): raise LimitlessCollectorError("limitless_active_markets_invalid")
    output = []
    for row in rows:
        if not isinstance(row, dict): continue
        slug = str(row.get("slug") or "").strip()
        tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        if slug and str(tokens.get("yes") or "") and str(tokens.get("no") or ""):
            output.append(row)
    return output


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("id", "slug", "stableSlug", "title", "description", "status", "expired",
                                           "expirationTimestamp", "startAt", "updatedAt", "tokens", "prices", "tradeType",
                                           "marketType", "priceOracleMetadata", "metadata", "settings")}


def collect_once(*, repository_root: Path, config_path: Path, tape_path: Path, state_path: Path,
                 status_path: Path, client: Any | None = None, now_ms: int | None = None) -> dict[str, Any]:
    config = _load(config_path); policy = config.get("limitless") if isinstance(config.get("limitless"), dict) else {}
    if (config.get("schema") != "polymarket_v7_external_inputs_v1" or config.get("version") != 7
            or config.get("paper_only") is not True or config.get("authenticated_execution") is not False
            or config.get("real_order_submission") is not False or policy.get("credentials_required") is not False):
        raise LimitlessCollectorError("limitless_config_invalid")
    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms); sha = git_head(repository_root); state = _state(state_path)
    own_client = client is None; client = client or PersistentJsonClient(str(policy.get("base_url") or ""))
    discovered = books = trades = failures = 0; blocker = ""; latency: list[float] = []
    try:
        raw, timing = client.get("/markets/active"); latency.append(float(timing.get("request_ms") or 0.0))
        markets = _markets(raw); discovered = len(markets); maximum = max(1, min(100, int(policy.get("max_markets_per_cycle") or 20)))
        for market in markets[:maximum]:
            slug = str(market["slug"]); metadata = _metadata(market)
            digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if state["metadata_hashes"].get(slug) != digest:
                append_record(tape_path, state, {"kind": "MARKET_METADATA", "venue": "limitless", "contract_id": slug,
                    "received_at_ms": timestamp, "transport": "PUBLIC_REST", "polling_latency_not_event_latency": True,
                    "metadata": metadata, "metadata_hash": digest, "repository_sha": sha})
                state["metadata_hashes"][slug] = digest
            try:
                raw_book, book_timing = client.get("/markets/" + slug + "/orderbook"); latency.append(float(book_timing.get("request_ms") or 0.0))
                append_record(tape_path, state, {"kind": "ORDERBOOK_SNAPSHOT", "venue": "limitless", "contract_id": slug,
                    "received_at_ms": timestamp, "transport": "PUBLIC_REST", "polling_latency_not_event_latency": True,
                    "book": _book(raw_book), "token_id": raw_book.get("tokenId"), "repository_sha": sha})
                books += 1
            except Exception: failures += 1
            try:
                raw_trades, trade_timing = client.get("/markets/" + slug + "/events"); latency.append(float(trade_timing.get("request_ms") or 0.0))
                rows = raw_trades.get("events") if isinstance(raw_trades, dict) else []
                if not isinstance(rows, list): raise LimitlessCollectorError("limitless_events_invalid")
                for trade in rows:
                    if not isinstance(trade, dict): continue
                    event_id = str(trade.get("txHash") or "")
                    digest = hashlib.sha256(json.dumps(trade, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    if event_id and state["trade_hashes"].get(event_id) == digest: continue
                    append_record(tape_path, state, {"kind": "PREDICTION_MARKET_TRADE", "venue": "limitless", "contract_id": slug,
                        "received_at_ms": timestamp, "transport": "PUBLIC_REST", "polling_latency_not_event_latency": True,
                        "trade": trade, "repository_sha": sha})
                    if event_id: state["trade_hashes"][event_id] = digest
                    trades += 1
            except Exception: failures += 1
        feed_status = "OPERATIONAL" if books > 0 else "DEGRADED"
    except Exception as exc:
        feed_status, blocker = "DOWN", f"BLOCKED_LIMITLESS_PUBLIC:{type(exc).__name__}:{exc}"
    finally:
        if own_client and hasattr(client, "close"): client.close()
    atomic_json(state_path, state)
    status = {"schema": STATUS_SCHEMA, "version": 7, "family": "cross_platform", "source_id": "limitless_public",
        "authority": "RESEARCH", "model_sha": sha, "timestamp_ms": timestamp, "timestamp": timestamp // 1000,
        "process_state": "RUNNING", "implementation_complete": True, "feed_status": feed_status,
        "feed_operational": feed_status == "OPERATIONAL", "transport": "PUBLIC_REST", "credentials_required": False,
        "token_used": False, "discovered_markets": discovered, "synchronized_books": books, "trades_observed": trades,
        "parse_failure_count": failures, "request_latency_ms": latency, "blocker": blocker, "reason_codes": [blocker] if blocker else [],
        "paper_only": True, "research_only": True, "authenticated_execution": False, "real_order_submission": False,
        "execution_authority": False, "capital_authority": False, "oms_authority": False, "ledger_write_authority": False,
        "promotion_authority": False}
    atomic_json(status_path, status); return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_inputs.json")); parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True); parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=15.0); parser.add_argument("--loop", action="store_true"); args = parser.parse_args(argv)
    while True:
        print(json.dumps(collect_once(repository_root=args.repository_root, config_path=args.config, tape_path=args.tape,
                                      state_path=args.state, status_path=args.status), sort_keys=True), flush=True)
        if not args.loop: return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
