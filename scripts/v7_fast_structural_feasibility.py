#!/usr/bin/env python3
"""Audit the Fast Structural feasibility funnel, capacity, and latency reach."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any, Iterable

SCHEMA = "polymarket_v7_fast_structural_feasibility_report_v1"
STRATEGY = "FAST_STRUCTURAL"


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _paths(inputs: Iterable[Path]) -> list[Path]:
    found: set[Path] = set()
    for raw in inputs:
        path = Path(raw)
        if path.is_file() and path.suffix == ".jsonl":
            found.add(path.resolve())
        elif path.is_dir():
            found.update(item.resolve() for item in path.rglob("execution.jsonl"))
    return sorted(found)


def load_records(inputs: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    conflicts = malformed = 0
    paths = _paths(inputs)
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(row, dict) or str(row.get("strategy") or "").upper() != STRATEGY:
                continue
            record_id = str(row.get("record_id") or "")
            if not record_id:
                malformed += 1
                continue
            prior = unique.get(record_id)
            if prior is None:
                unique[record_id] = row
            elif prior != row:
                conflicts += 1
    rows = sorted(unique.values(), key=lambda row: (
        int(row.get("recorded_ts_ms") or 0), str(row.get("record_id") or ""),
    ))
    return rows, {
        "paths": [str(path) for path in paths], "records": len(rows),
        "malformed": malformed, "conflicts": conflicts,
        "fail_closed": bool(malformed or conflicts),
    }


def _nearest_rank(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))]


def _latency_p99(report: dict[str, Any]) -> float | None:
    components = report.get("components") if isinstance(report.get("components"), dict) else {}
    decision = components.get("decision_to_arrival") if isinstance(
        components.get("decision_to_arrival"), dict) else {}
    return _finite(decision.get("p99_ms"))


def build_report(records: list[dict[str, Any]], quality: dict[str, Any], *,
                 p99_latency_ms: float | None = None) -> dict[str, Any]:
    candidates = [row for row in records if row.get("event_type") == "CANDIDATE"]
    observations: dict[str, dict[str, Any]] = {}
    bundle_for_candidate: dict[str, str] = {}
    for row in records:
        if row.get("event_type") != "OPPORTUNITY":
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        value = metadata.get("fast_structural_feasibility")
        if not isinstance(value, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        if candidate_id:
            observations.setdefault(candidate_id, value)
            bundle_for_candidate[candidate_id] = str(row.get("bundle_id") or "")
    fills_by_bundle: dict[str, set[str]] = {}
    finals: set[str] = set()
    for row in records:
        bundle_id = str(row.get("bundle_id") or "")
        if row.get("event_type") == "FILL" and bundle_id and row.get("leg_id"):
            fills_by_bundle.setdefault(bundle_id, set()).add(str(row["leg_id"]))
        elif row.get("event_type") == "FINAL" and bundle_id:
            finals.add(bundle_id)

    funnel = {
        "detected": len(candidates), "structurally_valid": 0,
        "full_depth_positive": 0, "positive_after_fees": 0,
        "positive_after_latency": 0, "all_legs_filled": 0, "terminal": 0,
    }
    tau_values: list[float] = []
    q_values: list[float] = []
    inaccessible = 0
    opportunity_rows = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or candidate.get("record_id") or "")
        value = observations.get(candidate_id)
        if value is None:
            opportunity_rows.append({
                "candidate_id": candidate_id, "state": "FEASIBILITY_OBSERVATION_MISSING",
            })
            continue
        for key in ("structurally_valid", "full_depth_positive",
                    "positive_after_fees", "positive_after_latency"):
            funnel[key] += int(value.get(key) is True)
        tau = _finite(value.get("tau_star_ms"))
        quantity = _finite(value.get("q_star"))
        if tau is not None and tau >= 0.0:
            tau_values.append(tau)
            if p99_latency_ms is not None and p99_latency_ms > tau:
                inaccessible += 1
        if quantity is not None and quantity >= 0.0:
            q_values.append(quantity)
        bundle_id = bundle_for_candidate.get(candidate_id, "")
        expected_legs = 0
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        if isinstance(metadata.get("structured_legs"), list):
            expected_legs = len(metadata["structured_legs"])
        all_filled = bool(
            bundle_id and expected_legs > 0
            and len(fills_by_bundle.get(bundle_id, set())) >= expected_legs
        )
        funnel["all_legs_filled"] += int(all_filled)
        funnel["terminal"] += int(bundle_id in finals)
        opportunity_rows.append({
            "candidate_id": candidate_id, "bundle_id": bundle_id,
            "structurally_valid": value.get("structurally_valid") is True,
            "full_depth_positive": value.get("full_depth_positive") is True,
            "positive_after_fees": value.get("positive_after_fees") is True,
            "positive_after_latency": value.get("positive_after_latency") is True,
            "q_star": quantity, "tau_star_ms": tau,
            "p99_latency_exceeds_tau_star": bool(
                tau is not None and p99_latency_ms is not None and p99_latency_ms > tau),
            "all_legs_filled": all_filled, "terminal": bundle_id in finals,
            "capacity_curve": value.get("capacity_curve") or [],
        })
    tau_eligible = len(tau_values)
    inaccessible_fraction = inaccessible / tau_eligible if tau_eligible else None
    report = {
        "schema": SCHEMA, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "advisory_only": True,
        "automatic_strategy_state_change": False,
        "source_quality": quality, "funnel": funnel,
        "opportunities": opportunity_rows,
        "latency": {
            "p99_ms": p99_latency_ms,
            "tau_star_count": tau_eligible,
            "tau_star_p50_ms": _nearest_rank(tau_values, 0.50),
            "tau_star_p90_ms": _nearest_rank(tau_values, 0.90),
            "tau_star_p99_ms": _nearest_rank(tau_values, 0.99),
            "p99_exceeds_tau_star_count": inaccessible,
            "p99_exceeds_tau_star_fraction": inaccessible_fraction,
        },
        "capacity": {
            "q_star_count": len(q_values),
            "q_star_p50": _nearest_rank(q_values, 0.50),
            "q_star_p10": _nearest_rank(q_values, 0.10),
        },
        "freeze_recommended": bool(
            tau_eligible >= 20 and inaccessible_fraction is not None
            and inaccessible_fraction >= 0.80),
        "freeze_rule": "P99_LATENCY_EXCEEDS_TAU_STAR_FOR_AT_LEAST_80_PERCENT_OF_20_OPPORTUNITIES",
        "promotion_eligible": bool(
            funnel["terminal"] >= 50 and not quality.get("fail_closed")
            and not (tau_eligible >= 20 and inaccessible_fraction is not None
                     and inaccessible_fraction >= 0.80)),
        "minimum_terminal_bundles": 50,
        "automatic_promotion": False,
    }
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
    report["content_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    return report


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--latency-report", type=Path)
    parser.add_argument("--p99-latency-ms", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    latency = args.p99_latency_ms
    if latency is None and args.latency_report:
        latency = _latency_p99(json.loads(args.latency_report.read_text(encoding="utf-8")))
    records, quality = load_records(args.input)
    report = build_report(records, quality, p99_latency_ms=latency)
    atomic_json(args.output, report)
    print(json.dumps({
        "detected": report["funnel"]["detected"],
        "terminal": report["funnel"]["terminal"],
        "freeze_recommended": report["freeze_recommended"],
        "promotion_eligible": report["promotion_eligible"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
