#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def rolling_max(rows: list[tuple[int, float]], window: int) -> float:
    if not rows or window <= 0:
        return 0.0
    ordered = sorted(rows)
    left = 0
    total = 0.0
    best = 0.0
    for right, (clock, size) in enumerate(ordered):
        total += max(0.0, size)
        cutoff = clock - window
        while left <= right and ordered[left][0] < cutoff:
            total -= max(0.0, ordered[left][1])
            left += 1
        best = max(best, total)
    return best


def compatible_prior(
    tape_rows: list[dict[str, str]],
    *,
    token_id: str,
    limit_price: float,
    decision_ms: int,
    lookback_seconds: int,
) -> list[dict[str, str]]:
    start_s = decision_ms // 1000 - max(1, int(lookback_seconds))
    end_s = decision_ms // 1000
    out: list[dict[str, str]] = []
    for row in tape_rows:
        if str(row.get("asset_id") or row.get("token_id") or "") != token_id:
            continue
        if str(row.get("side") or "").upper() != "SELL":
            continue
        received_ms = integer(row.get("received_ms"), 0)
        event_s = integer(row.get("timestamp"), 0)
        price = finite(row.get("price"), math.nan)
        size = finite(row.get("size"), 0.0)
        if received_ms <= 0 or event_s <= 0 or size <= 0.0 or not math.isfinite(price):
            continue
        # Causal availability: the decision may use only rows already received.
        if received_ms >= decision_ms:
            continue
        # Market-activity window: preserve the exchange/event-time spacing of those known rows.
        if event_s < start_s or event_s >= end_s:
            continue
        if price > limit_price + 1e-12:
            continue
        out.append(row)
    return out


def leg_audit(
    leg: dict[str, str],
    tape_rows: list[dict[str, str]],
    *,
    lookback_seconds: int,
    execution_window_seconds: int,
) -> dict[str, Any]:
    token_id = str(leg.get("token_id") or "")
    decision_ms = integer(leg.get("arrival_ms"), 0)
    limit_price = finite(leg.get("limit_price"), math.nan)
    queue_ahead = max(0.0, finite(leg.get("queue_ahead"), 0.0))
    target_shares = max(0.0, finite(leg.get("target_shares"), 0.0))
    required = queue_ahead + target_shares
    rows = compatible_prior(
        tape_rows,
        token_id=token_id,
        limit_price=limit_price,
        decision_ms=decision_ms,
        lookback_seconds=lookback_seconds,
    )
    receive_rows = [(integer(x.get("received_ms"), 0), finite(x.get("size"), 0.0)) for x in rows]
    event_rows = [(integer(x.get("timestamp"), 0), finite(x.get("size"), 0.0)) for x in rows]
    receive_max = rolling_max(receive_rows, execution_window_seconds * 1000)
    event_max = rolling_max(event_rows, execution_window_seconds)
    inflation = receive_max / event_max if event_max > 1e-12 else (math.inf if receive_max > 1e-12 else 1.0)
    return {
        "market_id": str(leg.get("market_id") or ""),
        "token_id": token_id,
        "known_compatible_rows": len(rows),
        "required_queue_plus_target": required,
        "receive_clock_max_window_flow": receive_max,
        "event_clock_max_window_flow": event_max,
        "receive_clock_capacity_ratio": receive_max / required if required > 1e-12 else 0.0,
        "event_clock_capacity_ratio": event_max / required if required > 1e-12 else 0.0,
        "receive_over_event_flow_inflation": inflation,
    }


def audit(
    legs_rows: list[dict[str, str]],
    tape_rows: list[dict[str, str]],
    *,
    lookback_seconds: int = 900,
    execution_window_seconds: int = 180,
) -> dict[str, Any]:
    bundles: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in legs_rows:
        bundle_id = str(row.get("bundle_id") or "")
        if bundle_id:
            bundles[bundle_id].append(row)

    received = [integer(x.get("received_ms"), 0) for x in tape_rows if integer(x.get("received_ms"), 0) > 0]
    events = [integer(x.get("timestamp"), 0) for x in tape_rows if integer(x.get("timestamp"), 0) > 0]
    bundle_reports = []
    for bundle_id, legs in sorted(bundles.items()):
        reports = [
            leg_audit(
                leg,
                tape_rows,
                lookback_seconds=lookback_seconds,
                execution_window_seconds=execution_window_seconds,
            )
            for leg in legs
        ]
        bundle_reports.append({
            "bundle_id": bundle_id,
            "legs": reports,
            "max_receive_over_event_flow_inflation": max(
                (finite(x["receive_over_event_flow_inflation"], 1.0) for x in reports),
                default=1.0,
            ),
            "all_legs_have_known_compatible_flow": bool(reports) and all(x["known_compatible_rows"] > 0 for x in reports),
        })

    return {
        "schema": "lf_v6_dual_clock_flow_audit_v1",
        "paper_only": True,
        "tape_rows": len(tape_rows),
        "unique_receive_timestamps": len(set(received)),
        "receive_span_ms": max(received) - min(received) if received else 0,
        "event_span_seconds": max(events) - min(events) if events else 0,
        "lookback_seconds": lookback_seconds,
        "execution_window_seconds": execution_window_seconds,
        "bundles": bundle_reports,
        "contract": {
            "causal_availability_clock": "received_ms",
            "market_activity_window_clock": "timestamp",
            "forward_fill_requires": "received after order arrival AND event timestamp after order arrival, within execution deadline",
        },
        "interpretation": (
            "received_ms is a knowledge/ordering clock, not a market-activity clock when REST polling backfills many historical trades in one response. "
            "Queue-clearance and recurrence windows must preserve event-time spacing after filtering to rows causally available by received_ms."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit dual-clock public-flow semantics for V6 LF/Graph paper execution")
    parser.add_argument("--legs", type=Path, required=True)
    parser.add_argument("--trade-tape", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lookback-seconds", type=int, default=900)
    parser.add_argument("--execution-window-seconds", type=int, default=180)
    args = parser.parse_args()
    report = audit(
        load_csv(args.legs),
        load_csv(args.trade_tape),
        lookback_seconds=args.lookback_seconds,
        execution_window_seconds=args.execution_window_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"paper_only": True, "tape_rows": report["tape_rows"], "bundles": len(report["bundles"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
