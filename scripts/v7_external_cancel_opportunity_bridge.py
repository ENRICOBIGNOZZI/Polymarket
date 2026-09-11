#!/usr/bin/env python3
"""Build PAPER CANCEL opportunities from the current BTC M5 research rule.

This module has zero execution authority. It requires a current receive-time signal that is hash-identical to the checked-in research rule and a matching active BUY-only PAPER maker order. The global coordinator remains the sole decision owner.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from v7_opportunity import OpportunityEnvelope, OpportunityError


BRIDGE_SCHEMA = "polymarket_v7_external_cancel_opportunity_bridge_v2"
SIGNAL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_live_signal_v2"
RULE_ID = "btc-m5-external-cancel-v1"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "v7_crypto_execution_alpha.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _stable(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ")).encode()).hexdigest()


@lru_cache(maxsize=4)
def _research_rule(path: Path = CONFIG_PATH) -> tuple[dict[str, Any], str]:
    """Load immutable checked-in rule once per process, not every 5 ms fast poll."""
    value = _load(path)
    execution = value.get("execution_alpha") if isinstance(value.get("execution_alpha"), dict) else {}
    cancel = execution.get("cancel") if isinstance(execution.get("cancel"), dict) else {}
    rule = cancel.get("research_rule") if isinstance(cancel.get("research_rule"), dict) else {}
    ready = (
        value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
        and cancel.get("research_only") is True
        and rule.get("rule_id") == RULE_ID
        and rule.get("shock_source") == "BINANCE_SPOT_TRADES"
        and rule.get("confirmation_source") == "COINBASE_SPOT_TOP_OF_BOOK"
        and rule.get("confirmation") == "NON_OPPOSING"
        and rule.get("shock_window_ms") == 100
        and float(rule.get("minimum_absolute_log_return_bp") or 0.0) == 0.30
        and rule.get("trigger_cooldown_ms") == 250
        and rule.get("trigger_grid_ms") == 25
        and rule.get("overlap_warmup_ms") == 300
        and rule.get("maximum_signal_age_ms") == 100
        and rule.get("supported_cancel_side") == "BUY"
        and rule.get("receive_time_global_merge_required") is True
        and rule.get("same_timestamp_atomic_group_required") is True
    )
    return (dict(rule), _canonical_hash(rule)) if ready else ({}, "")


def _signal_ready(
    value: dict[str, Any], *, model_sha: str, rule: dict[str, Any], rule_sha: str, now_ns: int,
) -> tuple[bool, str]:
    trigger_wall = int(value.get("trigger_receive_wall_ns") or 0)
    valid_until_wall = int(value.get("valid_until_wall_ns") or 0)
    publish_wall = int(value.get("publish_wall_ns") or 0)
    ready = (
        value.get("schema") == SIGNAL_SCHEMA
        and value.get("rule_id") == RULE_ID
        and value.get("code_sha") == model_sha
        and value.get("rule_sha256") == rule_sha
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
        and value.get("real_money_authority") is False
        and value.get("research_only") is True
        and value.get("execution_authority") == "ZERO_AUTHORITY_SIGNAL_ONLY"
        and value.get("shock_source") == rule.get("shock_source")
        and value.get("confirmation_source") == rule.get("confirmation_source")
        and value.get("confirmation") == rule.get("confirmation")
        and value.get("shock_window_ms") == rule.get("shock_window_ms")
        and float(value.get("minimum_absolute_log_return_bp") or 0.0) == float(rule.get("minimum_absolute_log_return_bp") or 0.0)
        and value.get("trigger_cooldown_ms") == rule.get("trigger_cooldown_ms")
        and value.get("trigger_grid_ms") == rule.get("trigger_grid_ms")
        and value.get("overlap_warmup_ms") == rule.get("overlap_warmup_ms")
        and value.get("maximum_signal_age_ms") == rule.get("maximum_signal_age_ms")
        and value.get("supported_cancel_side") == rule.get("supported_cancel_side")
        and value.get("confirmed_non_opposing") is True
        and value.get("valid") is True
        and int(value.get("signal_version") or 0) > 0
        and value.get("stale_buy_outcome") in {"YES", "NO"}
        and 0 < trigger_wall <= publish_wall <= now_ns < valid_until_wall
    )
    return ready, str(value.get("stale_buy_outcome") or "")


EXECUTOR_STATUS_SCHEMA = "polymarket_v7_authorized_maker_paper_executor_status_v1"
EXECUTOR_STATUS_MAX_AGE_MS = 5_000


def _active_executor_orders(
    path: Path, *, model_sha: str, now_ns: int,
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], bool]:
    value = _load(path)
    now_ms = int(now_ns) // 1_000_000
    timestamp_ms = int(value.get("timestamp_ms") or 0)
    ready = (
        value.get("schema") == EXECUTOR_STATUS_SCHEMA
        and value.get("model_sha") == model_sha
        and value.get("paper_only") is True
        and value.get("authenticated_execution") is False
        and value.get("real_order_submission") is False
        and value.get("real_capital_at_risk") is False
        and value.get("execution_authority") == "SIMULATED_PAPER_ONLY"
        and 0 < timestamp_ms <= now_ms
        and now_ms - timestamp_ms <= EXECUTOR_STATUS_MAX_AGE_MS
    )
    rows = value.get("active_order_details") if isinstance(value.get("active_order_details"), list) else []
    output: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not ready:
        return output, False
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        replay = str(raw.get("replay_key") or "")
        market = str(raw.get("market_id") or "")
        token = str(raw.get("token_id") or "")
        order_id = str(raw.get("order_id") or "")
        outcome = str(raw.get("outcome") or "")
        side = str(raw.get("side") or "")
        try:
            remaining = float(raw.get("remaining_shares") or 0.0)
            price = float(raw.get("limit_price") or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not replay or not market or not token or not order_id
            or outcome not in {"YES", "NO"} or side != "BUY"
            or not remaining > 0.0 or not 0.0 < price < 1.0
            or raw.get("cancel_requested") is True
        ):
            continue
        output[(replay, market, token)] = {
            "order_id": order_id, "replay_key": replay, "market_id": market,
            "event_id": str(raw.get("event_id") or ""), "token_id": token,
            "outcome": outcome, "side": side, "remaining_shares": remaining,
            "limit_price": price,
        }
    return output, True


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


def _active_make_paths(
    root: Path, active_orders: dict[tuple[str, str, str], dict[str, Any]],
) -> list[Path]:
    """Resolve only currently active authorizations; never scan the live directory."""
    live_dir = root / "micro_maker" / "authorized_make" / "live"
    if not live_dir.exists():
        return []
    identities = {
        hashlib.sha256(str(replay).encode()).hexdigest()
        for replay, _market, _token in active_orders
        if replay
    }
    return [
        path for identity in sorted(identities)
        if (path := live_dir / f"{identity}.json").is_file()
    ]


def build_external_cancel_opportunities(
    run_root: Path, *, now_ns: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = Path(run_root)
    runtime = _load(root / "control" / "runtime_status.json")
    model_sha = str(runtime.get("model_sha") or "")
    signal = _load(root / "external_fair" / "external_cancel_signal.json")
    active_orders, executor_ready = _active_executor_orders(
        root / "micro_maker" / "authorized_make_executor_status.json",
        model_sha=model_sha, now_ns=now_ns,
    )
    rule, rule_sha = _research_rule()
    rule_ok = bool(rule and rule_sha)
    signal_ok, stale_outcome = _signal_ready(
        signal, model_sha=model_sha, rule=rule, rule_sha=rule_sha, now_ns=now_ns,
    ) if rule_ok else (False, "")

    reasons: list[str] = []
    if (
        runtime.get("schema") != "polymarket_v7_runtime_status_v3"
        or runtime.get("paper_only") is not True
        or runtime.get("authenticated_execution") is not False
        or runtime.get("real_order_submission") is not False
        or len(model_sha) != 40
    ):
        reasons.append("RUNTIME_IDENTITY_NOT_READY")
    if not rule_ok:
        reasons.append("EXTERNAL_CANCEL_RESEARCH_RULE_NOT_READY")
    if rule_ok and not signal_ok:
        reasons.append("EXTERNAL_CANCEL_LIVE_SIGNAL_NOT_ACTIVE")
    if not executor_ready:
        reasons.append("MAKER_EXECUTOR_ACTIVE_ORDER_STATE_NOT_READY")
    if reasons:
        return [], {
            "schema": BRIDGE_SCHEMA, "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "state": "FAIL_CLOSED", "reasons": reasons,
            "active_make_files": 0, "cancel_opportunities": 0,
        }

    files = _active_make_paths(root, active_orders)
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
        original_leg = plan["legs"][0]
        original_replay = str(original.get("deterministic_replay_key") or "")
        exact = active_orders.get((
            original_replay, str(original.get("market_id") or ""),
            str(original_leg.get("token_id") or ""),
        ))
        if exact is None or exact["outcome"] != stale_outcome:
            continue
        leg = dict(original_leg)
        leg["leg_id"] = exact["order_id"]
        leg["target_quantity"] = exact["remaining_shares"]
        leg["limit_price"] = exact["limit_price"]
        replay = _stable(
            rule_sha, signal_version, exact["order_id"], exact["replay_key"],
        )
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
                "profile_id": "btc-m5-external-cancel-research-v1",
                "profile_valid": True, "economic_percentile": "p99",
                "arrival_ns": max(0, int(now_ns) - trigger_wall),
            },
            "capacity": {
                "executable_size": float(exact["remaining_shares"]),
                "depth_provenance": str(original.get("source_snapshot_identity") or ""),
            },
            "execution_plan": {
                "atomic_unit_id": exact["replay_key"],
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
                "RESEARCH_CANCEL_RULE_MATCH",
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
        "active_make_files": len(files), "active_executor_orders": len(active_orders),
        "rejected_active_make_files": rejected, "cancel_opportunities": len(output),
        "exact_order_targeting": True,
        "active_make_lookup": "DIRECT_REPLAY_KEY_HASH",
        "static_rule_cached": True,
    }
