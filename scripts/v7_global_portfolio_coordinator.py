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
from v7_disk_pressure import disk_pressure_status
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


def envelope_from_ingress(value: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") == "polymarket_v7_opportunity_envelope_v1":
        return OpportunityEnvelope.parse(value).raw
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    embedded = metadata.get("opportunity_envelope")
    if not isinstance(embedded, dict):
        raise OpportunityError("canonical_opportunity_envelope_required")
    envelope = OpportunityEnvelope.parse(embedded)
    if envelope.raw["model_sha"] != value.get("model_sha"):
        raise OpportunityError("embedded_model_sha_mismatch")
    ingress = value.get("ingress") if isinstance(value.get("ingress"), dict) else {}
    if ingress and envelope.engine_id != ingress.get("engine_id"):
        raise OpportunityError("embedded_engine_mismatch")
    return envelope.raw

def _is_paper_probe(envelope: dict[str, Any]) -> bool:
    exploration = envelope.get("exploration")
    return isinstance(exploration, dict) and exploration.get("mode") == "PAPER_BOOTSTRAP_PROBE"


def _is_forward_test(envelope: dict[str, Any]) -> bool:
    forward = envelope.get("forward_test")
    return isinstance(forward, dict) and forward.get("mode") == "PAPER_FORWARD_TEST"


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
        and not _is_forward_test(row)
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
    forward_tests = [
        row for row in envelopes
        if row.get("engine_id") == "CRYPTO_SETTLEMENT_ENGINE"
        and row.get("action") == "TAKE" and _is_forward_test(row)
    ]
    forward_keys = {str(row.get("deterministic_replay_key") or "") for row in forward_tests}

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
        if _is_forward_test(row):
            if replay_key in forward_keys:
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
        "paper_forward_test_candidate_count": len(forward_tests),
        "paper_forward_test_never_economic_filtered": True,
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
        or "RESEARCH_CANCEL_RULE_MATCH" not in (envelope.get("reasons") or [])
    ):
        return
    key = str(decision["selected_replay_key"])
    identity = __import__("hashlib").sha256(key.encode()).hexdigest()
    authorized_wall_ns = time.time_ns()
    source_stamps = envelope.get("source_event_timestamps_ns") if isinstance(envelope.get("source_event_timestamps_ns"), list) else []
    signal_trigger_wall_ns = int(source_stamps[0] or 0) if source_stamps else 0
    atomic_json(root / "micro_maker" / "authorized_cancel" / f"{identity}.json", {
        "schema": "polymarket_v7_authorized_cancel_intent_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "execution_authority": "SIMULATED_PAPER_CANCEL_ONLY",
        "selected_replay_key": key,
        "coordinator_authorized_wall_ns": authorized_wall_ns,
        "signal_trigger_wall_ns": signal_trigger_wall_ns,
        "signal_age_ns_at_authorization": max(0, authorized_wall_ns - signal_trigger_wall_ns) if signal_trigger_wall_ns > 0 else None,
        "fast_cancel_path": bool(decision.get("fast_cancel_path")),
        "decision": decision,
        "opportunity_envelope": envelope,
        "expires_at_ns": int(envelope.get("expires_at_ns") or 0),
    })


