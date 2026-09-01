#!/usr/bin/env python3
"""Single runtime consumer for both V7 economic-engine opportunity cuts.

Checked-in operation is PAPER observation only: this coordinator has no flag
that can authorize new risk. It validates fully typed envelopes, compares both
engines on conservative expected account-wealth change, gives CANCEL priority,
and emits a deterministic fail-closed decision receipt.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from v7_opportunity import OpportunityEnvelope, OpportunityError, coordinate, fail_closed_decision


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


COMPATIBILITY_COMPONENTS = {
    "CRYPTO_SETTLEMENT_FAIR": ("BTC_SETTLEMENT_ENGINE", "crypto_settlement_fair"),
    "CRYPTO_INFORMED_TAKER": ("BTC_SETTLEMENT_ENGINE", "crypto_informed_taker"),
    "MICRO_MAKER_PRO": ("BTC_SETTLEMENT_ENGINE", "professional_maker"),
    "PROFESSIONAL_MAKER": ("BTC_SETTLEMENT_ENGINE", "professional_maker"),
    "FAST_STRUCTURAL": ("STRUCTURAL_ARB_ENGINE", "fast_structural"),
    "HARD_ARB": ("STRUCTURAL_ARB_ENGINE", "hard_arb"),
}


def _compatibility_envelope(value: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    strategy = str(value.get("strategy") or "").upper()
    ownership = COMPATIBILITY_COMPONENTS.get(strategy)
    if ownership is None:
        raise OpportunityError("compatibility_strategy_unowned")
    engine_id, component = ownership
    ingress = value.get("ingress") if isinstance(value.get("ingress"), dict) else {}
    if ingress.get("engine_id") != engine_id:
        raise OpportunityError("compatibility_engine_mismatch")
    if (
        context.get("schema") != "polymarket_v7_runtime_status_v3"
        or context.get("model_sha") != value.get("model_sha")
        or not isinstance(context.get("config_hash"), str)
        or not isinstance(context.get("policy_hash"), str)
        or not isinstance(context.get("run_id"), str)
    ):
        raise OpportunityError("compatibility_runtime_identity_missing")
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    recorded_ms = int(value.get("recorded_ts_ms") or 0)
    decision_ms = int(value.get("decision_ts_ms") or recorded_ms)
    receive_ms = int(value.get("receive_ts_ms") or decision_ms)
    exchange_ms = int(value.get("exchange_ts_ms") or receive_ms)
    if min(recorded_ms, decision_ms, receive_ms, exchange_ms) <= 0:
        raise OpportunityError("compatibility_causal_clock_missing")
    decision_ns = max(recorded_ms, decision_ms, receive_ms, exchange_ms) * 1_000_000
    source_ns = sorted({exchange_ms * 1_000_000, receive_ms * 1_000_000})
    identity = str(
        value.get("candidate_id") or value.get("opportunity_id")
        or value.get("record_id") or ""
    )
    if not identity:
        raise OpportunityError("compatibility_identity_missing")
    expected = float(value.get("expected_ev") or 0.0)
    structured = metadata.get("structured_legs")
    raw_legs = structured if isinstance(structured, list) else []
    legs: list[dict[str, Any]] = []
    for index, leg in enumerate(raw_legs):
        if not isinstance(leg, dict):
            continue
        quantity = float(leg.get("target_quantity") or value.get("intended_size") or 0.0)
        price = float(leg.get("detector_average_price") or value.get("limit_price") or 0.0)
        if quantity <= 0.0 or not 0.0 <= price <= 1.0:
            continue
        legs.append({
            "leg_id": str(leg.get("leg_id") or f"leg-{index + 1}"),
            "market_id": str(leg.get("market_id") or value.get("market_id") or f"unmapped:{identity}"),
            "contract_id": str(leg.get("token_id") or value.get("token_id") or identity),
            "token_id": str(leg.get("token_id") or value.get("token_id") or identity),
            "side": str(leg.get("side") or "BUY").upper(),
            "target_quantity": quantity,
            "limit_price": price,
            "fee_authority": "CONSERVATIVE_ZERO",
        })
    if not legs:
        quantity = max(1e-12, float(value.get("intended_size") or 1e-12))
        price = min(1.0, max(0.0, float(value.get("limit_price") or 0.0)))
        legs = [{
            "leg_id": str(value.get("leg_id") or "leg-1"),
            "market_id": str(value.get("market_id") or f"unmapped:{identity}"),
            "contract_id": str(value.get("token_id") or value.get("market_id") or identity),
            "token_id": str(value.get("token_id") or identity),
            "side": "BUY", "target_quantity": quantity, "limit_price": price,
            "fee_authority": "CONSERVATIVE_ZERO",
        }]
    raw = {
        "schema": "polymarket_v7_opportunity_envelope_v1",
        "version": 1,
        "model_sha": value["model_sha"],
        "config_hash": context["config_hash"],
        "policy_hash": context["policy_hash"],
        "run_id": context["run_id"],
        "source_snapshot_identity": str(value.get("book_snapshot_id") or identity),
        "engine_id": engine_id,
        "component_provenance": [component],
        "market_id": str(value.get("market_id") or f"unmapped:{identity}"),
        "event_id": str(value.get("event_id") or f"unmapped:{identity}"),
        "contract_id": str(value.get("token_id") or value.get("market_id") or identity),
        "mapping_identity": str(metadata.get("contract_rules_hash") or f"unverified:{identity}"),
        # The adapter cannot manufacture missing evidence. It preserves the
        # candidate's economics while forcing its actionable surface to NOTHING.
        "action": "NOTHING",
        "side": "NONE",
        "decision_receive_timestamp_ns": decision_ns,
        "source_event_timestamps_ns": source_ns,
        "fair_value": {"lower": 0.0, "point": 0.5, "upper": 1.0},
        "conservative_expected_wealth_change": expected,
        "cost_vector": {
            "fee": max(0.0, float(value.get("fee") or 0.0)),
            "slippage": max(0.0, float(value.get("slippage") or 0.0)),
            "unwind_loss": max(0.0, float(value.get("unwind_loss") or 0.0)),
            "capital_cost": max(0.0, float(value.get("capital_cost") or 0.0)),
            "latency_cost": max(0.0, float(value.get("latency_cost") or 0.0)),
            "adverse_markout": 0.0,
            "rebate": 0.0,
        },
        "cost_authority": {
            "fee": "CONSERVATIVE_ZERO", "slippage": "CONSERVATIVE_ZERO",
            "unwind_loss": "CONSERVATIVE_ZERO", "capital_cost": "CONSERVATIVE_ZERO",
            "latency_cost": "CONSERVATIVE_ZERO", "adverse_markout": "CONSERVATIVE_ZERO",
            "rebate": "CONSERVATIVE_ZERO",
        },
        "uncertainty": {"lower_bound": -1.0, "upper_bound": 1.0, "status": "MISSING"},
        "calibration_status": "MISSING",
        "latency": {
            "profile_id": "compatibility-missing", "profile_valid": False,
            "economic_percentile": "p99", "arrival_ns": 0,
        },
        "capacity": {
            "executable_size": max(0.0, float(value.get("intended_size") or 0.0)),
            "depth_provenance": str(value.get("book_snapshot_id") or "MISSING"),
        },
        "execution_plan": {
            "atomic_unit_id": str(value.get("bundle_id") or identity),
            "execution_style": (
                "SEQUENTIAL_ATOMIC_INTENT" if engine_id == "STRUCTURAL_ARB_ENGINE"
                else "SINGLE_LEG"
            ),
            "legs": legs,
            "partial_fill_plan": (
                "COMPLETE_OR_UNWIND" if engine_id == "STRUCTURAL_ARB_ENGINE"
                else "NO_NEW_RISK"
            ),
            "timeout_ms": int(value.get("timeout_ms") or 0),
            "unwind_plan": (
                "FULL_DEPTH_BOUNDED_UNWIND" if engine_id == "STRUCTURAL_ARB_ENGINE"
                else "NONE"
            ),
        },
        "inventory_delta": 0.0,
        "portfolio_exposure_delta": 0.0,
        "settlement": {
            "definition": "compatibility adapter has no verified settlement binding",
            "source": "V7_LEDGER_SPOOL_CANDIDATE_INGRESS", "verified": False,
        },
        "eligible": True,
        "reasons": ["TEMPORARY_ADAPTER_FORCES_CANCEL_NOTHING_ONLY"],
        "deterministic_replay_key": f"compat:{engine_id}:{identity}",
        "expires_at_ns": decision_ns + 1_000_000_000,
    }
    return OpportunityEnvelope.parse(raw).raw


def envelope_from_ingress(value: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") == "polymarket_v7_opportunity_envelope_v1":
        OpportunityEnvelope.parse(value)
        return value
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    embedded = metadata.get("opportunity_envelope")
    if not isinstance(embedded, dict):
        return _compatibility_envelope(value, context)
    envelope = OpportunityEnvelope.parse(embedded)
    if envelope.raw["model_sha"] != value.get("model_sha"):
        raise OpportunityError("compatibility_model_sha_mismatch")
    ingress = value.get("ingress") if isinstance(value.get("ingress"), dict) else {}
    if envelope.engine_id != ingress.get("engine_id"):
        raise OpportunityError("compatibility_engine_mismatch")
    return envelope.raw


def process_cut(run_root: Path, *, now_ns: int | None = None) -> dict[str, Any]:
    root = Path(run_root)
    current_ns = int(now_ns if now_ns is not None else time.time_ns())
    inbox = root / "opportunities" / "inbox"
    context_path = root / "control" / "runtime_status.json"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if not isinstance(context, dict):
            context = {}
    except (OSError, json.JSONDecodeError):
        context = {}
    files = sorted(inbox.glob("*.json")) if inbox.exists() else []
    envelopes: list[dict[str, Any]] = []
    adapter_errors: list[str] = []
    for index, path in enumerate(files):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise OpportunityError("ingress_not_object")
            envelopes.append(envelope_from_ingress(raw, context))
        except (OSError, json.JSONDecodeError, OpportunityError, ValueError) as exc:
            adapter_errors.append(f"ADAPTER_REJECTED:{index}:{exc}")
    if adapter_errors:
        decision = fail_closed_decision(now_ns=current_ns, reasons=adapter_errors)
    elif envelopes:
        decision = coordinate(envelopes, now_ns=current_ns, new_risk_authorized=False)
    else:
        decision = fail_closed_decision(now_ns=current_ns, reasons=["NO_LIVE_OPPORTUNITIES"])
    decision.update({
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "economic_engine_count": 2,
        "input_count": len(files),
        "valid_envelope_count": len(envelopes),
        "adapter_error_count": len(adapter_errors),
        "new_risk_policy": "CHECKED_IN_DISABLED_NO_RUNTIME_OVERRIDE",
    })
    status = {
        "schema": "polymarket_v7_global_portfolio_coordinator_status_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "state": (
            "IDLE_FAIL_CLOSED" if not files
            else "FAIL_CLOSED" if decision["action"] == "NOTHING"
            else "SAFE_ACTION"
        ),
        "last_decision": decision,
    }
    atomic_json(root / "control" / "global_portfolio_coordinator.json", status)
    if files:
        append_jsonl(root / "opportunities" / "decisions.jsonl", decision)
    archive = root / "opportunities" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for path in files:
        os.replace(path, archive / path.name)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    if not args.loop:
        print(json.dumps(process_cut(args.run_root), sort_keys=True))
        return 0
    while True:
        print(json.dumps(process_cut(args.run_root), sort_keys=True), flush=True)
        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
