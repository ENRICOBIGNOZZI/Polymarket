#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

LIVE_ORDER_STATES = {"RESTING", "CANCEL_PENDING"}
FINAL_BUNDLE_STATES = {"CLOSED", "UNWOUND", "CANCELLED"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def leg_commitment(row: dict[str, str]) -> float:
    entry = max(0.0, finite(row.get("entry_cash")))
    if str(row.get("exited") or "0") in {"1", "true", "True"}:
        return 0.0
    state = str(row.get("order_state") or "").strip().upper()
    if state not in LIVE_ORDER_STATES:
        return entry
    target = max(0.0, finite(row.get("target_shares")))
    filled = max(0.0, finite(row.get("filled_shares")))
    price = max(0.0, finite(row.get("limit_price")))
    return entry + max(0.0, target - filled) * price


def audit(intent_rows: list[dict[str, str]], leg_rows: list[dict[str, str]]) -> dict[str, Any]:
    intent_events: dict[str, set[str]] = defaultdict(set)
    for row in intent_rows:
        bundle = str(row.get("bundle_id") or "").strip()
        event = str(row.get("event_id") or "").strip()
        if bundle and event:
            intent_events[bundle].add(event)

    persisted: dict[str, float] = defaultdict(float)
    canonical: dict[str, float] = defaultdict(float)
    by_bundle: dict[str, list[dict[str, str]]] = defaultdict(list)
    unresolved_bundles: set[str] = set()
    for row in leg_rows:
        bundle = str(row.get("bundle_id") or "").strip()
        if not bundle:
            continue
        by_bundle[bundle].append(row)
        amount = leg_commitment(row)
        persisted_id = str(row.get("event_id") or "").strip()
        if persisted_id:
            persisted[persisted_id] += amount
        expected = intent_events.get(bundle, set())
        if len(expected) == 1:
            canonical[next(iter(expected))] += amount
        else:
            unresolved_bundles.add(bundle)

    mismatches: list[dict[str, Any]] = []
    for bundle, rows in sorted(by_bundle.items()):
        expected = sorted(intent_events.get(bundle, set()))
        observed = sorted({str(row.get("event_id") or "").strip() for row in rows if row.get("event_id")})
        if len(expected) == 1 and (len(observed) != 1 or observed[0] != expected[0]):
            amount = sum(leg_commitment(row) for row in rows)
            largest = max((leg_commitment(row) for row in rows), default=0.0)
            mismatches.append(
                {
                    "bundle_id": bundle,
                    "intent_event_id": expected[0],
                    "persisted_leg_event_ids": observed,
                    "commitment_usd": amount,
                    "largest_persisted_bucket_usd": largest,
                    "fragmentation_ratio": amount / largest if largest > 0.0 else None,
                }
            )

    return {
        "bundles": len(by_bundle),
        "mismatch_bundles": len(mismatches),
        "mismatches": mismatches,
        "persisted_event_commitment_usd": dict(sorted(persisted.items())),
        "intent_event_commitment_usd": dict(sorted(canonical.items())),
        "unresolved_intent_event_bundles": sorted(unresolved_bundles),
    }


def fragmented_cap_allows(commitments: list[float], event_cap_usd: float) -> bool:
    """Return true when per-fragment checks pass but the canonical event total breaches the cap."""
    cap = max(0.0, float(event_cap_usd))
    values = [max(0.0, float(value)) for value in commitments]
    return bool(values) and max(values) <= cap + 1e-12 and sum(values) > cap + 1e-12


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit V6 multi-leg event-risk identity fragmentation")
    parser.add_argument("--intents", type=Path, required=True)
    parser.add_argument("--legs", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(read_csv(args.intents), read_csv(args.legs))
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if result["mismatch_bundles"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
