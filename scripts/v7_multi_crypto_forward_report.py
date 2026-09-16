#!/usr/bin/env python3
"""Prospective report for one frozen multi-crypto PAPER-forward cohort.

The denominator begins at durable CAPITAL_RESERVE. Missing FINAL remains pending.
The report never annualizes short samples and never promotes execution.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics
from typing import Any, Mapping

from v7_execution_ledger import LedgerEvent, iter_events
from v7_lead_lag_replay import ReplayError, digest
from v7_lead_lag_research import cluster_mean_ci, connected_clusters

SCHEMA = "polymarket_v7_multi_crypto_forward_report_v1"
MANIFEST_SCHEMA = "polymarket_v7_multi_crypto_forward_protocol_v1"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def manifest_identity(value: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if (not isinstance(value, Mapping) or value.get("schema") != MANIFEST_SCHEMA
            or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False
            or value.get("real_order_submission") is not False
            or value.get("real_capital_at_risk") is not False
            or value.get("automatic_promotion") is not False
            or value.get("entry_authority") is not False):
        raise ReplayError("MULTI_FORWARD_REPORT_MANIFEST_INVALID")
    frozen = value.get("frozen_protocol")
    packet = value.get("multi_crypto_forward")
    if not isinstance(frozen, dict) or not isinstance(packet, dict):
        raise ReplayError("MULTI_FORWARD_REPORT_MANIFEST_CONTENT_MISSING")
    protocol_hash = str(value.get("protocol_hash") or "")
    if protocol_hash != digest(frozen) or packet.get("protocol_hash") != protocol_hash:
        raise ReplayError("MULTI_FORWARD_REPORT_PROTOCOL_HASH_MISMATCH")
    if (packet.get("experiment_id") != frozen.get("experiment_id")
            or packet.get("asset") != frozen.get("asset")
            or packet.get("horizon") != frozen.get("horizon")):
        raise ReplayError("MULTI_FORWARD_REPORT_PACKET_SCOPE_MISMATCH")
    code_sha = str(frozen.get("code_sha") or "")
    if len(code_sha) != 40 or any(ch not in "0123456789abcdef" for ch in code_sha):
        raise ReplayError("MULTI_FORWARD_REPORT_CODE_SHA_INVALID")
    return code_sha, dict(frozen), dict(packet)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values); pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def summarize(events: list[LedgerEvent], manifest: Mapping[str, Any], *,
              bootstrap_draws: int | None = None) -> dict[str, Any]:
    code_sha, frozen, packet = manifest_identity(manifest)
    if any(event.model_sha != code_sha for event in events):
        raise ReplayError("MULTI_FORWARD_REPORT_MIXED_SHA_INPUT")
    experiment_id = str(packet["experiment_id"])
    cohort: list[LedgerEvent] = []
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        candidate_packet = metadata.get("multi_crypto_forward")
        receipt = metadata.get("coordinator_receipt") if isinstance(metadata.get("coordinator_receipt"), dict) else {}
        receipt_packet = receipt.get("multi_crypto_forward")
        if isinstance(candidate_packet, dict) and candidate_packet.get("experiment_id") == experiment_id:
            if candidate_packet != packet or receipt_packet != packet:
                raise ReplayError("MULTI_FORWARD_REPORT_LINEAGE_CONFLICT")
            if (receipt.get("owner") != "V7_GLOBAL_PORTFOLIO_COORDINATOR"
                    or receipt.get("paper_multi_crypto_forward_authorized") is not True
                    or receipt.get("new_risk_authorized") is not False
                    or receipt.get("real_order_submission") is not False):
                raise ReplayError("MULTI_FORWARD_REPORT_RECEIPT_INVALID")
            cohort.append(event)

    by_market: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in cohort:
        if not event.market_id:
            raise ReplayError("MULTI_FORWARD_REPORT_MARKET_MISSING")
        by_market[str(event.market_id)].append(event)
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for market_id, market_events in sorted(by_market.items(), key=lambda item: min(e.recorded_ts_ms for e in item[1])):
        reserves = [e for e in market_events if e.event_type == "CAPITAL_RESERVE"]
        submitted = [e for e in market_events if e.event_type == "ORDER_SUBMITTED"]
        fills = [e for e in market_events if e.event_type == "FILL"]
        finals = [e for e in market_events if e.event_type == "FINAL"]
        if len(reserves) != 1:
            raise ReplayError("MULTI_FORWARD_REPORT_RESERVATION_COUNT_INVALID:" + market_id)
        order_ids = {str(e.order_id) for e in submitted + fills if e.order_id}
        if len(order_ids) > 1:
            raise ReplayError("MULTI_FORWARD_REPORT_MULTIPLE_ENTRIES_PER_MARKET:" + market_id)
        if len(finals) > 1:
            raise ReplayError("MULTI_FORWARD_REPORT_DUPLICATE_FINAL:" + market_id)
        reserve = reserves[0]
        projection = (reserve.metadata or {}).get("reservation_projection")
        request = projection.get("request") if isinstance(projection, dict) and isinstance(projection.get("request"), dict) else {}
        parent_shock = str(request.get("parent_shock_id") or "")
        reservation_id = str(projection.get("reservation_id") or "") if isinstance(projection, dict) else ""
        if not parent_shock or not reservation_id:
            raise ReplayError("MULTI_FORWARD_REPORT_RESERVATION_PROVENANCE_MISSING")
        filled_size = sum(float(e.filled_size or 0.0) for e in fills)
        pnl = float(finals[0].final_pnl) if finals else None
        status = "RESOLVED" if finals else "FILLED_PENDING_SETTLEMENT" if fills else "AUTHORIZED_NOT_FILLED"
        reasons[status] += 1
        rows.append({
            "market_id": market_id, "asset": packet["asset"], "horizon": packet["horizon"],
            "reservation_id": reservation_id, "parent_shock_id": parent_shock,
            "authorized_ms": int(reserve.recorded_ts_ms), "submitted": bool(submitted),
            "fill_records": len(fills), "filled_size": filled_size,
            "resolved": bool(finals), "pnl": pnl, "status": status,
        })

    pnl_values = [float(row["pnl"]) for row in rows if row["pnl"] is not None]
    all_terminal = len(pnl_values) == len(rows)
    statistical = frozen.get("statistical_protocol") if isinstance(frozen.get("statistical_protocol"), dict) else {}
    minimum_clusters = int(statistical.get("minimum_clusters") or 2)
    draws = int(bootstrap_draws or statistical.get("bootstrap_draws") or 2000)
    block_ns = int(statistical.get("bootstrap_block_ns") or 60_000_000_000)
    clusters = connected_clusters([
        {"decision_ns": row["authorized_ms"] * 1_000_000,
         "market_id": row["market_id"], "parent_shock_id": row["parent_shock_id"]}
        for row in rows
    ], block_ns=block_ns) if rows else ()
    pnl_ci = cluster_mean_ci(
        [float(row["pnl"]) if row["pnl"] is not None else None for row in rows],
        clusters, draws=draws, seed=int(statistical.get("bootstrap_seed") or 17),
        minimum_clusters=minimum_clusters,
    ) if rows else None
    gross_positive = sum(max(0.0, value) for value in pnl_values)
    positives = sorted((max(0.0, value) for value in pnl_values), reverse=True)
    largest_fraction = positives[0] / gross_positive if gross_positive > 0 and positives else None
    terminal_sorted = sorted((row for row in rows if row["pnl"] is not None), key=lambda row: (row["authorized_ms"], row["market_id"]))
    blocks = []
    for start in range(0, len(terminal_sorted), 25):
        block = terminal_sorted[start:start + 25]
        blocks.append({"block": start // 25 + 1, "markets": len(block), "complete_25": len(block) == 25,
                       "pnl": sum(float(row["pnl"]) for row in block)})
    target = int(statistical.get("target_independent_markets") or 0)
    return {
        "schema": SCHEMA, "experiment_id": experiment_id, "protocol_hash": packet["protocol_hash"],
        "code_sha": code_sha, "asset": packet["asset"], "horizon": packet["horizon"],
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "automatic_promotion": False, "entry_authority": False,
        "status": "COMPLETE_ACCOUNTING" if rows and all_terminal else "IN_PROGRESS",
        "authorized_markets": len(rows), "submitted_markets": sum(row["submitted"] for row in rows),
        "filled_markets": sum(row["filled_size"] > 0 for row in rows),
        "resolved_markets": len(pnl_values), "pending_markets": len(rows) - len(pnl_values),
        "state_counts": dict(sorted(reasons.items())),
        "total_pnl": sum(pnl_values) if all_terminal else None,
        "resolved_pnl_contribution": sum(pnl_values),
        "mean_pnl_per_authorized_market": statistics.fmean(pnl_values) if all_terminal and pnl_values else None,
        "median_resolved_pnl": statistics.median(pnl_values) if pnl_values else None,
        "cluster_pnl_ci": pnl_ci,
        "largest_positive_market_fraction_of_gross_positive_pnl": largest_fraction,
        "pnl_without_best_resolved_market": sum(pnl_values) - max(pnl_values) if pnl_values else None,
        "pnl_without_best_three_resolved_markets": sum(pnl_values) - sum(sorted(pnl_values, reverse=True)[:3]) if pnl_values else None,
        "chronological_25_market_blocks": blocks,
        "target_independent_markets": target,
        "minimum_market_target_met": len(rows) >= target if target > 0 else False,
        "economic_evidence": "PENDING" if not all_terminal else "OBSERVED_PAPER_ACCOUNTING_NOT_REAL_EXECUTION",
        "annualized_sharpe": None,
        "limitations": [
            "PAPER fills are simulated and do not validate real matching-engine execution.",
            "Pending markets are not assigned zero PnL.",
            "Authorized markets are the denominator available from the canonical reservation ledger; pre-authorization signal funnel requires its separate observer.",
            "No automatic promotion follows from this report.",
        ],
        "market_rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or "ledger" in args.output.parts:
        parser.exit(2, "refusing overwrite or canonical ledger destination\n")
    try:
        manifest = load(args.manifest); code_sha, _, _ = manifest_identity(manifest)
        report = summarize(list(iter_events(args.ledger, expected_model_sha=code_sha)), manifest)
        report["report_hash"] = digest(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False); handle.write("\n")
        print(json.dumps({"status": report["status"], "authorized": report["authorized_markets"],
                          "resolved": report["resolved_markets"], "economic_evidence": report["economic_evidence"],
                          "report_hash": report["report_hash"]}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"multi-crypto forward report failed closed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
