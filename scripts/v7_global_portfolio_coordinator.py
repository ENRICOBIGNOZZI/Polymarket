#!/usr/bin/env python3
"""Single runtime consumer for both V7 economic-engine opportunity cuts.

Checked-in operation is PAPER observation only: this coordinator has no flag
that can authorize real new risk. It validates fully typed envelopes, compares
both engines on conservative expected account-wealth change, gives CANCEL
priority, concentrates ordinary crypto risk on the best market windows, and
keeps minimum-size PAPER exploration on a separate information-gain lane.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from v7_opportunity import OpportunityEnvelope, OpportunityError, coordinate, fail_closed_decision
from v7_crypto_settlement import aggregate_correlated_crypto_risk
from v7_crypto_execution_alpha import (
    ExecutionAlphaError,
    action_competition,
    information_rank,
    load_config as load_execution_alpha_config,
    select_crypto_markets,
)
from v7_maker_opportunity_bridge import build_maker_opportunities
from v7_external_cancel_opportunity_bridge import build_external_cancel_opportunities


ROOT = Path(__file__).resolve().parents[1]
EXECUTION_ALPHA_CONFIG = ROOT / "config" / "v7_crypto_execution_alpha.json"


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
    "CRYPTO_SETTLEMENT_FAIR": ("CRYPTO_SETTLEMENT_ENGINE", "crypto_settlement_fair"),
    "CRYPTO_INFORMED_TAKER": ("CRYPTO_SETTLEMENT_ENGINE", "crypto_informed_taker"),
    "MICRO_MAKER_PRO": ("CRYPTO_SETTLEMENT_ENGINE", "professional_maker"),
    "PROFESSIONAL_MAKER": ("CRYPTO_SETTLEMENT_ENGINE", "professional_maker"),
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
    crypto_context = None
    if engine_id == "CRYPTO_SETTLEMENT_ENGINE":
        crypto_context = metadata.get("crypto_context")
        if not isinstance(crypto_context, dict):
            raise OpportunityError("compatibility_crypto_context_missing")
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
        "crypto_context": crypto_context,
        # The adapter cannot manufacture missing settlement, latency or
        # calibration evidence. Preserve diagnostic economics but force the
        # actionable surface to NOTHING until the producer emits a typed
        # opportunity envelope of its own.
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
            "economic_percentile": "p99", "arrival_ns": 1,
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


def _is_paper_probe(envelope: dict[str, Any]) -> bool:
    exploration = envelope.get("exploration")
    return isinstance(exploration, dict) and exploration.get("mode") == "PAPER_BOOTSTRAP_PROBE"


def _execution_alpha_cut(
    envelopes: list[dict[str, Any]], config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Concentrate ordinary crypto risk while preserving safe/probe actions.

    CANCEL/WITHDRAW are never filtered. Structural opportunities keep their own
    engine selection. Minimum-size PAPER probes are deliberately exempt from the
    top-market economic filter so the experiment can learn underrepresented
    strata; at most one probe is left in the cut, selected before outcome using
    declared information value.
    """
    ordinary_crypto = [
        row for row in envelopes
        if row.get("engine_id") == "CRYPTO_SETTLEMENT_ENGINE"
        and row.get("action") in {"MAKE", "TAKE"}
        and not _is_paper_probe(row)
    ]
    selection = select_crypto_markets(ordinary_crypto, config)
    retained = selection.retained_replay_keys
    probes = [
        row for row in envelopes
        if row.get("engine_id") == "CRYPTO_SETTLEMENT_ENGINE"
        and row.get("action") in {"MAKE", "TAKE"}
        and _is_paper_probe(row)
    ]
    best_probe_key = None
    if probes:
        best_probe = max(probes, key=lambda row: information_rank(row, config))
        best_probe_key = str(best_probe.get("deterministic_replay_key") or "")

    filtered: list[dict[str, Any]] = []
    filtered_economic = 0
    filtered_probes = 0
    for row in envelopes:
        engine = row.get("engine_id")
        action = row.get("action")
        replay_key = str(row.get("deterministic_replay_key") or "")
        if engine != "CRYPTO_SETTLEMENT_ENGINE" or action not in {"MAKE", "TAKE"}:
            filtered.append(row)
            continue
        if _is_paper_probe(row):
            if replay_key == best_probe_key:
                filtered.append(row)
            else:
                filtered_probes += 1
            continue
        if replay_key in retained:
            filtered.append(row)
        else:
            filtered_economic += 1

    diagnostics = dict(selection.diagnostics)
    diagnostics.update({
        "action_competition": action_competition(envelopes),
        "ordinary_crypto_candidate_count": len(ordinary_crypto),
        "paper_probe_candidate_count": len(probes),
        "selected_probe_replay_key": best_probe_key,
        "filtered_ordinary_crypto_candidates": filtered_economic,
        "filtered_paper_probe_candidates": filtered_probes,
        "coordinator_input_after_selection": len(filtered),
        "risk_actions_never_filtered": True,
        "structural_engine_never_filtered": True,
        "paper_probe_market_filter_exemption": "ONE_INFORMATION_RANKED_MINIMUM_SIZE_PROBE",
    })
    return filtered, diagnostics


