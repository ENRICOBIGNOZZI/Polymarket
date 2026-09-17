#!/usr/bin/env python3
"""Causal zero-authority ContractState for six-asset Polymarket SHADOW research."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_multi_crypto_contract_state_v1"
SELECTION_SCHEMA = "polymarket_v7_multi_crypto_book_selection_v1"
ASSETS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
HORIZONS = {"M5", "M15"}
STOP = False


def stop_handler(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def exact_hash(value: Any, size: int) -> bool:
    text = str(value or "")
    return len(text) == size and all(ch in "0123456789abcdef" for ch in text)


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_utc_ns(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def safe_source(value: dict[str, Any]) -> bool:
    return value.get("paper_only") is True and value.get("authenticated_execution") is False \
        and value.get("real_order_submission") is False


def validate_selection(value: dict[str, Any], model_sha: str) -> list[dict[str, Any]]:
    if (value.get("schema") != SELECTION_SCHEMA or value.get("model_sha") != model_sha
            or not safe_source(value) or value.get("execution_authority") is not False
            or value.get("real_capital_at_risk") is not False or value.get("selection_only") is not True):
        raise ValueError("contract_state:selection_identity_or_authority")
    if not exact_hash(value.get("generation_sha256"), 64):
        raise ValueError("contract_state:selection_generation")
    rows = value.get("markets")
    if not isinstance(rows, list) or not rows:
        raise ValueError("contract_state:selection_markets")
    seen_markets: set[str] = set(); seen_tokens: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("contract_state:market_row")
        market = str(row.get("market_id") or ""); yes = str(row.get("yes_token") or ""); no = str(row.get("no_token") or "")
        if (row.get("asset") not in ASSETS or row.get("horizon") not in HORIZONS or not market
                or market in seen_markets or not yes or not no or yes == no or yes in seen_tokens or no in seen_tokens
                or not exact_hash(row.get("normalized_rules_hash"), 64)
                or not exact_hash(row.get("rule_snapshot_sha256"), 64)):
            raise ValueError("contract_state:market_identity")
        seen_markets.add(market); seen_tokens.update((yes, no))
    return rows


def book_state(path: Path, *, model_sha: str, market_id: str, token_id: str,
               now_ns: int, maximum_age_ms: int) -> dict[str, Any]:
    value = load(path)
    receive_ms = int(value.get("receive_wall_ms") or 0)
    receive_ns = receive_ms * 1_000_000
    age_ms = (now_ns - receive_ns) / 1_000_000.0 if 0 < receive_ns <= now_ns else None
    valid = bool(
        safe_source(value) and value.get("execution_authority") == "ZERO_AUTHORITY_RESEARCH_ONLY"
        and value.get("model_sha") == model_sha and str(value.get("market_id") or "") == market_id
        and str(value.get("token_id") or "") == token_id and value.get("valid") is True
        and value.get("lineage_continuous") is True and age_ms is not None and age_ms <= maximum_age_ms
    )
    return {
        "valid": valid, "age_ms": age_ms, "receive_wall_ms": receive_ms,
        "state_version": int(value.get("state_version") or 0),
        "connection_epoch": int(value.get("connection_epoch") or 0),
        "best_bid": finite(value.get("best_bid")) if valid else None,
        "best_ask": finite(value.get("best_ask")) if valid else None,
    }


def build(selection: dict[str, Any], oracle: dict[str, Any], *, book_dir: Path,
          model_sha: str, now_ns: int, maximum_book_age_ms: int,
          maximum_oracle_age_ms: int) -> dict[str, Any]:
    rows = validate_selection(selection, model_sha)
    if (not safe_source(oracle) or oracle.get("execution_authority") is not False
            or oracle.get("model_sha") != model_sha):
        raise ValueError("contract_state:oracle_identity_or_authority")
    oracle_assets = oracle.get("assets") if isinstance(oracle.get("assets"), dict) else {}
    references = oracle.get("settlement_references") if isinstance(oracle.get("settlement_references"), dict) else {}
    output: list[dict[str, Any]] = []; blocker_counts: dict[str, int] = {}
    active_markets = active_ready = 0
    for row in rows:
        asset = str(row["asset"]); horizon = str(row["horizon"]); market_id = str(row["market_id"])
        rules_hash = str(row["normalized_rules_hash"]); start_ns = parse_utc_ns(row.get("start_timestamp")); end_ns = parse_utc_ns(row.get("end_timestamp"))
        active = bool(start_ns > 0 and end_ns > start_ns and start_ns <= now_ns < end_ns)
        active_markets += int(active)
        oracle_row = oracle_assets.get(asset) if isinstance(oracle_assets.get(asset), dict) else {}
        oracle_age = finite(oracle_row.get("receive_age_ms"))
        oracle_fresh = bool(oracle_row.get("fresh") is True and oracle_age is not None
                            and 0 <= oracle_age <= maximum_oracle_age_ms and finite(oracle_row.get("price")) is not None)
        reference = references.get(market_id) if isinstance(references.get(market_id), dict) else {}
        ref_source_ms = int(reference.get("source_timestamp_ms") or 0); ref_capture_ms = int(reference.get("captured_at_ms") or 0)
        reference_valid = bool(reference.get("valid") is True and reference.get("asset") == asset
            and reference.get("horizon") == horizon and str(reference.get("market_id") or "") == market_id
            and reference.get("normalized_rules_hash") == rules_hash and finite(reference.get("price")) is not None
            and start_ns > 0 and 0 < ref_source_ms * 1_000_000 <= start_ns and 0 < ref_capture_ms * 1_000_000 <= now_ns)
        yes = book_state(book_dir / f"{row['yes_token']}.json", model_sha=model_sha, market_id=market_id,
                         token_id=str(row["yes_token"]), now_ns=now_ns, maximum_age_ms=maximum_book_age_ms)
        no = book_state(book_dir / f"{row['no_token']}.json", model_sha=model_sha, market_id=market_id,
                        token_id=str(row["no_token"]), now_ns=now_ns, maximum_age_ms=maximum_book_age_ms)
        book_valid = yes["valid"] and no["valid"]
        blockers: list[str] = []
        if not oracle_fresh:
            blockers.append("ORACLE_NOT_FRESH")
        if active and not reference_valid:
            blockers.append("REFERENCE_NOT_CAUSAL_OR_MISMATCHED")
        if active and not book_valid:
            blockers.append("PM_BOOK_NOT_READY")
        state = "ACTIVE_READY_SHADOW" if active and not blockers else (
            "FUTURE_WARMING" if not active else "ACTIVE_BLOCKED")
        active_ready += int(state == "ACTIVE_READY_SHADOW")
        for reason in blockers:
            blocker_counts[reason] = blocker_counts.get(reason, 0) + 1
        source_versions = {
            "selection_generated_at_ms": int(selection.get("generated_at_ms") or 0),
            "oracle_version": int(oracle_row.get("version") or 0),
            "oracle_receive_wall_ns": int(oracle_row.get("receive_wall_ns") or 0),
            "reference_captured_at_ms": ref_capture_ms,
            "yes_state_version": yes["state_version"], "no_state_version": no["state_version"],
            "yes_connection_epoch": yes["connection_epoch"], "no_connection_epoch": no["connection_epoch"],
            "yes_receive_wall_ms": yes["receive_wall_ms"], "no_receive_wall_ms": no["receive_wall_ms"],
        }
        available_at_ns = max(
            source_versions["selection_generated_at_ms"] * 1_000_000,
            source_versions["oracle_receive_wall_ns"], source_versions["reference_captured_at_ms"] * 1_000_000,
            source_versions["yes_receive_wall_ms"] * 1_000_000, source_versions["no_receive_wall_ms"] * 1_000_000,
        )
        identity = {
            "asset": asset, "horizon": horizon, "market_id": market_id,
            "event_id": str(row.get("event_id") or ""), "yes_token": str(row["yes_token"]),
            "no_token": str(row["no_token"]), "normalized_rules_hash": rules_hash,
            "rule_snapshot_sha256": str(row["rule_snapshot_sha256"]), "source_versions": source_versions,
        }
        output.append({
            **identity, "contract_state_hash": canonical_hash(identity), "state": state,
            "active_now": active, "entry_authority": False, "rules_verified": True,
            "oracle_fresh": oracle_fresh, "oracle_price": finite(oracle_row.get("price")) if oracle_fresh else None,
            "reference_valid": reference_valid, "reference_price": finite(reference.get("price")) if reference_valid else None,
            "book_valid": book_valid, "yes_book": yes, "no_book": no, "available_at_ns": available_at_ns,
            "blockers": blockers,
        })
    return {
        "schema": SCHEMA, "version": 1, "timestamp_ns": now_ns, "model_sha": model_sha,
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "execution_authority": False, "research_only": True,
        "selection_generation": selection.get("generation_sha256"), "market_count": len(output),
        "active_markets": active_markets, "active_ready_markets": active_ready,
        "all_active_ready": active_markets > 0 and active_markets == active_ready,
        "blocker_counts": dict(sorted(blocker_counts.items())), "markets": output,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--oracle-status", type=Path, required=True)
    parser.add_argument("--book-features-dir", type=Path, required=True)
    parser.add_argument("--feature-policy", type=Path, default=Path("config/v7_multi_crypto_feature_policy.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-ms", type=int, default=50)
    args = parser.parse_args()
    if not exact_hash(args.model_sha, 40):
        raise ValueError("exact model SHA required")
    if not 10 <= args.interval_ms <= 5000:
        raise ValueError("interval-ms out of range")
    policy = load(args.feature_policy)
    max_book = int(policy.get("maximum_book_age_ms") or 0); max_oracle = int(policy.get("maximum_oracle_age_ms") or 0)
    if max_book <= 0 or max_oracle <= 0:
        raise ValueError("feature policy freshness missing")
    signal.signal(signal.SIGINT, stop_handler); signal.signal(signal.SIGTERM, stop_handler)
    while True:
        now_ns = time.time_ns()
        try:
            value = build(load(args.selection), load(args.oracle_status), book_dir=args.book_features_dir,
                          model_sha=args.model_sha, now_ns=now_ns, maximum_book_age_ms=max_book,
                          maximum_oracle_age_ms=max_oracle)
            value["runtime_state"] = "RUNNING_SHADOW" if value["all_active_ready"] else "WARMING_OR_BLOCKED"
            value["runtime_blockers"] = [] if value["all_active_ready"] else ["ACTIVE_CONTRACT_STATE_NOT_READY"]
        except ValueError as error:
            value = {
                "schema": SCHEMA, "version": 1, "timestamp_ns": now_ns, "model_sha": args.model_sha,
                "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
                "real_capital_at_risk": False, "execution_authority": False, "research_only": True,
                "market_count": 0, "active_markets": 0, "active_ready_markets": 0, "all_active_ready": False,
                "blocker_counts": {}, "markets": [], "runtime_state": "WARMING_OR_BLOCKED",
                "runtime_blockers": [str(error)],
            }
        atomic_json(args.output, value)
        if not args.loop or STOP:
            break
        time.sleep(args.interval_ms / 1000.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
