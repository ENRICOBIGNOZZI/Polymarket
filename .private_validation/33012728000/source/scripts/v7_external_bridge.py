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

DEFAULT_SIGNALS = "https://raw.githubusercontent.com/ENRICOBIGNOZZI/Polymarket/telemetry/telemetry/latest-external-signals.jsonl"
DEFAULT_REPORT = "https://raw.githubusercontent.com/ENRICOBIGNOZZI/Polymarket/telemetry/telemetry/latest-external-intelligence.json"
EMPTY_FEED = "market_key,q_yes,confidence,source,timestamp\n"


def fetch_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "polymarket-v7-paper/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def finite(value, default=math.nan):
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def approved_direct_models(report: dict, *, allow_unvalidated: bool = False) -> set[tuple[str, str]]:
    candidates = ((report.get("backtest") or {}).get("candidates") or [])
    passing = [
        candidate for candidate in candidates
        if isinstance(candidate, dict)
        and candidate.get("gate_pass") is True
        and str(candidate.get("feature_name") or "") == "external_probability"
    ]
    if allow_unvalidated:
        return {(str(candidate.get("source") or ""), "external_probability") for candidate in passing if candidate.get("source")}
    evidence = report.get("alpha_factory_evidence") or {}
    if not isinstance(evidence, dict) or evidence.get("integration_evidence_pass") is not True:
        return set()
    approved_candidate_id = str(evidence.get("candidate_id") or "")
    if not approved_candidate_id:
        return set()
    return {
        (str(candidate.get("source") or ""), "external_probability")
        for candidate in passing
        if str(candidate.get("candidate_id") or "") == approved_candidate_id and str(candidate.get("source") or "")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize only integration-approved external probabilities for V7 PAPER")
    parser.add_argument("--signals-url", default=DEFAULT_SIGNALS)
    parser.add_argument("--report-url", default=DEFAULT_REPORT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=21600)
    parser.add_argument("--min-confidence", type=float, default=0.35)
    parser.add_argument("--allow-unvalidated", action="store_true", help="PAPER diagnostics only; never use in champion")
    args = parser.parse_args()
    now = int(time.time())

    atomic_write(args.output, EMPTY_FEED)
    if args.status:
        atomic_write(args.status, json.dumps({
            "schema": "polymarket_v7_external_bridge_status_v1",
            "timestamp": now,
            "report_status": "initializing",
            "candidate_passing_direct_models": 0,
            "integration_evidence_pass": False,
            "approved_candidate_id": "",
            "approved_direct_models": 0,
            "passing_direct_models": 0,
            "materialized_signals": 0,
            "failures": ["bridge_incomplete"],
            "paper_only": True,
        }, indent=2, sort_keys=True) + "\n")

    failures: list[str] = []
    accepted: dict[str, tuple[float, float, str, int]] = {}
    approved: set[tuple[str, str]] = set()
    candidate_passing_direct_models = 0
    integration_evidence_pass = False
    approved_candidate_id = ""
    report_status = "missing"

    try:
        report = json.loads(fetch_text(args.report_url))
        report_status = str(report.get("status") or "unknown")
        candidate_passing_direct_models = sum(
            1 for candidate in ((report.get("backtest") or {}).get("candidates") or [])
            if isinstance(candidate, dict)
            and candidate.get("gate_pass") is True
            and str(candidate.get("feature_name") or "") == "external_probability"
        )
        evidence = report.get("alpha_factory_evidence") or {}
        if isinstance(evidence, dict):
            integration_evidence_pass = evidence.get("integration_evidence_pass") is True
            approved_candidate_id = str(evidence.get("candidate_id") or "")
        approved = approved_direct_models(report, allow_unvalidated=args.allow_unvalidated)
    except Exception as exc:
        failures.append(f"report:{type(exc).__name__}:{exc}")

    try:
        for raw in fetch_text(args.signals_url).splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            probability = finite(row.get("q_external"))
            confidence = finite(row.get("confidence"), 0.0)
            source = str(row.get("source") or "")
            feature = str(row.get("feature_name") or "")
            market_id = str(row.get("market_id") or "")
            observed = int(finite(row.get("observed_ts"), 0.0))
            if not market_id or not math.isfinite(probability) or not 0.0 < probability < 1.0:
                continue
            if confidence < args.min_confidence or observed <= 0 or now - observed > args.max_age_seconds:
                continue
            if feature != "external_probability" or (source, feature) not in approved:
                continue
            current = accepted.get(market_id)
            item = (probability, confidence, f"{source}:{feature}", observed)
            if current is None or (confidence, observed) > (current[1], current[3]):
                accepted[market_id] = item
    except Exception as exc:
        failures.append(f"signals:{type(exc).__name__}:{exc}")

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["market_key", "q_yes", "confidence", "source", "timestamp"])
    for market_id, (probability, confidence, source, timestamp) in sorted(accepted.items()):
        writer.writerow([market_id, f"{probability:.12g}", f"{confidence:.12g}", source, timestamp])
    atomic_write(args.output, output.getvalue())

    status = {
        "schema": "polymarket_v7_external_bridge_status_v1",
        "timestamp": now,
        "report_status": report_status,
        "candidate_passing_direct_models": candidate_passing_direct_models,
        "integration_evidence_pass": integration_evidence_pass,
        "approved_candidate_id": approved_candidate_id,
        "approved_direct_models": len(approved),
        "passing_direct_models": len(approved),
        "materialized_signals": len(accepted),
        "failures": failures,
        "paper_only": True,
    }
    if args.status:
        atomic_write(args.status, json.dumps(status, indent=2, sort_keys=True) + "\n")
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