def _selected_envelope(envelopes: list[dict[str, Any]], replay_key: Any) -> dict[str, Any] | None:
    key = str(replay_key or "")
    return next(
        (row for row in envelopes if str(row.get("deterministic_replay_key") or "") == key),
        None,
    )


def _publish_cancel_authorization(root: Path, decision: dict[str, Any], envelopes: list[dict[str, Any]]) -> None:
    """Publish coordinator-owned PAPER cancel intent for the maker executor."""
    if (
        decision.get("action") != "CANCEL"
        or decision.get("new_risk_authorized") is not False
        or not decision.get("selected_replay_key")
    ):
        return
    envelope = _selected_envelope(envelopes, decision.get("selected_replay_key"))
    if not isinstance(envelope, dict):
        return
    if (
        envelope.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or envelope.get("action") != "CANCEL"
        or "FROZEN_FORWARD_CANCEL_GATE_PASS" not in (envelope.get("reasons") or [])
    ):
        return
    key = str(decision["selected_replay_key"])
    identity = __import__("hashlib").sha256(key.encode()).hexdigest()
    atomic_json(root / "micro_maker" / "authorized_cancel" / f"{identity}.json", {
        "schema": "polymarket_v7_authorized_cancel_intent_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "execution_authority": "SIMULATED_PAPER_CANCEL_ONLY",
        "selected_replay_key": key,
        "decision": decision,
        "opportunity_envelope": envelope,
        "expires_at_ns": int(envelope.get("expires_at_ns") or 0),
    })


