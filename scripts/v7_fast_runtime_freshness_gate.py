#!/usr/bin/env python3
"""Validate research-only Fast Structural evidence produced by the per-leg L2 gate.

This gate deliberately does not promote observations to canonical execution evidence:
current Fast CSV rows retain only conservative aggregate opportunity clocks.  It verifies
that the exact runtime source enforces per-token exchange/receive freshness before every
structural evaluator, and that emitted hard rows satisfy the resulting aggregate bounds.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def source_contract(root: Path) -> list[str]:
    p2 = (root / "src/fast_runtime/part2.inc").read_text(encoding="utf-8")
    p3 = (root / "src/fast_runtime/part3.inc").read_text(encoding="utf-8")
    ws = (root / "src/fast_ws.cpp").read_text(encoding="utf-8")
    required = {
        "per-token exchange clocks": "book_exchange_ts_ms_" in p3,
        "per-token receive clocks": "book_received_ts_ms_" in p3,
        "full WS lineage": "ws_snapshot_ready_" in p3,
        "binary freshness": "{market.yes_token, market.no_token}, decision" in p3,
        "NegRisk freshness": "freshness_window_locked(required_tokens, decision)" in p3,
        "relation freshness": "right->second.yes_token" in p3 and "freshness_window_locked(" in p3,
        "REST cannot refresh WS lineage": "if (fresh_ws) continue;" in p2,
        "reconnect invalidates lineage": "ws_snapshot_ready_.erase(token);" in p2,
        "clean close enters invalidation path": "websocket closed; reconnecting and invalidating L2 lineage" in ws,
        "stale current opportunity expires": "stale_or_unsynchronized_leg_book" in p2,
    }
    return [name for name, present in required.items() if not present]


def assess(root: Path, status: dict[str, Any], rows: list[dict[str, str]], policy: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    failures: list[dict[str, Any]] = []
    source_failures = source_contract(root)
    if source_failures:
        failures.append({"reason": "source_contract", "missing": source_failures})

    max_age = int(policy.get("max_token_age_ms", 0) or 0)
    max_skew = int(policy.get("max_cross_leg_skew_ms", 0) or 0)
    if not policy.get("paper_only", False) or policy.get("real_order_submission", True):
        failures.append({"reason": "unsafe_policy_mode"})
    if not policy.get("strict_multi_leg_evidence_required", False):
        failures.append({"reason": "strict_multi_leg_gate_disabled"})
    if max_age <= 0 or max_skew <= 0:
        failures.append({"reason": "invalid_freshness_policy"})

    status_age = int(status.get("book_freshness_max_age_ms", 0) or 0)
    status_skew = int(status.get("book_freshness_max_skew_ms", 0) or 0)
    if status.get("mode") != "shadow" or status.get("real_order_submission") is not False:
        failures.append({"reason": "unsafe_runtime_mode"})
    if status_age <= 0 or status_age > max_age:
        failures.append({"reason": "runtime_age_bound_mismatch", "status": status_age, "policy": max_age})
    if status_skew <= 0 or status_skew > max_skew:
        failures.append({"reason": "runtime_skew_bound_mismatch", "status": status_skew, "policy": max_skew})
    if int(status.get("ws_messages", 0) or 0) > 0 and int(status.get("ws_snapshot_ready_tokens", 0) or 0) <= 0:
        failures.append({"reason": "no_ws_snapshot_ready_tokens"})

    hard = [row for row in rows if row.get("hard_arbitrage") == "1" and row.get("executable") == "1"]
    for row in hard:
        try:
            exchange = int(row.get("exchange_ts_ms") or 0)
            received = int(row.get("received_ts_ms") or 0)
            decision = int(row.get("decision_ts_ms") or 0)
        except ValueError:
            failures.append({"id": row.get("id", ""), "reason": "invalid_aggregate_clock"})
            continue
        if min(exchange, received, decision) <= 0:
            failures.append({"id": row.get("id", ""), "reason": "missing_aggregate_clock"})
            continue
        if received > decision:
            failures.append({"id": row.get("id", ""), "reason": "future_receive_clock"})
            continue
        if exchange > received + max_skew:
            failures.append({"id": row.get("id", ""), "reason": "exchange_receive_clock_skew"})
            continue
        if decision - exchange > max_age:
            failures.append({"id": row.get("id", ""), "reason": "aggregate_exchange_age"})
            continue
        if decision - received > max_age:
            failures.append({"id": row.get("id", ""), "reason": "aggregate_receive_age"})

    valid = not failures
    if not hard:
        state = "NO_FRESH_HARD_EXECUTABLE_OBSERVATIONS" if valid else "RUNTIME_FRESHNESS_EVIDENCE_BLOCKED"
    else:
        state = "INTERNALLY_FRESHNESS_GATED_HARD_OBSERVATIONS" if valid else "RUNTIME_FRESHNESS_EVIDENCE_BLOCKED"
    report = {
        "schema_version": 1,
        "mode": "v7_research_shadow",
        "state": state,
        "hard_executable_rows": len(hard),
        "internal_per_leg_freshness_gate_valid": valid,
        "canonical_per_leg_provenance_serialized": False,
        "promotion_allowed": False,
        "failures": failures,
        "runtime": {
            "book_freshness_max_age_ms": status_age,
            "book_freshness_max_skew_ms": status_skew,
            "ws_snapshot_ready_tokens": int(status.get("ws_snapshot_ready_tokens", 0) or 0),
            "freshness_missing_rejections": int(status.get("freshness_missing_rejections", 0) or 0),
            "freshness_age_rejections": int(status.get("freshness_age_rejections", 0) or 0),
            "freshness_skew_rejections": int(status.get("freshness_skew_rejections", 0) or 0),
        },
        "interpretation": "Research hard rows have passed the runtime's per-leg L2 gate, but canonical promotion remains blocked until exact per-leg provenance and PAPER execution outcomes are written to the V7 ledger.",
    }
    return report, valid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--status", required=True)
    parser.add_argument("--opportunities", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    status = load_json(Path(args.status))
    policy = load_json(Path(args.policy))
    with Path(args.opportunities).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    report, valid = assess(root, status, rows, policy)
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
