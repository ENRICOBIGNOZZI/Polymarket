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


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-v6-paper/1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8")


def finite(value, default=math.nan):
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def approved_direct_models(report: dict, *, allow_unvalidated: bool = False) -> set[tuple[str, str]]:
    candidates = ((report.get("backtest") or {}).get("candidates") or [])
    candidate_passing: list[dict] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("gate_pass") is not True:
            continue
        if str(candidate.get("feature_name") or "") != "external_probability":
            continue
        candidate_passing.append(candidate)

    if allow_unvalidated:
        return {
            (str(candidate.get("source") or ""), "external_probability")
            for candidate in candidate_passing
            if str(candidate.get("source") or "")
        }

    evidence = report.get("alpha_factory_evidence") or {}
    if not isinstance(evidence, dict) or evidence.get("integration_evidence_pass") is not True:
        return set()
    approved_candidate_id = str(evidence.get("candidate_id") or "")
    if not approved_candidate_id:
        return set()

    approved: set[tuple[str, str]] = set()
    for candidate in candidate_passing:
        if str(candidate.get("candidate_id") or "") != approved_candidate_id:
            continue
        source = str(candidate.get("source") or "")
        if source:
            approved.add((source, "external_probability"))
    return approved


def main() -> int:
    ap = argparse.ArgumentParser(description="Materialize only integration-approved external probabilities for V6 paper trading")
    ap.add_argument("--signals-url", default=DEFAULT_SIGNALS)
    ap.add_argument("--report-url", default=DEFAULT_REPORT)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path)
    ap.add_argument("--max-age-seconds", type=int, default=21600)
    ap.add_argument("--min-confidence", type=float, default=0.35)
    ap.add_argument("--allow-unvalidated", action="store_true", help="paper diagnostics only; never use in champion")
    args = ap.parse_args()

    now = int(time.time())
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
            1
            for candidate in ((report.get("backtest") or {}).get("candidates") or [])
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
        text = fetch_text(args.signals_url)
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            q = finite(row.get("q_external"))
            confidence = finite(row.get("confidence"), 0.0)
            source = str(row.get("source") or "")
            feature = str(row.get("feature_name") or "")
            market_id = str(row.get("market_id") or "")
            observed = int(finite(row.get("observed_ts"), 0.0))
            if not market_id or not math.isfinite(q) or not 0.0 < q < 1.0:
                continue
            if confidence < args.min_confidence or observed <= 0 or now - observed > args.max_age_seconds:
                continue
            # Direct terminal probabilities only. Feature rows (returns, volatility,
            # GDELT tone) stay research inputs until a calibrated probability model
            # has explicit integration evidence. A candidate-local gate is never
            # sufficient authorization for the champion feed.
            if feature != "external_probability":
                continue
            if (source, feature) not in approved:
                continue
            current = accepted.get(market_id)
            item = (q, confidence, f"{source}:{feature}", observed)
            if current is None or (confidence, observed) > (current[1], current[3]):
                accepted[market_id] = item
    except Exception as exc:
        failures.append(f"signals:{type(exc).__name__}:{exc}")

    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["market_key", "q_yes", "confidence", "source", "timestamp"])
    for market_id, (q, confidence, source, ts) in sorted(accepted.items()):
        writer.writerow([market_id, f"{q:.12g}", f"{confidence:.12g}", source, ts])
    atomic_write(args.output, output.getvalue())

    status = {
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
