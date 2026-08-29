#!/usr/bin/env python3
"""Route valid External Fair snapshots into canonical V7 shadow evidence.

This component emits opportunity/candidate evidence through the single ledger
spool. It intentionally cannot emit orders or fills while the fair model is a
cold-start challenger; that promotion requires independent settled-contract
evidence and is never inferred from a live signal.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import spool_event


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def fee_per_share(price: float, schedule: dict[str, Any]) -> float:
    if not 0.0 < price < 1.0:
        return math.inf
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return math.inf
    if not math.isfinite(rate) or not math.isfinite(exponent) or rate <= 0.0 or exponent < 0.0:
        return math.inf
    return rate * (price * (1.0 - price)) ** exponent


def candidates(status: dict[str, Any], *, minimum_ev: float = 0.001) -> list[dict[str, Any]]:
    if status.get("code_sha") is None or status.get("paper_only") is not True:
        return []
    if status.get("authenticated_execution") is not False or status.get("real_order_submission") is not False:
        return []
    contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
    reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
    oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
    external = status.get("external") if isinstance(status.get("external"), dict) else {}
    fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    if not (contract.get("verified") and contract.get("rules_hash_recognized")
            and reference.get("valid") and oracle.get("healthy") and external.get("healthy")
            and fair.get("valid")):
        return []
    best_bid, best_ask = float(market.get("best_bid") or 0.0), float(market.get("best_ask") or 0.0)
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    rows = []
    for outcome, token, ask, robust_value in (
        ("YES", market.get("yes_token"), best_ask, float(fair.get("lower") or 0.0)),
        ("NO", market.get("no_token"), 1.0 - best_bid, 1.0 - float(fair.get("upper") or 1.0)),
    ):
        fee = fee_per_share(ask, schedule)
        execution_risk = 0.0005
        robust_ev = robust_value - ask - fee - execution_risk
        if token and 0.0 < ask < 1.0 and math.isfinite(robust_ev) and robust_ev >= minimum_ev:
            rows.append({"outcome": outcome, "token_id": str(token), "ask": ask,
                         "fee": fee, "execution_risk": execution_risk, "robust_ev": robust_ev})
    return sorted(rows, key=lambda row: (-row["robust_ev"], row["outcome"]))


def run(run_root: Path, model_sha: str, interval: float) -> None:
    source = run_root / "external_fair" / "status.json"
    status_path = run_root / "external_fair" / "shadow_router_status.json"
    emitted: set[str] = set()
    opportunities = 0
    while True:
        snapshot = load(source)
        now_ms = time.time_ns() // 1_000_000
        active = candidates(snapshot)
        market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
        fair = snapshot.get("fair") if isinstance(snapshot.get("fair"), dict) else {}
        contract = snapshot.get("contract") if isinstance(snapshot.get("contract"), dict) else {}
        reference = snapshot.get("settlement_reference") if isinstance(snapshot.get("settlement_reference"), dict) else {}
        for row in active:
            identity = f"external-fair:{market.get('market_id')}:{row['outcome']}:{now_ms // 5000}"
            if identity in emitted:
                continue
            emitted.add(identity)
            opportunities += 1
            receive_ms = max(1, now_ms - 1)
            metadata = {
                "authority": "SHADOW_ZERO_AUTHORITY",
                "proposed_action": "TAKE",
                "model_maturity": "COLD_START_MORE_EVIDENCE_REQUIRED",
                "contract_rules_hash": contract.get("rules_hash"),
                "reference_version": reference.get("version"),
                "fair_yes": fair.get("yes"), "fair_lower": fair.get("lower"),
                "fair_upper": fair.get("upper"), "expected_fee": row["fee"],
                "expected_execution_risk": row["execution_risk"],
                "execution_blocker": "FAIR_MODEL_NOT_MATURE_NO_ORDER_AUTHORITY",
            }
            spool_event(run_root, LedgerEvent(
                event_type="CANDIDATE", strategy="EXTERNAL_INFORMATION", model_sha=model_sha,
                model_version="external-fair-cold-start-v1", opportunity_id=identity,
                candidate_id=identity, market_id=str(market.get("market_id") or ""),
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"],
                receive_ts_ms=receive_ms, exchange_ts_ms=receive_ms, decision_ts_ms=now_ms,
                book_snapshot_id=f"gamma-touch:{market.get('market_id')}:{receive_ms}",
                side="BUY", ask=row["ask"], predicted_alpha=row["robust_ev"],
                expected_ev=row["robust_ev"], intended_action="SHADOW_TAKE",
                intended_size=0.0, fee=row["fee"], fee_rate=float((market.get("fee_schedule") or {}).get("rate") or 0.0),
                fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE", metadata=metadata,
            ))
        if len(emitted) > 5000:
            emitted = set(sorted(emitted)[-2500:])
        atomic_json(status_path, {
            "schema": "polymarket_v7_external_fair_shadow_router_v1",
            "timestamp": int(time.time()), "code_sha": model_sha, "state": "RUNNING",
            "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
            "execution_authority": "SHADOW_ZERO_AUTHORITY", "model_mature": False,
            "active_candidates": len(active), "candidates_spooled": opportunities,
            "order_submission_enabled": False,
            "blocker": "FAIR_MODEL_NOT_MATURE_NO_ORDER_AUTHORITY",
        })
        time.sleep(max(0.25, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if len(args.model_sha) != 40:
        raise SystemExit("exact model SHA required")
    run(args.run_root.resolve(), args.model_sha, args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
