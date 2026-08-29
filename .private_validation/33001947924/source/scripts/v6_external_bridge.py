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


def main() -> int:
    ap = argparse.ArgumentParser(description="Materialize only OOS-approved external probabilities for V6 paper trading")
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
    passing: set[tuple[str, str]] = set()
    report_status = "missing"

    try:
        report = json.loads(fetch_text(args.report_url))
        report_status = str(report.get("status") or "unknown")
        for candidate in ((report.get("backtest") or {}).get("candidates") or []):
            if candidate.get("gate_pass"):
                passing.add((str(candidate.get("source") or ""), str(candidate.get("feature_name") or "")))
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
            # GDELT tone) stay research inputs until their own calibrated forecast is promoted.
            if feature != "external_probability":
                continue
            if not args.allow_unvalidated and (source, feature) not in passing:
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
        "passing_direct_models": len(passing),
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
