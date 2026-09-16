#!/usr/bin/env python3
"""Build a zero-authority Polymarket WS selection from verified discovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "polymarket_v7_multi_crypto_book_selection_v1"
ASSETS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
HORIZONS = {"M5", "M15"}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("discovery snapshot must be an object")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_utc(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def horizon_of(row: dict[str, Any]) -> str | None:
    family = str(row.get("contract_family") or "").upper()
    slug = str(row.get("slug") or "").lower()
    if family.endswith("_M5") or "-5m-" in slug:
        return "M5"
    if family.endswith("_M15") or "-15m-" in slug:
        return "M15"
    return None


def build_selection(snapshot: dict[str, Any], *, model_sha: str, now_unix: float, maximum_markets: int = 40) -> dict[str, Any]:
    if snapshot.get("paper_only") is not True \
            or snapshot.get("authenticated_execution") is not False \
            or snapshot.get("real_order_submission") is not False \
            or snapshot.get("execution_authority") is not False:
        raise ValueError("discovery snapshot is not zero-authority PAPER")
    if len(model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model_sha):
        raise ValueError("exact model SHA required")
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ValueError("discovery records missing")
    markets: list[dict[str, Any]] = []
    rejected = 0
    for row in records:
        if not isinstance(row, dict):
            rejected += 1
            continue
        asset = str(row.get("asset_hint") or "")
        horizon = horizon_of(row)
        mapping = row.get("token_mapping")
        yes = str(mapping.get("YES") or "") if isinstance(mapping, dict) else ""
        no = str(mapping.get("NO") or "") if isinstance(mapping, dict) else ""
        end_unix = parse_utc(row.get("end_timestamp"))
        valid = (
            asset in ASSETS and horizon in HORIZONS
            and row.get("verified_template") is True
            and row.get("accepting_orders") is True
            and row.get("closed") is False
            and bool(str(row.get("market_id") or ""))
            and bool(yes) and bool(no) and yes != no
            and end_unix > now_unix
        )
        if not valid:
            rejected += 1
            continue
        markets.append({
            "asset": asset,
            "horizon": horizon,
            "market_id": str(row["market_id"]),
            "event_id": str(row.get("event_id") or ""),
            "yes_token": yes,
            "no_token": no,
            "start_timestamp": str(row.get("start_timestamp") or ""),
            "end_timestamp": str(row.get("end_timestamp") or ""),
            "start_timestamp_ms": int(parse_utc(row.get("start_timestamp")) * 1000),
            "end_timestamp_ms": int(parse_utc(row.get("end_timestamp")) * 1000),
            "normalized_rules_hash": str(row.get("normalized_rules_hash") or ""),
            "rule_snapshot_sha256": str(row.get("rule_snapshot_sha256") or ""),
        })
    markets.sort(key=lambda row: (row["end_timestamp"], row["asset"], row["horizon"], row["market_id"]))
    if not markets:
        raise ValueError("no verified open multi-crypto markets")
    if len(markets) > maximum_markets:
        raise ValueError("verified market set exceeds bounded observer capacity")
    identity_rows = [{k: row[k] for k in (
        "asset", "horizon", "market_id", "event_id", "yes_token", "no_token",
        "start_timestamp", "end_timestamp", "start_timestamp_ms", "end_timestamp_ms",
        "normalized_rules_hash",
    )} for row in markets]
    generation = hashlib.sha256(json.dumps(
        identity_rows, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "schema": SCHEMA,
        "version": 1,
        "model_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "selection_only": True,
        "generated_at_ms": int(now_unix * 1000),
        "source_discovery_fetched_at_ms": int(snapshot.get("fetched_at_ms") or 0),
        "generation_sha256": generation,
        "market_count": len(markets),
        "token_count": 2 * len(markets),
        "rejected_records": rejected,
        "markets": markets,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--now-unix", type=float, default=0.0)
    parser.add_argument("--maximum-markets", type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.maximum_markets <= 40:
        raise ValueError("maximum-markets must be in [1, 40]")
    selection = build_selection(
        load_json(args.discovery), model_sha=args.model_sha,
        now_unix=args.now_unix or time.time(),
        maximum_markets=args.maximum_markets,
    )
    atomic_json(args.output, selection)
    print(json.dumps(selection, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
