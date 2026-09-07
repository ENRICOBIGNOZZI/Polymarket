#!/usr/bin/env python3
"""Build receipt-gated PAPER CANCEL opportunities from the frozen BTC M5 overlay.

This module has zero execution authority.  It requires three independent facts:
(1) the frozen forward experiment passed the activation gate, (2) the live
receive-time trigger is current and rule-identical, and (3) a matching BUY-only
maker PAPER authorization is still active.  It only emits typed opportunity
envelopes; the global coordinator remains the sole decision owner.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from v7_opportunity import OpportunityEnvelope, OpportunityError


BRIDGE_SCHEMA = "polymarket_v7_external_cancel_opportunity_bridge_v1"
ACTIVATION_SCHEMA = "polymarket_v7_external_cancel_activation_v1"
SIGNAL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_live_signal_v1"
EXPERIMENT_ID = "btc-m5-external-cancel-overlay-forward-v1"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _stable(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def _activation_ready(value: dict[str, Any]) -> tuple[bool, str]:
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    rule_sha = str(evidence.get("rule_sha256") or "")
    ready = (
        value.get("schema") == ACTIVATION_SCHEMA
        and value.get("experiment_id") == EXPERIMENT_ID
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
        and value.get("real_money_authority") is False
        and value.get("automatic_promotion") is False
        and value.get("paper_execution_alpha_overlay_eligible") is True
        and value.get("manual_exact_sha_promotion_required") is True
        and value.get("frozen_rule_retuning_allowed") is False
        and value.get("failed_checks") == []
        and len(rule_sha) == 64
        and all(ch in "0123456789abcdef" for ch in rule_sha)
    )
    return ready, rule_sha


def _signal_ready(
    value: dict[str, Any], *, model_sha: str, rule_sha: str, now_ns: int,
) -> tuple[bool, str]:
    trigger_wall = int(value.get("trigger_receive_wall_ns") or 0)
    valid_until_wall = int(value.get("valid_until_wall_ns") or 0)
    publish_wall = int(value.get("publish_wall_ns") or 0)
    ready = (
        value.get("schema") == SIGNAL_SCHEMA
        and value.get("experiment_id") == EXPERIMENT_ID
        and value.get("code_sha") == model_sha
        and value.get("rule_sha256") == rule_sha
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
        and value.get("real_money_authority") is False
        and value.get("automatic_promotion") is False
        and value.get("execution_authority") == "ZERO_AUTHORITY_SIGNAL_ONLY"
        and value.get("shock_source") == "BINANCE_SPOT_TRADES"
        and value.get("confirmation_source") == "COINBASE_SPOT_TOP_OF_BOOK"
        and value.get("confirmation") == "NON_OPPOSING"
        and value.get("shock_window_ms") == 100
        and float(value.get("minimum_absolute_log_return_bp") or 0.0) == 0.30
        and value.get("trigger_cooldown_ms") == 250
        and value.get("trigger_grid_ms") == 25
        and value.get("overlap_warmup_ms") == 300
        and value.get("maximum_live_signal_age_ms") == 100
        and value.get("supported_cancel_side") == "BUY"
        and value.get("confirmed_non_opposing") is True
        and value.get("valid") is True
        and int(value.get("signal_version") or 0) > 0
        and value.get("stale_buy_outcome") in {"YES", "NO"}
        and 0 < trigger_wall <= publish_wall <= now_ns < valid_until_wall
    )
    return ready, str(value.get("stale_buy_outcome") or "")


def _active_make(path: Path, *, model_sha: str) -> dict[str, Any] | None:
    value = _load(path)
    decision = value.get("decision") if isinstance(value.get("decision"), dict) else {}
    envelope = value.get("opportunity_envelope") if isinstance(value.get("opportunity_envelope"), dict) else {}
    plan = envelope.get("execution_plan") if isinstance(envelope.get("execution_plan"), dict) else {}
    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
    if (
        value.get("schema") != "polymarket_v7_authorized_make_intent_v1"
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("owner") != "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        or value.get("execution_authority") != "SIMULATED_PAPER_ONLY"
        or decision.get("action") != "MAKE"
        or decision.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or envelope.get("model_sha") != model_sha
        or envelope.get("action") != "MAKE"
        or envelope.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE"
        or envelope.get("side") not in {"YES", "NO"}
        or len(legs) != 1 or not isinstance(legs[0], dict)
        or legs[0].get("side") != "BUY"
    ):
        return None
    try:
        OpportunityEnvelope.parse(envelope)
    except (OpportunityError, TypeError, ValueError):
        return None
    return value


def build_external_cancel_opportunities(
    run_root: Path, *, now_ns: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = Path(run_root)
    runtime = _load(root / "control" / "runtime_status.json")
    model_sha = str(runtime.get("model_sha") or "")
    activation = _load(root / "control" / "external_cancel_activation.json")
    signal = _load(root / "external_fair" / "external_cancel_signal.json")
    activation_ok, rule_sha = _activation_ready(activation)
    signal_ok, stale_outcome = _signal_ready(
        signal, model_sha=model_sha, rule_sha=rule_sha, now_ns=now_ns,
    ) if activation_ok else (False, "")

    reasons: list[str] = []
    if (
        runtime.get("schema") != "polymarket_v7_runtime_status_v3"
        or runtime.get("paper_only") is not True
        or runtime.get("authenticated_execution") is not False
        or runtime.get("real_order_submission") is not False
        or len(model_sha) != 40
    ):
        reasons.append("RUNTIME_IDENTITY_NOT_READY")
    if not activation_ok:
        reasons.append("EXTERNAL_CANCEL_FORWARD_GATE_NOT_ACTIVE")
    if activation_ok and not signal_ok:
        reasons.append("EXTERNAL_CANCEL_LIVE_SIGNAL_NOT_ACTIVE")
    if reasons:
        return [], {
            "schema": BRIDGE_SCHEMA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "state": "FAIL_CLOSED", "reasons": reasons,
            "active_make_files": 0, "cancel_opportunities": 0,
        }

    live_dir = root / "micro_maker" / "authorized_make" / "live"
    files = sorted(live_dir.glob("*.json")) if live_dir.exists() else []
    output: list[dict[str, Any]] = []
    rejected = 0
    trigger_wall = int(signal["trigger_receive_wall_ns"])
    valid_until_wall = int(signal["valid_until_wall_ns"])
    signal_version = int(signal["signal_version"])
    for path in files:
        active = _active_make(path, model_sha=model_sha)
        if active is None:
            rejected += 1
            continue
        original = active["opportunity_envelope"]
        if original.get("side") != stale_outcome:
            continue
        plan = original["execution_plan"]
        leg = dict(plan["legs"][0])
        replay = _stable(rule_sha, signal_version, original["deterministic_replay_key"])
        raw = {
            "schema": "polymarket_v7_opportunity_envelope_v1",
            "version": 1,
            "model_sha": model_sha,
            "config_hash": str(runtime.get("config_hash") or ""),
            "policy_hash": str(runtime.get("policy_hash") or ""),
            "run_id": str(runtime.get("run_id") or ""),
            "source_snapshot_identity": _stable("external-cancel", replay),
            "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
            "component_provenance": ["professional_maker"],
            "market_id": original["market_id"],
            "event_id": original["event_id"],
            "contract_id": original["contract_id"],
            "mapping_identity": original["mapping_identity"],
            "crypto_context": original["crypto_context"],
            "action": "CANCEL",
            "side": "NONE",
            "decision_receive_timestamp_ns": int(now_ns),
            "source_event_timestamps_ns": [trigger_wall],
            "fair_value": original["fair_value"],
            "conservative_expected_wealth_change": 0.0,
            "cost_vector": {
                "fee": 0.0, "slippage": 0.0, "unwind_loss": 0.0,
                "capital_cost": 0.0, "latency_cost": 0.0,
                "adverse_markout": 0.0, "rebate": 0.0,
            },
            "cost_authority": {
                "fee": "CONSERVATIVE_ZERO", "slippage": "CONSERVATIVE_ZERO",
                "unwind_loss": "CONSERVATIVE_ZERO", "capital_cost": "CONSERVATIVE_ZERO",
                "latency_cost": "CONSERVATIVE_ZERO", "adverse_markout": "CONSERVATIVE_ZERO",
                "rebate": "CONSERVATIVE_ZERO",
            },
            "uncertainty": {"lower_bound": 0.0, "upper_bound": 0.0, "status": "MATURE"},
            "calibration_status": "NOT_APPLICABLE",
            "latency": {
                "profile_id": "frozen-btc-m5-external-cancel-v1",
                "profile_valid": True, "economic_percentile": "p99",
                "arrival_ns": max(0, int(now_ns) - trigger_wall),
            },
            "capacity": {
                "executable_size": float(leg.get("target_quantity") or 0.0),
                "depth_provenance": str(original.get("source_snapshot_identity") or ""),
            },
            "execution_plan": {
                "atomic_unit_id": f"cancel-{replay[:24]}",
                "execution_style": "SINGLE_LEG",
                "legs": [leg],
                "partial_fill_plan": "CANCEL_REMAINDER",
                "timeout_ms": 0,
                "unwind_plan": "CANCEL_ONLY",
            },
            "inventory_delta": 0.0,
            "portfolio_exposure_delta": 0.0,
            "settlement": original["settlement"],
            "eligible": True,
            "reasons": [
                "FROZEN_FORWARD_CANCEL_GATE_PASS",
                "LIVE_RECEIVE_TIME_TRIGGER_ACTIVE",
                f"STALE_BUY_OUTCOME_{stale_outcome}",
                f"CANCEL_SIGNAL_VERSION_{signal_version}",
            ],
            "deterministic_replay_key": f"external-cancel:{replay}",
            "expires_at_ns": valid_until_wall,
        }
        try:
            output.append(OpportunityEnvelope.parse(raw).raw)
        except OpportunityError:
            rejected += 1
    output.sort(key=lambda row: row["deterministic_replay_key"])
    return output, {
        "schema": BRIDGE_SCHEMA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "state": "ACTIVE" if output else "NO_MATCHING_ACTIVE_BUY_QUOTES",
        "reasons": [], "rule_sha256": rule_sha,
        "signal_version": signal_version, "stale_buy_outcome": stale_outcome,
        "active_make_files": len(files), "rejected_active_make_files": rejected,
        "cancel_opportunities": len(output),
    }
