#!/usr/bin/env python3
"""Fail-closed V7 fast-arbitrage evidence gate."""
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


def parse_freshness(raw: str) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for item in (raw or "").split("|"):
        if not item:
            continue
        fields = item.rsplit(":", 2)
        if len(fields) != 3:
            return []
        token, exchange, received = fields
        try:
            out.append((token, int(exchange), int(received)))
        except ValueError:
            return []
    return out


def assess(status: dict[str, Any], rows: list[dict[str, str]], policy: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    hard = [row for row in rows if row.get("hard_arbitrage") == "1" and row.get("executable") == "1"]
    max_age = int(policy.get("max_token_age_ms", 5000))
    max_skew = int(policy.get("max_cross_leg_skew_ms", 1000))
    strict = bool(policy.get("strict_multi_leg_evidence_required", True))
    failures: list[dict[str, Any]] = []

    for row in hard:
        decision = int(row.get("decision_ts_ms") or 0)
        freshness = parse_freshness(row.get("per_token_freshness", ""))
        if strict and not freshness:
            failures.append({"id": row.get("id", ""), "reason": "per_token_freshness_not_serialized"})
            continue
        if not freshness:
            continue
        exchanges = [exchange for _, exchange, _ in freshness]
        receives = [received for _, _, received in freshness]
        if decision <= 0 or any(value <= 0 for value in exchanges + receives):
            failures.append({"id": row.get("id", ""), "reason": "invalid_freshness_clock"})
            continue
        if any(received > decision for received in receives):
            failures.append({"id": row.get("id", ""), "reason": "future_receive_timestamp"})
            continue
        if max(decision - received for received in receives) > max_age:
            failures.append({"id": row.get("id", ""), "reason": "receive_age_gate"})
            continue
        if max(decision - exchange for exchange in exchanges) > max_age:
            failures.append({"id": row.get("id", ""), "reason": "exchange_age_gate"})
            continue
        if max(receives) - min(receives) > max_skew:
            failures.append({"id": row.get("id", ""), "reason": "receive_skew_gate"})
            continue
        if max(exchanges) - min(exchanges) > max_skew:
            failures.append({"id": row.get("id", ""), "reason": "exchange_skew_gate"})

    valid = not failures
    state = "NO_HARD_EXECUTABLE_OBSERVATIONS" if not hard else ("STRICT_HARD_EVIDENCE_VALID" if valid else "STRICT_HARD_EVIDENCE_BLOCKED")
    report = {
        "schema_version": 1,
        "mode": "v7_research_shadow",
        "real_order_submission": False,
        "state": state,
        "strict_hard_evidence_valid": bool(hard) and valid,
        "hard_executable_rows": len(hard),
        "failures": failures,
        "policy": {
            "max_token_age_ms": max_age,
            "max_cross_leg_skew_ms": max_skew,
            "require_per_token_receive_timestamp": bool(policy.get("require_per_token_receive_timestamp", True)),
            "require_per_token_exchange_timestamp": bool(policy.get("require_per_token_exchange_timestamp", True)),
        },
        "feed": {
            "ws_messages": int(status.get("ws_messages", 0) or 0),
            "book_updates": int(status.get("book_updates", 0) or 0),
            "feed_stale_ms": int(status.get("feed_stale_ms", -1) or -1),
            "rest_resyncs": int(status.get("rest_resyncs", 0) or 0),
        },
    }
    return report, valid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True)
    parser.add_argument("--opportunities", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    status = load_json(Path(args.status))
    policy = load_json(Path(args.policy))
    with Path(args.opportunities).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    report, valid = assess(status, rows, policy)
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
