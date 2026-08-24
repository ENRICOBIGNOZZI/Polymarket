#!/usr/bin/env python3
"""Research-only integrity checks for low-frequency external information feeds.

This module does not collect live data or emit trading signals.  It codifies two
fail-closed API contracts that materially affect LF evidence quality:

* Polymarket CLOB absolute price-history windows use startTs/endTs + fidelity,
  without an interval shorthand in the same request.
* Kalshi direct-market discovery excludes multivariate/combo markets so a bounded
  market sample is not saturated by dynamically generated cross-category legs.

The CLI can also classify the latest external-intelligence report and explain
whether missing LF candidates are plausibly data-pipeline-bound rather than
model-bound.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.parse
from pathlib import Path
from typing import Any


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def build_clob_absolute_history_params(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int = 60,
) -> dict[str, Any]:
    """Return a valid absolute-window CLOB price-history query."""
    token_id = str(token_id or "").strip()
    if not token_id:
        raise ValueError("token_id is required")
    start_ts = integer(start_ts)
    end_ts = integer(end_ts)
    fidelity_minutes = integer(fidelity_minutes)
    if start_ts <= 0 or end_ts <= start_ts:
        raise ValueError("end_ts must be greater than start_ts")
    if fidelity_minutes <= 0:
        raise ValueError("positive fidelity is required")
    return {
        "market": token_id,
        "startTs": start_ts,
        "endTs": end_ts,
        "fidelity": fidelity_minutes,
    }


def build_clob_absolute_history_url(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int = 60,
) -> str:
    params = build_clob_absolute_history_params(token_id, start_ts, end_ts, fidelity_minutes)
    return "https://clob.polymarket.com/prices-history?" + urllib.parse.urlencode(params)


def build_kalshi_direct_market_params(
    *,
    status: str = "open",
    limit: int = 1000,
    cursor: str = "",
) -> dict[str, Any]:
    """Return Kalshi market-discovery params restricted to non-MVE contracts."""
    limit = max(1, min(1000, integer(limit, 1000)))
    params: dict[str, Any] = {
        "status": str(status or "open"),
        "limit": limit,
        "mve_filter": "exclude",
    }
    if cursor:
        params["cursor"] = str(cursor)
    return params


def _is_mve_diagnostic(row: dict[str, Any]) -> bool:
    ticker = str(row.get("kalshi_ticker") or "").upper()
    title = str(row.get("kalshi_title") or "")
    return ticker.startswith("KXMVE") or "CROSSCATEGORY" in ticker or title.count(",") >= 2


def analyze_external_report(report: dict[str, Any]) -> dict[str, Any]:
    collection = report.get("collection") if isinstance(report.get("collection"), dict) else {}
    errors = collection.get("source_errors") if isinstance(collection.get("source_errors"), list) else []
    diagnostics = report.get("mapping_diagnostics") if isinstance(report.get("mapping_diagnostics"), list) else []
    backtest = report.get("backtest") if isinstance(report.get("backtest"), dict) else {}

    clob_filter_errors = [
        str(error) for error in errors
        if "prices-history" in str(error) and "HTTP Error 400" in str(error)
    ]
    gdelt_rate_limits = [
        str(error) for error in errors
        if "gdeltproject.org" in str(error) and "HTTP Error 429" in str(error)
    ]
    kalshi_markets = integer(collection.get("kalshi_markets"))
    kalshi_matches = integer(collection.get("kalshi_matches"))
    mve_diagnostics = sum(
        1 for row in diagnostics if isinstance(row, dict) and _is_mve_diagnostic(row)
    )
    diagnostic_count = sum(isinstance(row, dict) for row in diagnostics)
    mve_fraction = mve_diagnostics / diagnostic_count if diagnostic_count else 0.0
    candidate_count = integer(backtest.get("candidate_count"))

    defects: list[str] = []
    recommendations: list[str] = []
    if clob_filter_errors:
        defects.append("clob_absolute_history_filter_conflict")
        recommendations.append("remove interval when startTs/endTs are supplied; retain explicit fidelity")
    if kalshi_markets > 0 and kalshi_matches == 0 and mve_fraction >= 0.50:
        defects.append("kalshi_multivariate_sample_saturation")
        recommendations.append("set mve_filter=exclude before bounded direct-market matching")
    if gdelt_rate_limits:
        defects.append("gdelt_rate_limit_pressure")
        recommendations.append("pace GDELT requests and honor Retry-After before treating missing rows as no-news")

    model_evidence_ready = not defects and candidate_count > 0
    return {
        "schema": "polymarket_lf_external_data_integrity_v1",
        "generated_ts": integer(report.get("generated_ts")),
        "kalshi_markets": kalshi_markets,
        "kalshi_matches": kalshi_matches,
        "mapping_diagnostics": diagnostic_count,
        "mve_diagnostic_fraction": mve_fraction,
        "clob_history_400_errors": len(clob_filter_errors),
        "gdelt_429_errors": len(gdelt_rate_limits),
        "candidate_count": candidate_count,
        "defects": defects,
        "recommendations": recommendations,
        "model_evidence_ready": model_evidence_ready,
        "decision": "MORE_EVIDENCE_REQUIRED" if defects or candidate_count == 0 else "EVIDENCE_AVAILABLE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose LF external-information data integrity")
    parser.add_argument("--report", required=True, help="latest external-intelligence JSON report")
    parser.add_argument("--output")
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("report root must be an object")
    result = analyze_external_report(report)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
