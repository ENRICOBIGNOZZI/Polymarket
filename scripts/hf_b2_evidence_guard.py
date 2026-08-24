#!/usr/bin/env python3
"""Fail-closed interpretation guard for B2 forward-execution evidence.

The B2 forward probe is read-only. This guard prevents coarse timestamp/book
sampling from being mistaken for independent sub-second latency evidence and
keeps post-decision diagnostics separate from predictor-safe features.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _result_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        bool(row.get("full_completion")),
        int(finite(row.get("filled_legs"), 0.0)),
        round(finite(row.get("bundle_completion_fraction")), 12),
        round(finite(row.get("force_completion_cost_usd")), 12),
        round(finite(row.get("filled_markout_60_usd")), 12),
        round(finite(row.get("filled_markout_300_usd")), 12),
    )


def assess_probe(payload: dict[str, Any], trade_timestamp_resolution_ms: float = 1000.0) -> dict[str, Any]:
    method = payload.get("method") if isinstance(payload.get("method"), dict) else {}
    raw_latencies = method.get("arrival_latencies_ms") if isinstance(method, dict) else []
    latencies = sorted({finite(x, -1.0) for x in raw_latencies or [] if finite(x, -1.0) >= 0.0})
    latency_span_ms = (max(latencies) - min(latencies)) if len(latencies) >= 2 else 0.0

    start = finite(payload.get("quote_start_ts"), 0.0)
    end = finite(payload.get("quote_end_ts"), start)
    snapshots = max(0, int(finite(payload.get("book_snapshots"), 0.0)))
    average_snapshot_interval_ms: float | None = None
    if snapshots > 1 and end > start:
        average_snapshot_interval_ms = 1000.0 * (end - start) / float(snapshots - 1)

    trade_resolution = max(0.0, finite(trade_timestamp_resolution_ms, 1000.0))
    trade_cutoff_resolved = bool(latencies) and latency_span_ms + 1e-9 >= trade_resolution
    arrival_book_resolved = (
        bool(latencies)
        and average_snapshot_interval_ms is not None
        and latency_span_ms + 1e-9 >= average_snapshot_interval_ms
    )
    independent_latency_evidence = trade_cutoff_resolved and arrival_book_resolved

    groups: dict[tuple[str, str, float], dict[float, tuple[Any, ...]]] = defaultdict(dict)
    rows = payload.get("results") if isinstance(payload.get("results"), list) else []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        key = (
            str(raw.get("candidate_market") or ""),
            str(raw.get("policy") or ""),
            finite(raw.get("queue_multiplier"), 0.0),
        )
        latency = finite(raw.get("arrival_latency_ms"), -1.0)
        if latency >= 0.0:
            groups[key][latency] = _result_signature(raw)

    multi_latency_groups = 0
    outcome_distinct_groups = 0
    for values in groups.values():
        if len(values) < 2:
            continue
        multi_latency_groups += 1
        if len(set(values.values())) > 1:
            outcome_distinct_groups += 1

    legacy_ofi_present = any(
        isinstance(row, dict) and "snapshot_ofi_per_second" in row for row in rows
    )
    predictor_safe_fields = [
        "initial_weighted_imbalance_l1",
        "initial_weighted_microprice_minus_mid",
    ]
    post_decision_diagnostics = [
        "filled_markout_60_usd",
        "filled_markout_300_usd",
        "bundle_completion_fraction",
        "force_completion_cost_usd",
    ]
    if legacy_ofi_present:
        post_decision_diagnostics.append("snapshot_ofi_per_second")

    reasons: list[str] = []
    if not trade_cutoff_resolved:
        reasons.append(
            "requested latency span is below the trade timestamp resolution used by the replay"
        )
    if not arrival_book_resolved:
        reasons.append(
            "requested latency span is below the observed REST book-snapshot interval; arrival-book state is not identified"
        )
    if legacy_ofi_present:
        reasons.append(
            "snapshot_ofi_per_second is accumulated after quote placement and is an outcome diagnostic, not a causal predictor"
        )

    return {
        "schema": "polymarket_hf_b2_evidence_guard_v1",
        "evidence_state": "MORE_EVIDENCE_REQUIRED",
        "latency": {
            "requested_scenarios_ms": latencies,
            "requested_span_ms": latency_span_ms,
            "trade_timestamp_resolution_ms": trade_resolution,
            "average_book_snapshot_interval_ms": average_snapshot_interval_ms,
            "trade_cutoff_resolved": trade_cutoff_resolved,
            "arrival_book_resolved": arrival_book_resolved,
            "latency_scenarios_count_as_independent_evidence": independent_latency_evidence,
            "multi_latency_comparison_groups": multi_latency_groups,
            "groups_with_distinct_observed_outcomes": outcome_distinct_groups,
        },
        "causality": {
            "predictor_safe_at_quote_start": predictor_safe_fields,
            "post_decision_diagnostics_not_predictor_safe": post_decision_diagnostics,
        },
        "reasons": reasons,
        "next_measurement": (
            "Capture the actual B2 token books in event time at or before quote decision and through order arrival, "
            "with timestamp resolution finer than the latency scenarios; then compute causal pre-arrival OFI and "
            "replay queue/fill/markout on those same legs."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trade-timestamp-resolution-ms", type=float, default=1000.0)
    args = parser.parse_args()
    payload = json.loads(args.probe.read_text(encoding="utf-8"))
    report = assess_probe(payload, args.trade_timestamp_resolution_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
