#!/usr/bin/env python3
"""Read-only signal-to-PAPER-arrival latency report for lead-lag evidence.

Legacy wall-clock stages are reported only as same-recorder descriptive deltas;
host/boot identity is absent, so they are not promoted to cross-host one-way
latency. The +1ms ledger recording convention is classified as synthetic
ordering and excluded from performance percentiles. No exchange ACK or real
fill time is invented.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Any, Iterable

from v7_execution_ledger import LedgerEvent, iter_events
from v7_lead_lag_replay import ReplayError, digest

SCHEMA = "polymarket_v7_lead_lag_latency_stage_report_v1"
FAMILY = "lead_lag_taker_v1"


def finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def quantile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    xs = sorted(values); pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return int(round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)))


def stage_summary(values: list[int]) -> dict[str, Any]:
    n = len(values)
    thresholds = {"p50": 1, "p90": 10, "p95": 20, "p99": 100, "p99_9": 1000}
    qs = {"p50": .5, "p90": .9, "p95": .95, "p99": .99, "p99_9": .999}
    result = {"samples": n, "max": max(values) if values else None}
    for name, q in qs.items():
        result[name] = quantile(values, q) if n >= thresholds[name] else None
        result[name + "_minimum_samples_met"] = n >= thresholds[name]
    return result


def read_runtime_events(path: Path | None) -> tuple[dict[str, dict[str, Any]], Counter[str], int]:
    candidates: dict[str, dict[str, Any]] = {}
    reasons: Counter[str] = Counter(); rows = 0
    if path is None:
        return candidates, reasons, rows
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            rows += 1
            raw = json.loads(line)
            if not isinstance(raw, dict) or raw.get("model_family") not in (None, FAMILY):
                continue
            event = str(raw.get("event") or "")
            if event == "CANDIDATE":
                key = str(raw.get("replay_key") or "")
                timestamp = raw.get("timestamp_ns")
                if key and isinstance(timestamp, int) and timestamp > 0:
                    prior = candidates.get(key)
                    if prior is not None and prior != raw:
                        raise ReplayError("LATENCY_CANDIDATE_REDEFINED:" + key)
                    candidates[key] = raw
            elif event == "REJECTED":
                reasons[str(raw.get("reason") or "UNKNOWN")] += 1
    return candidates, reasons, rows


def summarize(events: Iterable[LedgerEvent], *, runtime_events: Path | None = None) -> dict[str, Any]:
    candidates, reject_reasons, runtime_rows = read_runtime_events(runtime_events)
    fills = []
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if event.event_type == "FILL" and metadata.get("model_family") == FAMILY:
            fills.append(event)
    stages: dict[str, list[int]] = {
        "signal_to_candidate_wall_ns": [],
        "candidate_to_coordinator_decision_wall_ns": [],
        "coordinator_to_paper_arrival_decision_wall_ns": [],
        "signal_to_paper_arrival_decision_wall_ns": [],
        "book_receive_to_paper_arrival_decision_ns": [],
        "signal_age_capture_to_paper_arrival_decision_ns": [],
    }
    missing = Counter(); inconsistencies = Counter(); synthetic_offsets: list[int] = []
    rows: list[dict[str, Any]] = []
    for fill in fills:
        md = fill.metadata if isinstance(fill.metadata, dict) else {}
        trigger = md.get("signal_trigger_wall_ns")
        receipt = md.get("coordinator_receipt") if isinstance(md.get("coordinator_receipt"), dict) else {}
        coordinator_ns = receipt.get("decision_timestamp_ns")
        candidate = candidates.get(str(fill.opportunity_id or ""))
        candidate_ns = candidate.get("timestamp_ns") if isinstance(candidate, dict) else None
        decision_ns = int(fill.decision_ts_ms) * 1_000_000 if fill.decision_ts_ms else None
        receive_ns = int(fill.receive_ts_ms) * 1_000_000 if fill.receive_ts_ms else None
        record_ns = int(fill.recorded_ts_ms) * 1_000_000

        def add(stage: str, start: Any, end: Any) -> int | None:
            if not isinstance(start, int) or not isinstance(end, int) or start <= 0 or end <= 0:
                missing[stage] += 1; return None
            if end < start:
                inconsistencies[stage + ":NEGATIVE"] += 1; return None
            value = end - start; stages[stage].append(value); return value

        s_candidate = add("signal_to_candidate_wall_ns", trigger, candidate_ns)
        c_coord = add("candidate_to_coordinator_decision_wall_ns", candidate_ns, coordinator_ns)
        coord_arrival = add("coordinator_to_paper_arrival_decision_wall_ns", coordinator_ns, decision_ns)
        signal_arrival = add("signal_to_paper_arrival_decision_wall_ns", trigger, decision_ns)
        book_arrival = add("book_receive_to_paper_arrival_decision_ns", receive_ns, decision_ns)
        synthetic = record_ns - decision_ns if decision_ns is not None else None
        if synthetic is not None:
            synthetic_offsets.append(synthetic)
            if synthetic != 1_000_000:
                inconsistencies["RECORDING_OFFSET_NOT_LEGACY_PLUS_1MS"] += 1
        declared_age_ms = finite(md.get("signal_age_ms_at_fill"))
        age_capture_gap = None
        if isinstance(trigger, int) and trigger > 0 and declared_age_ms is not None and decision_ns is not None:
            age_capture_ns = trigger + int(round(declared_age_ms * 1_000_000.0))
            age_capture_gap = add("signal_age_capture_to_paper_arrival_decision_ns", age_capture_ns, decision_ns)
        rows.append({
            "fill_id": fill.fill_id, "market_id": fill.market_id,
            "signal_to_candidate_wall_ns": s_candidate,
            "candidate_to_coordinator_decision_wall_ns": c_coord,
            "coordinator_to_paper_arrival_decision_wall_ns": coord_arrival,
            "signal_to_paper_arrival_decision_wall_ns": signal_arrival,
            "book_receive_to_paper_arrival_decision_ns": book_arrival,
            "signal_age_capture_to_paper_arrival_decision_ns": age_capture_gap,
            "ledger_recording_offset_ns": synthetic,
            "observed_exchange_execution_time": None, "exchange_ack": None,
        })
    result = {
        "schema": SCHEMA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "execution_evidence": "PAPER_SIMULATION_ONLY", "clock_domain_proven": False,
        "wall_clock_stage_semantics": "DESCRIPTIVE_SAME_RECORDER_ONLY_NOT_CROSS_HOST_ONE_WAY_LATENCY",
        "fill_count": len(fills), "runtime_event_rows": runtime_rows,
        "candidate_event_count": len(candidates), "runtime_rejection_reason_counts": dict(sorted(reject_reasons.items())),
        "stages_ns": {name: stage_summary(values) for name, values in stages.items()},
        "missing_stage_counts": dict(sorted(missing.items())),
        "inconsistency_counts": dict(sorted(inconsistencies.items())),
        "synthetic_ledger_recording_offset_ns": stage_summary(synthetic_offsets),
        "synthetic_recording_offset_excluded_from_latency_claims": True,
        "legacy_signal_age_semantics": "CAPTURED_BEFORE_FINAL_BOOK_REVALIDATION_NOT_FILL_LATENCY",
        "exchange_ack_available": False, "real_fill_time_available": False,
        "coordinated_omission_note": "candidate/rejection counts are retained; fill-only latency is not the opportunity denominator",
        "rows": rows,
    }
    result["report_hash"] = digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--runtime-events", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or "ledger" in args.output.parts:
        parser.exit(2, "refusing overwrite or canonical ledger destination\n")
    try:
        report = summarize(iter_events(args.ledger, expected_model_sha=args.model_sha), runtime_events=args.runtime_events)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False); handle.write("\n")
        print(json.dumps({"fills": report["fill_count"], "candidate_events": report["candidate_event_count"],
                          "exchange_ack_available": False, "report_hash": report["report_hash"]}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"latency stage report failed closed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
