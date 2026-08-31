#!/usr/bin/env python3
"""Create a read-only reconciliation summary from an independent V7 report."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class ReconciliationError(ValueError):
    pass


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_JOURNAL_SOURCES = {"CLOB_USER_WS", "WALLET_RPC", "POLYGON_RPC"}
REQUIRED_EVIDENCE_SOURCES = {"CLOB_USER_WS", "DATA_API_ACTIVITY", "DATA_API_POSITIONS", "WALLET_RPC", "POLYGON_RPC"}


def report_digest(report: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                        allow_nan=False).encode("utf-8")).hexdigest()


def _integrity_breaks(report: dict[str, Any]) -> list[str]:
    breaks: list[str] = []
    if report.get("schema") != "polymarket_v7_real_pnl_independent_verifier_v1":
        breaks.append("independent_verifier_schema_missing")
    supplied_hash = report.get("report_sha256")
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    if not SHA256_RE.fullmatch(str(supplied_hash)) or report_digest(unsigned) != supplied_hash:
        breaks.append("report_hash_invalid")
    if report.get("state") != "REAL_PNL_RECONCILED_UNSIGNED" or report.get("real_pnl_verified") is not False:
        breaks.append("report_not_reconciled_unsigned")
    if report.get("all_entries_live_observed") is not True or not isinstance(report.get("journal_entries"), int) or report["journal_entries"] <= 0:
        breaks.append("live_journal_evidence_missing")
    if not REQUIRED_JOURNAL_SOURCES.issubset(set(report.get("sources_seen") or [])):
        breaks.append("required_journal_sources_missing")
    if not REQUIRED_EVIDENCE_SOURCES.issubset(set(report.get("evidence_sources_seen") or [])):
        breaks.append("required_evidence_sources_missing")
    for key in ("wallet_snapshot_verified", "data_api_position_snapshot_verified", "data_api_activity_coverage_verified"):
        if report.get(key) is not True:
            breaks.append(f"{key}_missing")
    return breaks


def reconcile(report: dict[str, Any], *, run_id: str, exact_code_sha: str,
              period_start: str, period_end: str) -> dict[str, Any]:
    if not isinstance(report, dict) or report.get("model_sha") != exact_code_sha:
        raise ReconciliationError("report_identity_mismatch")
    if not all(isinstance(value, str) and value for value in (run_id, period_start, period_end)):
        raise ReconciliationError("reconciliation_identity_missing")
    integrity = _integrity_breaks(report)
    order = list(report.get("journal_provenance_reference_breaks", []))
    fills = list(report.get("journal_clob_fill_evidence_breaks", []))
    cash = list(report.get("observed_balance_breaks", []))
    positions = list(report.get("open_outcome_positions", []))
    if report.get("data_api_position_reconciliation_break"):
        positions.append("data_api_position_reconciliation_break")
    cash.extend(str(code) for code in report.get("reason_codes", []))
    breaks = integrity + order + fills + cash + positions
    source_hashes = {key: value for key, value in {
        "ledger": report.get("ledger_sha256"), "evidence_tape": report.get("evidence_tape_sha256"),
        "provenance_tape": report.get("provenance_tape_sha256"), "report": report.get("report_sha256"),
    }.items() if isinstance(value, str) and len(value) == 64}
    return {"schema_version": 1, "run_id": run_id, "exact_code_sha": exact_code_sha,
            "period_start": period_start, "period_end": period_end, "report_integrity_breaks": integrity, "order_breaks": order,
            "fill_breaks": fills, "cash_breaks": cash, "position_breaks": positions,
            "unresolved_break_count": len(breaks), "source_hashes": source_hashes,
            "state": "RECONCILED" if not breaks else "MORE_EVIDENCE_REQUIRED"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--exact-code-sha", required=True)
    parser.add_argument("--period-start", required=True)
    parser.add_argument("--period-end", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = reconcile(json.loads(args.report.read_text(encoding="utf-8")), run_id=args.run_id,
                       exact_code_sha=args.exact_code_sha, period_start=args.period_start,
                       period_end=args.period_end)
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output: args.output.write_text(rendered, encoding="utf-8")
    else: print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