def _publish_make_authorization(root: Path, decision: dict[str, Any], envelopes: list[dict[str, Any]]) -> None:
    """Publish coordinator-owned PAPER intent; this does not simulate a fill."""
    if (
        decision.get("action") != "MAKE"
        or decision.get("paper_exploration_authorized") is not True
        or decision.get("new_risk_authorized") is not False
        or not decision.get("selected_replay_key")
    ):
        return
    envelope = _selected_envelope(envelopes, decision.get("selected_replay_key"))
    if not isinstance(envelope, dict):
        return
    key = str(decision["selected_replay_key"])
    identity = __import__("hashlib").sha256(key.encode()).hexdigest()
    atomic_json(root / "micro_maker" / "authorized_make" / f"{identity}.json", {
        "schema": "polymarket_v7_authorized_make_intent_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "execution_authority": "SIMULATED_PAPER_ONLY",
        "selected_replay_key": key,
        "decision": decision,
        "opportunity_envelope": envelope,
        "expires_at_ns": int(envelope.get("expires_at_ns") or 0),
    })


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

    try:
        maker_envelopes, maker_diagnostics = build_maker_opportunities(
            root, now_ns=current_ns, repository_root=ROOT,
        )
        envelopes.extend(maker_envelopes)
    except Exception as exc:  # maker fault containment: disable maker, preserve other engines
        maker_envelopes = []
        maker_diagnostics = {
            "schema": "polymarket_v7_maker_opportunity_bridge_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "FAIL_CLOSED",
            "reasons": [f"UNEXPECTED_BRIDGE_ERROR:{type(exc).__name__}:{exc}"],
            "candidate_cells": 0,
            "typed_make_opportunities": 0,
        }

    try:
        cancel_envelopes, cancel_diagnostics = build_external_cancel_opportunities(
            root, now_ns=current_ns,
        )
        envelopes.extend(cancel_envelopes)
    except Exception as exc:  # cancel fault containment: never block other safe decisions
        cancel_envelopes = []
        cancel_diagnostics = {
            "schema": "polymarket_v7_external_cancel_opportunity_bridge_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "state": "FAIL_CLOSED",
            "reasons": [f"UNEXPECTED_CANCEL_BRIDGE_ERROR:{type(exc).__name__}:{exc}"],
            "active_make_files": 0,
            "cancel_opportunities": 0,
        }

    execution_alpha_diagnostics: dict[str, Any] = {
        "schema": "polymarket_v7_crypto_execution_alpha_selection_v1",
        "state": "NOT_EVALUATED",
    }
    selected_envelopes = envelopes
    if not adapter_errors:
        try:
            execution_config = load_execution_alpha_config(EXECUTION_ALPHA_CONFIG)
            selected_envelopes, execution_alpha_diagnostics = _execution_alpha_cut(
                envelopes, execution_config,
            )
            execution_alpha_diagnostics["state"] = "EVALUATED"
            execution_alpha_diagnostics["config_path"] = str(EXECUTION_ALPHA_CONFIG)
        except (OSError, json.JSONDecodeError, ExecutionAlphaError, ValueError) as exc:
            adapter_errors.append(f"EXECUTION_ALPHA_FAIL_CLOSED:{type(exc).__name__}:{exc}")
            execution_alpha_diagnostics = {
                "schema": "polymarket_v7_crypto_execution_alpha_selection_v1",
                "state": "FAIL_CLOSED",
                "config_path": str(EXECUTION_ALPHA_CONFIG),
                "error": f"{type(exc).__name__}:{exc}",
            }

    drain_active = any((root / "control" / name).exists() for name in ("CUTOVER_DRAIN", "KILL", "MAKER_FREEZE"))
    if drain_active:
        selected_envelopes = [row for row in selected_envelopes if row.get("action") in {"CANCEL", "WITHDRAW", "NOTHING"}]
        execution_alpha_diagnostics["canonical_drain_new_risk_blocked"] = True

    if adapter_errors:
        decision = fail_closed_decision(now_ns=current_ns, reasons=adapter_errors)
    elif selected_envelopes:
        decision = coordinate(
            selected_envelopes,
            now_ns=current_ns,
            new_risk_authorized=False,
            paper_exploration_authorized=True,
        )
    else:
        decision = fail_closed_decision(now_ns=current_ns, reasons=["NO_LIVE_OPPORTUNITIES"])

    crypto_exposures = []
    for envelope in envelopes:
        context_row = envelope.get("crypto_context")
        if envelope.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE" or not isinstance(context_row, dict):
            continue
        settlement = envelope.get("settlement") if isinstance(envelope.get("settlement"), dict) else {}
        capacity = envelope.get("capacity") if isinstance(envelope.get("capacity"), dict) else {}
        crypto_exposures.append({
            "asset": context_row.get("asset"), "horizon": context_row.get("horizon"),
            "signed_exposure_usd": envelope.get("portfolio_exposure_delta", 0.0),
            "oracle_source": settlement.get("source", "UNKNOWN"),
            "exchange_source": capacity.get("depth_provenance", "UNKNOWN"),
        })
    decision["crypto_correlation_risk"] = aggregate_correlated_crypto_risk(crypto_exposures)
    execution_alpha_diagnostics["maker_opportunity_bridge"] = maker_diagnostics
    execution_alpha_diagnostics["external_cancel_opportunity_bridge"] = cancel_diagnostics
    decision["crypto_execution_alpha"] = execution_alpha_diagnostics
    decision.update({
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "economic_engine_count": 2,
        "input_count": len(files),
        "generated_maker_opportunity_count": len(maker_envelopes),
        "generated_cancel_opportunity_count": len(cancel_envelopes),
        "valid_envelope_count": len(envelopes),
        "selected_envelope_count": len(selected_envelopes),
        "adapter_error_count": len(adapter_errors),
        "new_risk_policy": "CHECKED_IN_DISABLED_NO_RUNTIME_OVERRIDE",
        "paper_exploration_policy": "BTC_M5_BOUNDED_NO_REAL_MONEY",
        "market_selection_policy": "TOP_CONSERVATIVE_OPPORTUNITY_VALUE_WITH_SEPARATE_INFORMATION_PROBES",
    })
    status = {
        "schema": "polymarket_v7_global_portfolio_coordinator_status_v1",
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "state": (
            "IDLE_FAIL_CLOSED" if not files and not maker_envelopes
            else "FAIL_CLOSED" if decision["action"] == "NOTHING"
            else "SAFE_ACTION"
        ),
        "last_decision": decision,
    }
    atomic_json(root / "control" / "global_portfolio_coordinator.json", status)
    if (
        decision.get("paper_exploration_authorized") is True
        and decision.get("new_risk_authorized") is False
        and decision.get("action") in {"MAKE", "TAKE"}
        and isinstance(decision.get("selected_replay_key"), str)
        and decision.get("selected_replay_key")
    ):
        receipt_name = decision["selected_replay_key"].replace("/", "_") + ".json"
        atomic_json(root / "opportunities" / "receipts" / receipt_name, decision)
    _publish_make_authorization(root, decision, selected_envelopes)
    _publish_cancel_authorization(root, decision, selected_envelopes)
    if files or maker_envelopes:
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
