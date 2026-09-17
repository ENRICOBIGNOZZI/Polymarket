#!/usr/bin/env python3
"""Read-only audit of canonical V7 lead-lag long/hold accounting.

This never edits history or grants execution authority. Missing authoritative
resolution evidence is UNRESOLVED, never an invented zero payout.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from v7_execution_ledger import EconomicJournalEntry, LedgerEvent, iter_records
from v7_lead_lag_replay import ZERO, ReplayError, ResolutionProof, decimal, digest


def is_lead_lag(event: LedgerEvent) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return metadata.get("model_family") == "lead_lag_taker_v1" \
        or metadata.get("component") == "crypto_informed_taker"


def parse_proof(raw: Mapping[str, Any]) -> ResolutionProof:
    value = dict(raw)
    value["token_payouts"] = tuple((token, decimal(payout)) for token, payout in value["token_payouts"])
    return ResolutionProof(**value)

def terminal_recovery_flag(event: LedgerEvent) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    if metadata.get("forced_flat") is True or metadata.get("zero_recovery") is True:
        return True
    text = " ".join(str(metadata.get(key) or "") for key in (
        "terminal_reason", "recovery_reason", "settlement_source", "close_reason"
    )).upper()
    return any(token in text for token in (
        "ZERO_RECOVERY", "FORCED_FLAT", "CONSERVATIVE_TERMINAL", "CUTOVER_ZERO", "DEPLOY_ZERO"
    ))


def fill_accounting(fill: LedgerEvent) -> tuple[Decimal, Decimal, str, list[str]]:
    flags: list[str] = []
    metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
    exact = metadata.get("exact_paper_fill")
    if isinstance(exact, dict):
        required = {"gross_shares", "net_shares", "cash_debit", "cash_fee", "shares_fee"}
        if set(exact) != required:
            raise ReplayError("EXACT_FILL_ACCOUNTING_SHAPE")
        gross, net, debit, cash_fee, shares_fee = (
            decimal(exact[key]) for key in ("gross_shares", "net_shares", "cash_debit", "cash_fee", "shares_fee")
        )
        if net != gross - shares_fee or min(gross, net, debit, cash_fee, shares_fee) < ZERO:
            flags.append("FILL_CASH_SHARE_FEE_MISMATCH")
        if cash_fee > ZERO and shares_fee > ZERO:
            flags.append("FEE_DOUBLE_INCIDENCE")
        return net, debit, "DECIMAL_EXACT_METADATA", flags

    quantity = decimal(fill.filled_size)
    debit = quantity * decimal(fill.fill_price) + decimal(fill.fee)
    return quantity, debit, "RECORDED_FLOAT_COLUMNS_CASH_FEE_INTERPRETATION", flags

def audit_records(records: Iterable[LedgerEvent | EconomicJournalEntry], *,
                  resolution_evidence: Mapping[str, ResolutionProof] | None = None) -> dict[str, Any]:
    proofs = resolution_evidence or {}
    seen: dict[str, str] = {}
    events: list[LedgerEvent] = []
    journal_count = 0
    duplicate_identical = 0
    for record in records:
        if isinstance(record, EconomicJournalEntry):
            record.validate()
            journal_count += 1
            continue
        record.validate()
        encoded = digest(record.to_dict())
        prior = seen.get(record.record_id)
        if prior is not None:
            if prior != encoded:
                raise ReplayError("CANONICAL_RECORD_ID_CONFLICT:" + record.record_id)
            duplicate_identical += 1
            continue
        seen[record.record_id] = encoded
        events.append(record)

    lead_positions = {event.position_id for event in events if is_lead_lag(event) and event.position_id}
    grouped: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in events:
        if event.position_id in lead_positions and event.event_type in {"FILL", "FINAL"}:
            grouped[str(event.position_id)].append(event)

    positions: list[dict[str, Any]] = []
    flag_counts: Counter[str] = Counter()
    supported_total = ZERO
    claimed_total = ZERO
    for position_id, rows in sorted(grouped.items()):
        fills = [row for row in rows if row.event_type == "FILL"]
        finals = [row for row in rows if row.event_type == "FINAL"]
        flags: list[str] = []
        if len(fills) != 1:
            flags.append("POSITION_FILL_COUNT_INVALID")
        if len(finals) > 1:
            flags.append("MULTIPLE_FINALS_REQUIRE_EXPLICIT_CORRECTION")
        fill = fills[0] if len(fills) == 1 else None
        final = finals[0] if len(finals) == 1 else None
        net_shares, recorded_cost, precision = ZERO, ZERO, "UNKNOWN"
        market_id = (fill or final).market_id if (fill or final) else None
        token_id = (fill or final).token_id if (fill or final) else None
        if fill is not None:
            if fill.side != "BUY":
                flags.append("UNSUPPORTED_NON_LONG_HOLD_POSITION")
            net_shares, recorded_cost, precision, fill_flags = fill_accounting(fill)
            flags.extend(fill_flags)
            receipt = (fill.metadata or {}).get("coordinator_receipt")
            if not isinstance(receipt, dict) or not receipt.get("selected_replay_key"):
                flags.append("FILL_COORDINATOR_RECEIPT_MISSING")
            if fill.decision_ts_ms is not None and fill.recorded_ts_ms == fill.decision_ts_ms + 1:
                flags.append("FILL_TIMING_IS_SYNTHETIC_ORDERING_NOT_LATENCY")

        claimed_pnl = decimal(final.final_pnl) if final is not None else None
        if claimed_pnl is not None:
            claimed_total += claimed_pnl
        if final is not None:
            receipt = (final.metadata or {}).get("coordinator_receipt")
            if not isinstance(receipt, dict) or not receipt.get("selected_replay_key"):
                flags.append("FINAL_COORDINATOR_RECEIPT_MISSING")
            if terminal_recovery_flag(final):
                flags.append("CONSERVATIVE_TERMINAL_REQUIRES_OFFICIAL_RECONCILIATION")
        proof = proofs.get(str(market_id))
        if proof is None and final is not None and isinstance((final.metadata or {}).get("resolution_proof"), dict):
            proof = parse_proof(final.metadata["resolution_proof"])
        supported_pnl: Decimal | None = None
        payout: Decimal | None = None
        if proof is None or proof.status != "RESOLVED":
            flags.append("RESOLUTION_UNRESOLVED_OR_PROOF_MISSING")
        elif proof.market_id != market_id or str(token_id) not in dict(proof.token_payouts):
            flags.append("RESOLUTION_BINDING_MISMATCH")
        elif fill is not None and proof.available_wall_ns < fill.recorded_ts_ms * 1_000_000:
            flags.append("RESOLUTION_PRECEDES_ENTRY")
        elif fill is not None:
            payout = net_shares * dict(proof.token_payouts)[str(token_id)]
            candidate = payout - recorded_cost
            if final is not None:
                if final.market_id != market_id or final.token_id != token_id:
                    flags.append("FINAL_INSTRUMENT_MISMATCH")
                if abs(decimal(final.realized_cashflow) - payout) > Decimal("0.0000001"):
                    flags.append("RECORDED_PAYOUT_REQUIRES_CORRECTION")
                if claimed_pnl is not None and abs(claimed_pnl - candidate) > Decimal("0.0000001"):
                    flags.append("RECORDED_PNL_REQUIRES_CORRECTION")
            hard_errors = {
                "POSITION_FILL_COUNT_INVALID", "MULTIPLE_FINALS_REQUIRE_EXPLICIT_CORRECTION",
                "FILL_CASH_SHARE_FEE_MISMATCH", "FEE_DOUBLE_INCIDENCE",
                "UNSUPPORTED_NON_LONG_HOLD_POSITION", "FINAL_INSTRUMENT_MISMATCH",
            }
            if not (set(flags) & hard_errors):
                supported_pnl = candidate
                supported_total += candidate

        correction = None
        correction_flags = {"RECORDED_PAYOUT_REQUIRES_CORRECTION", "RECORDED_PNL_REQUIRES_CORRECTION",
                            "CONSERVATIVE_TERMINAL_REQUIRES_OFFICIAL_RECONCILIATION"}
        if final is not None and supported_pnl is not None and set(flags) & correction_flags:
            correction = {
                "correction_id": digest((final.record_id, proof.source_record_hash, str(supported_pnl))),
                "original_record_id": final.record_id,
                "original_record_hash": digest(final.to_dict()),
                "resolution_source_hash": proof.source_record_hash,
                "corrected_payout": str(payout),
                "corrected_pnl": str(supported_pnl),
                "status": "PROPOSED_APPEND_ONLY_NOT_APPLIED",
            }
        for flag in set(flags):
            flag_counts[flag] += 1
        positions.append({
            "position_id": position_id,
            "market_id": market_id,
            "token_id": token_id,
            "fill_count": len(fills),
            "final_count": len(finals),
            "net_shares": str(net_shares),
            "recorded_cost": str(recorded_cost),
            "precision": precision,
            "claimed_pnl": None if claimed_pnl is None else str(claimed_pnl),
            "supported_settlement_pnl": None if supported_pnl is None else str(supported_pnl),
            "state": "SUPPORTED_ACCOUNTING" if supported_pnl is not None else "UNRESOLVED",
            "flags": sorted(set(flags)),
            "correction_proposal": correction,
        })

    unresolved = sum(row["state"] == "UNRESOLVED" for row in positions)
    blocking = {key: value for key, value in flag_counts.items()
                if key != "FILL_TIMING_IS_SYNTHETIC_ORDERING_NOT_LATENCY"}
    return {
        "schema": "polymarket_v7_lead_lag_accounting_audit_v1",
        "scope": "LEAD_LAG_LONG_HOLD_POSITIONS_ONLY",
        "whole_portfolio_reconciled": False,
        "paper_only": True,
        "entry_authority": False,
        "real_order_submission": False,
        "history_modified": False,
        "corrections_applied": 0,
        "execution_record_count": len(events),
        "economic_journal_record_count": journal_count,
        "duplicate_identical_record_count": duplicate_identical,
        "lead_lag_position_count": len(positions),
        "unresolved_position_count": unresolved,
        "claimed_pnl_sum_not_certified": str(claimed_total),
        "supported_pnl_contribution": str(supported_total),
        "supported_complete_scope_pnl": str(supported_total) if unresolved == 0 and not blocking else None,
        "code_shas": sorted({event.model_sha for event in events}),
        "flag_counts": dict(sorted(flag_counts.items())),
        "positions": positions,
        "status": "REQUIRES_REVIEW" if unresolved or blocking else "SCOPED_AUDIT_COMPLETE_NO_PORTFOLIO_AUTHORITY",
    }


def load_resolution_evidence(path: Path | None) -> dict[str, ResolutionProof]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ReplayError("RESOLUTION_EVIDENCE_LIST_REQUIRED")
    output: dict[str, ResolutionProof] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "source", "market_id", "expected_tokens", "available_wall_ns", "raw_response", "response_hash"
        }:
            raise ReplayError("RESOLUTION_EVIDENCE_SHAPE_INVALID")
        if item["source"] != "OFFICIAL_GAMMA_RESOLUTION" or digest(item["raw_response"]) != item["response_hash"]:
            raise ReplayError("RESOLUTION_SOURCE_OR_BODY_HASH_INVALID")
        proof = ResolutionProof.from_gamma(
            item["raw_response"],
            expected_market=item["market_id"],
            expected_tokens=tuple(item["expected_tokens"]),
            available_wall_ns=item["available_wall_ns"],
        )
        if proof.market_id in output:
            raise ReplayError("DUPLICATE_RESOLUTION_REQUIRES_EXPLICIT_SELECTION")
        output[proof.market_id] = proof
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-snapshot", type=Path, required=True)
    parser.add_argument("--resolution-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.ledger_snapshot.resolve() or "ledger" in args.output.parts:
        parser.exit(2, "refusing source overwrite or canonical ledger destination\n")
    try:
        before = args.ledger_snapshot.stat()
        raw = args.ledger_snapshot.read_bytes()
        report = audit_records(iter_records(args.ledger_snapshot),
                               resolution_evidence=load_resolution_evidence(args.resolution_evidence))
        after = args.ledger_snapshot.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ReplayError("LEDGER_CHANGED_DURING_AUDIT_USE_IMMUTABLE_SNAPSHOT")
        report["ledger_snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
        report["ledger_snapshot_bytes"] = len(raw)
        report["report_hash"] = digest(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
        print(json.dumps({
            "status": report["status"],
            "positions": report["lead_lag_position_count"],
            "unresolved": report["unresolved_position_count"],
            "flag_counts": report["flag_counts"],
        }, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(2, f"accounting audit failed closed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