def _record_authorization_publication(root: Path, decision: dict[str, Any], kind: str) -> None:
    """Observation of an already-published intent; grants no additional authority."""
    key=decision.get('selected_replay_key')
    item=next((x for x in decision.get('opportunity_inputs',[]) if x.get('replay_key')==key),{})
    fingerprint=__import__('hashlib').sha256(json.dumps(decision,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    append_jsonl(root/'opportunities'/'authorization_publications.jsonl',{
        'schema':'polymarket_v7_authorization_publication_v1','paper_only':True,
        'authenticated_execution':False,'real_order_submission':False,
        'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','result':'GENERATED',
        'publication_kind':kind,'attempt_id':kind+':'+fingerprint,'decision_sha256':fingerprint,
        'timestamp_ms':time.time_ns()//1_000_000,'model_sha':item.get('model_sha'),
        'market_id':item.get('market_id'),'token_id':item.get('token_id'),'replay_key':key})


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
    _record_authorization_publication(root,decision,'MAKER_INTENT')


def process_fast_cancel(run_root: Path, *, now_ns: int | None = None) -> dict[str, Any]:
    """Coordinator-owned low-latency CANCEL lane; never authorizes new risk."""
    root = Path(run_root)
    current_ns = int(now_ns if now_ns is not None else time.time_ns())
    started = time.perf_counter_ns()
    try:
        envelopes, diagnostics = build_external_cancel_opportunities(root, now_ns=current_ns)
    except Exception as exc:
        envelopes = []
        diagnostics = {
            "schema": "polymarket_v7_external_cancel_opportunity_bridge_v2",
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "state": "FAIL_CLOSED",
            "reasons": [f"UNEXPECTED_CANCEL_BRIDGE_ERROR:{type(exc).__name__}:{exc}"],
            "cancel_opportunities": 0,
        }

    bridge_compute_ns = time.perf_counter_ns() - started
    if envelopes:
        decision = coordinate(
            envelopes, now_ns=current_ns,
            new_risk_authorized=False, paper_exploration_authorized=True,
        )
    else:
        decision = fail_closed_decision(
            now_ns=current_ns,
            reasons=diagnostics.get("reasons") or ["NO_FAST_CANCEL_OPPORTUNITY"],
        )
    decision["paper_only"] = True
    decision["authenticated_execution"] = False
    decision["real_order_submission"] = False
    decision["real_capital_at_risk"] = False
    decision["fast_cancel_path"] = True
    decision["fast_cancel_bridge_compute_ns"] = int(bridge_compute_ns)

    selected = _selected_envelope(envelopes, decision.get("selected_replay_key"))
    trigger_ns = 0
    if isinstance(selected, dict):
        stamps = selected.get("source_event_timestamps_ns")
        if isinstance(stamps, list) and stamps:
            trigger_ns = int(stamps[0] or 0)
    signal_age_ns = max(0, current_ns - trigger_ns) if trigger_ns > 0 else None
    decision["fast_cancel_signal_age_ns"] = signal_age_ns
    decision["opportunity_inputs"] = [{
        "replay_key": row.get("deterministic_replay_key"),
        "model_sha": row.get("model_sha"), "market_id": row.get("market_id"),
        "token_id": row.get("contract_id"), "action": row.get("action"),
        "component_provenance": row.get("component_provenance"),
        "source_snapshot_identity": row.get("source_snapshot_identity"),
        "paper_probe": False, "retained_after_execution_selection": True,
    } for row in envelopes]
    duplicate_suppressed = False
    if decision.get("action") == "CANCEL" and decision.get("selected_replay_key"):
        key = str(decision["selected_replay_key"])
        identity = __import__("hashlib").sha256(key.encode()).hexdigest()
        cancel_root = root / "micro_maker" / "authorized_cancel"
        already_recorded = any(
            (cancel_root / suffix / f"{identity}.json").exists()
            for suffix in ("", "archive", "rejected")
        )
        if not already_recorded:
            _publish_cancel_authorization(root, decision, envelopes)
            _record_authorization_publication(root, decision, "FAST_CANCEL_INTENT")
            append_jsonl(root / "opportunities" / "fast_cancel_decisions.jsonl", decision)
        else:
            duplicate_suppressed = True
            decision["fast_cancel_duplicate_suppressed"] = True
    status = {
        "schema": "polymarket_v7_fast_cancel_coordinator_status_v1",
        "timestamp_ns": current_ns, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "state": "CANCEL_AUTHORIZED" if decision.get("action") == "CANCEL" else diagnostics.get("state", "IDLE_FAIL_CLOSED"),
        "signal_version": diagnostics.get("signal_version"),
        "cancel_opportunities": len(envelopes),
        "bridge_compute_ns": int(bridge_compute_ns),
        "signal_age_ns": signal_age_ns,
        "selected_replay_key": decision.get("selected_replay_key"),
        "duplicate_suppressed": duplicate_suppressed,
    }
    if decision.get("action") == "CANCEL":
        atomic_json(root / "control" / "fast_cancel_status.json", status)
    return status


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

    disk_pressure = disk_pressure_status(root)
    disk_pressure_active = disk_pressure["active"]
    drain_active = any((root / "control" / name).exists() for name in ("CUTOVER_DRAIN", "KILL", "MAKER_FREEZE")) or disk_pressure_active
    risk_candidates_before_drain = [row for row in selected_envelopes if row.get("action") not in {"CANCEL", "WITHDRAW", "NOTHING"}]
    if drain_active:
        selected_envelopes = [row for row in selected_envelopes if row.get("action") in {"CANCEL", "WITHDRAW", "NOTHING"}]
        execution_alpha_diagnostics["canonical_drain_new_risk_blocked"] = True
        execution_alpha_diagnostics["disk_pressure"] = disk_pressure

    if adapter_errors:
        decision = fail_closed_decision(now_ns=current_ns, reasons=adapter_errors)
    elif selected_envelopes:
        decision = coordinate(
            selected_envelopes,
            now_ns=current_ns,
            new_risk_authorized=False,
            paper_exploration_authorized=True,
        )
    elif disk_pressure_active and risk_candidates_before_drain:
        decision = fail_closed_decision(now_ns=current_ns, reasons=["DISK_PRESSURE_NEW_RISK_BLOCKED"])
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
    # Identity-level evidence, distinct from how many times the cut was tried.
    # Retain all valid inputs, including those rejected by portfolio selection.
    retained_keys = {row.get("deterministic_replay_key") for row in selected_envelopes}
    decision["opportunity_inputs"] = [{
        "replay_key": row.get("deterministic_replay_key"),
        "model_sha": row.get("model_sha"), "market_id": row.get("market_id"),
        "token_id": row.get("contract_id"), "action": row.get("action"),
        "component_provenance": row.get("component_provenance"),
        "source_snapshot_identity": row.get("source_snapshot_identity"),
        "paper_probe": isinstance(row.get("exploration"), dict),
        "retained_after_execution_selection": row.get("deterministic_replay_key") in retained_keys,
    } for row in envelopes]
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
        if decision.get('action')=='TAKE':_record_authorization_publication(root,decision,'TAKER_RECEIPT')
    _publish_make_authorization(root, decision, selected_envelopes)
    _publish_cancel_authorization(root, decision, selected_envelopes)
    if files or maker_envelopes:
        append_jsonl(root / "opportunities" / "decisions.jsonl", decision)
    archive = root / "opportunities" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for path in files:
        os.replace(path, archive / path.name)
    return status



def process_fast_forward_take(
    run_root: Path, *, now_ns: int | None = None, risk_preempt: bool = False,
) -> dict[str, Any]:
    """5ms PAPER forward lane owned by the same global coordinator."""
    root = Path(run_root)
    current_ns = int(now_ns if now_ns is not None else time.time_ns())
    inbox = root / "opportunities" / "fast_forward_inbox"
    archive = root / "opportunities" / "fast_forward_archive"
    rejected = root / "opportunities" / "fast_forward_rejected"
    files = sorted(inbox.glob("*.json")) if inbox.exists() else []
    if not files:
        return {
            "schema": "polymarket_v7_fast_forward_coordinator_status_v1",
            "timestamp_ns": current_ns, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "real_capital_at_risk": False, "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "state": "IDLE", "pending": 0, "risk_preempt": bool(risk_preempt),
        }
    path = files[0]
    started = time.perf_counter_ns()
    raw: dict[str, Any] | None = None
    error = ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise OpportunityError("fast_forward_not_object")
        parsed = OpportunityEnvelope.parse(value)
        if (not parsed.is_forward_test or parsed.action != "TAKE"
                or parsed.engine_id != "CRYPTO_SETTLEMENT_ENGINE"):
            raise OpportunityError("fast_forward_wrong_envelope")
        raw = value
    except (OSError, json.JSONDecodeError, OpportunityError, TypeError, ValueError) as exc:
        error = f"FAST_FORWARD_REJECTED:{type(exc).__name__}:{exc}"
    disk_pressure = disk_pressure_status(root)
    disk_pressure_active = disk_pressure["active"]
    drain_active = any((root / "control" / name).exists()
                       for name in ("CUTOVER_DRAIN", "KILL", "MAKER_FREEZE")) or disk_pressure_active
    if error:
        decision = fail_closed_decision(now_ns=current_ns, reasons=[error])
    elif disk_pressure_active:
        decision = fail_closed_decision(now_ns=current_ns, reasons=["DISK_PRESSURE_NEW_RISK_BLOCKED"])
    elif drain_active:
        decision = fail_closed_decision(now_ns=current_ns, reasons=["CANONICAL_DRAIN_NEW_RISK_BLOCKED"])
    elif risk_preempt:
        decision = fail_closed_decision(now_ns=current_ns, reasons=["RISK_ACTION_PREEMPTS_FORWARD_ALPHA"])
    else:
        decision = coordinate([raw], now_ns=current_ns, new_risk_authorized=False,
                              paper_exploration_authorized=True)
    decision.update({
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "fast_forward_path": True,
        "fast_forward_compute_ns": int(time.perf_counter_ns() - started),
    })
    if isinstance(raw, dict):
        decision["opportunity_inputs"] = [{
            "replay_key": raw.get("deterministic_replay_key"),
            "model_sha": raw.get("model_sha"), "market_id": raw.get("market_id"),
            "token_id": raw.get("contract_id"), "action": raw.get("action"),
            "component_provenance": raw.get("component_provenance"),
            "source_snapshot_identity": raw.get("source_snapshot_identity"),
            "paper_forward_test": True, "retained_after_execution_selection": True,
        }]
    authorized = (
        decision.get("action") == "TAKE"
        and decision.get("paper_exploration_authorized") is True
        and decision.get("paper_forward_test_authorized") is True
        and decision.get("new_risk_authorized") is False
        and isinstance(decision.get("selected_replay_key"), str)
        and bool(decision.get("selected_replay_key"))
    )
    if authorized:
        receipt_name = decision["selected_replay_key"].replace("/", "_") + ".json"
        atomic_json(root / "opportunities" / "receipts" / receipt_name, decision)
        _record_authorization_publication(root, decision, "FAST_FORWARD_TAKER_RECEIPT")
        append_jsonl(root / "opportunities" / "fast_forward_decisions.jsonl", decision)
    destination = archive if authorized else rejected
    destination.mkdir(parents=True, exist_ok=True)
    os.replace(path, destination / path.name)
    status = {
        "schema": "polymarket_v7_fast_forward_coordinator_status_v1",
        "timestamp_ns": current_ns, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "real_capital_at_risk": False, "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "state": "TAKE_AUTHORIZED" if authorized else "FAIL_CLOSED",
        "risk_preempt": bool(risk_preempt), "pending": max(0, len(files) - 1),
        "selected_replay_key": decision.get("selected_replay_key"),
        "reasons": decision.get("reasons") or [],
        "compute_ns": decision["fast_forward_compute_ns"],
    }
    atomic_json(root / "control" / "fast_forward_status.json", status)
    return status


def coordinate_reserved_paper(
    envelopes: list[dict[str, Any]], *, now_ns: int,
    reservation_projection: Any, requests_by_replay_key: dict[str, Any],
    append_event: Any, entry_gate_open: bool = False,
) -> dict[str, Any]:
    """Opt-in in-memory admission under this SAME coordinator, not a new loop.

    The frozen BTC process never calls this helper. Existing typed coordinate()
    remains the authority gate: this does not make new assets PAPER-eligible.
    The caller must supply the canonical writer's synchronous durable append,
    a completely reconciled account projection and a current KILL/drain gate.
    """
    from v7_coordinator_reservations import ReservationProjection, ReservationRequest
    from v7_lead_lag_replay import ReplayError, decimal

    if type(now_ns) is not int or now_ns <= 0:
        raise OpportunityError("reserved_paper_clock_invalid")
    decision = coordinate(envelopes, now_ns=now_ns, new_risk_authorized=False,
                          paper_exploration_authorized=True)
    if decision.get("action") in {"CANCEL", "WITHDRAW", "NOTHING"}:
        return decision
    try:
        if entry_gate_open is not True:
            raise ReplayError("KILL_DRAIN_OR_NEW_ENTRY_GATE_CLOSED")
        if decision.get("action") != "TAKE" or not isinstance(reservation_projection, ReservationProjection):
            raise ReplayError("RESERVATION_PATH_REQUIRES_PAPER_TAKE")
        key = decision.get("selected_replay_key")
        request = requests_by_replay_key.get(key)
        raw = _selected_envelope(envelopes, key)
        if not isinstance(request, ReservationRequest) or not isinstance(raw, dict):
            raise ReplayError("RESERVATION_REQUEST_MISSING")
        legs = raw["execution_plan"]["legs"]
        context = raw["crypto_context"]
        protocol = (raw.get("forward_test") or {}).get("protocol_hash") or raw["policy_hash"]
        if (len(legs) != 1 or legs[0]["side"] != "BUY"
                or request.market_id != raw["market_id"] or legs[0]["contract_id"] != raw["contract_id"]
                or request.token_id != legs[0]["token_id"] or request.market_id != legs[0]["market_id"]
                or request.coordinator_replay_key != key or request.protocol_hash != protocol
                or request.asset != context["asset"] or request.horizon != context["horizon"]
                or request.strategy != raw["engine_id"]
                or request.quantity != decimal(legs[0]["target_quantity"])
                or request.limit_price != decimal(legs[0]["limit_price"])
                or request.expires_wall_ms * 1_000_000 > raw["expires_at_ns"]
                or request.maximum_debit < request.quantity * request.limit_price + decimal(raw["cost_vector"]["fee"])):
            raise ReplayError("RESERVATION_ENVELOPE_BINDING_MISMATCH")
        event = reservation_projection.reserve(request, now_ms=now_ns // 1_000_000,
            receipt=decision, append=append_event, entry_gate_open=True)
        return dict(decision, reservation_id=request.key, reservation_record_id=event.record_id,
                    reservation_durable=True, automatic_promotion=False)
    except (ReplayError, ValueError, KeyError, TypeError, OSError) as exc:
        return fail_closed_decision(now_ns=now_ns,
            reasons=[f"RESERVATION_FAIL_CLOSED:{type(exc).__name__}:{exc}"])


def _run_loop(args: argparse.Namespace, journal: Any | None = None) -> int:
    full_interval = max(0.05, float(args.interval))
    fast_interval = max(0.002, float(args.fast_cancel_interval))
    next_full = time.monotonic()
    next_fast = next_full
    while True:
        now = time.monotonic()
        if now >= next_fast:
            cancel_status = process_fast_cancel(args.run_root)
            process_fast_forward_take(
                args.run_root,
                risk_preempt=cancel_status.get("state") == "CANCEL_AUTHORIZED",
            )
            next_fast = now + fast_interval
        now = time.monotonic()
        if now >= next_full:
            status = process_cut(args.run_root)
            if journal is not None:
                journal.append(status)
            else:
                print(json.dumps(status, sort_keys=True), flush=True)
            next_full = now + full_interval
        if not args.loop:
            return 0
        sleep_for = min(next_fast, next_full) - time.monotonic()
        time.sleep(max(0.0005, sleep_for))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--fast-cancel-interval", type=float, default=0.005)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--event-log", type=Path)
    args = parser.parse_args()
    if args.event_log:
        from v7_compressed_journal import CompressedJournal
        with CompressedJournal(args.event_log) as journal:
            return _run_loop(args, journal)
    return _run_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
