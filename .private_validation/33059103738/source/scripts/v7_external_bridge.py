#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_SIGNALS = "https://raw.githubusercontent.com/ENRICOBIGNOZZI/Polymarket/telemetry/telemetry/latest-external-signals.jsonl"
DEFAULT_REPORT = "https://raw.githubusercontent.com/ENRICOBIGNOZZI/Polymarket/telemetry/telemetry/latest-external-intelligence.json"
EMPTY_FEED = "market_key,q_yes,confidence,source,timestamp\n"
SCHEMA = "polymarket_v7_external_bridge_status_v3"


def fetch_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-paper/3"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def finite(value: Any, default: float = math.nan) -> float:
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


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def approved_direct_candidate(report: dict[str, Any]) -> dict[str, Any] | None:
    """Return the one exact integration-approved direct-probability candidate."""
    evidence = report.get("alpha_factory_evidence") or {}
    if not isinstance(evidence, dict) or evidence.get("integration_evidence_pass") is not True:
        return None
    approved_id = str(evidence.get("candidate_id") or "").strip()
    if not approved_id:
        return None
    candidates = ((report.get("backtest") or {}).get("candidates") or [])
    matches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and str(candidate.get("candidate_id") or "").strip() == approved_id
        and candidate.get("gate_pass") is True
        and str(candidate.get("feature_name") or "") == "external_probability"
        and str(candidate.get("source") or "").strip()
    ]
    if len(matches) != 1:
        return None
    return dict(matches[0])


def report_fresh(report: dict[str, Any], now: int, max_age_seconds: int) -> tuple[bool, int]:
    generated = integer(report.get("generated_ts"), 0)
    if generated <= 0 or generated > now + 60:
        return False, -1
    age = now - generated
    return age <= max_age_seconds, age


def candidate_provenance(candidate: dict[str, Any]) -> str:
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    source = str(candidate.get("source") or "").strip()
    horizon = integer(candidate.get("horizon_seconds"), 0)
    return f"{source}:external_probability:candidate={candidate_id}:horizon={horizon}"


def materialize(
    report: dict[str, Any],
    signal_text: str,
    *,
    now: int,
    max_age_seconds: int,
    min_confidence: float,
) -> tuple[str, dict[str, Any]]:
    failures: list[str] = []
    fresh, report_age = report_fresh(report, now, max_age_seconds)
    if not fresh:
        failures.append("report_stale_or_invalid_timestamp")
    candidate = approved_direct_candidate(report) if fresh else None
    if candidate is None:
        failures.append("no_exact_integration_approved_direct_probability")
    accepted: dict[str, tuple[float, float, str, int]] = {}
    if candidate is not None:
        approved_source = str(candidate.get("source") or "").strip()
        approved_id = str(candidate.get("candidate_id") or "").strip()
        approved_horizon = integer(candidate.get("horizon_seconds"), 0)
        provenance = candidate_provenance(candidate)
        for raw in signal_text.splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("source") or "").strip() != approved_source:
                continue
            if str(row.get("feature_name") or "") != "external_probability":
                continue
            if str(row.get("candidate_id") or "").strip() != approved_id:
                continue
            if approved_horizon > 0 and integer(row.get("horizon_seconds"), 0) != approved_horizon:
                continue
            probability = finite(row.get("q_external"))
            confidence = finite(row.get("confidence"), 0.0)
            market_id = str(row.get("market_id") or "").strip()
            observed = integer(row.get("observed_ts"), 0)
            if not market_id or not math.isfinite(probability) or not 0.0 < probability < 1.0:
                continue
            if confidence < min_confidence or observed <= 0 or observed > now + 60:
                continue
            if now - observed > max_age_seconds:
                continue
            item = (probability, confidence, provenance, observed)
            current = accepted.get(market_id)
            if current is None or (confidence, observed) > (current[1], current[3]):
                accepted[market_id] = item
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["market_key", "q_yes", "confidence", "source", "timestamp"])
    for market_id, (probability, confidence, provenance, timestamp) in sorted(accepted.items()):
        writer.writerow([market_id, f"{probability:.12g}", f"{confidence:.12g}", provenance, timestamp])
    status = {
        "schema": SCHEMA,
        "timestamp": now,
        "report_age_seconds": report_age,
        "integration_evidence_pass": bool((report.get("alpha_factory_evidence") or {}).get("integration_evidence_pass") is True),
        "approved_candidate_id": "" if candidate is None else str(candidate.get("candidate_id") or ""),
        "approved_horizon_seconds": 0 if candidate is None else integer(candidate.get("horizon_seconds"), 0),
        "materialized_signals": len(accepted),
        "failures": failures,
        "paper_only": True,
        "authenticated_execution": False,
    }
    return output.getvalue(), status


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize exact integration-approved External Intelligence probabilities for V7 PAPER")
    parser.add_argument("--signals-url", default=DEFAULT_SIGNALS)
    parser.add_argument("--report-url", default=DEFAULT_REPORT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=7200)
    parser.add_argument("--min-confidence", type=float, default=0.35)
    args = parser.parse_args()
    now = int(time.time())
    atomic_write(args.output, EMPTY_FEED)
    initializing = {
        "schema": SCHEMA, "timestamp": now, "report_age_seconds": -1,
        "integration_evidence_pass": False, "approved_candidate_id": "",
        "approved_horizon_seconds": 0, "materialized_signals": 0,
        "failures": ["bridge_incomplete"], "paper_only": True,
        "authenticated_execution": False,
    }
    if args.status:
        atomic_write(args.status, json.dumps(initializing, indent=2, sort_keys=True) + "\n")
    try:
        report = json.loads(fetch_text(args.report_url))
        if not isinstance(report, dict):
            raise ValueError("external report is not an object")
        signal_text = fetch_text(args.signals_url)
        output, status = materialize(
            report, signal_text, now=now, max_age_seconds=max(1, args.max_age_seconds),
            min_confidence=max(0.0, min(1.0, args.min_confidence)),
        )
    except Exception as exc:
        status = dict(initializing)
        status["failures"] = [f"bridge_io:{type(exc).__name__}:{exc}"]
        if args.status:
            atomic_write(args.status, json.dumps(status, indent=2, sort_keys=True) + "\n")
        print(json.dumps(status, sort_keys=True))
        return 0
    atomic_write(args.output, output)
    if args.status:
        atomic_write(args.status, json.dumps(status, indent=2, sort_keys=True) + "\n")
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
