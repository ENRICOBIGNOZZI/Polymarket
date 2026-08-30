#!/usr/bin/env python3
"""Read-only monitoring summary for the V7 settlement-aware external-fair plane.

The component reports its observed PAPER or zero-authority state explicitly.
Missing status must not take the incumbent runtime down. Once a market is
explicitly external_fair_required, status fields are surfaced so Prometheus
rules can fail closed on invalid contract/oracle/external/fair state.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ACTIONS = ("MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING")
_PURPOSES = ("ALPHA", "INVENTORY_REDUCTION", "RISK", "LIQUIDATION")
_CONTINUITY = {"LIVE_CONTINUOUS", "RECOVERED_SAME_ORACLE_SNAPSHOT", "CONTINUITY_UNKNOWN"}
_SHADOW_AUTHORITIES = {"SHADOW", "SHADOW_ZERO_AUTHORITY", "ZERO_EXECUTION_AUTHORITY"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _model_pointer(path: Path) -> dict[str, Any]:
    raw = _load_json(path)
    if not raw:
        return {}
    model_hash = str(raw.get("model_hash") or "")
    return {
        "role": str(raw.get("role") or "UNKNOWN"),
        "model_version": str(raw.get("model_version") or ""),
        "model_hash": model_hash,
        "model_hash_valid": bool(re.fullmatch(r"[0-9a-f]{64}", model_hash)),
        "published_timestamp_ns": _integer(
            raw.get("published_timestamp_ns", raw.get("promoted_timestamp_ns")), 0
        ),
    }


def summarize_external_fair(
    run_root: Path,
    repository_root: Path,
    *,
    runtime_sha: str = "",
    now_s: int | None = None,
) -> dict[str, Any]:
    run_root = Path(run_root).resolve()
    repository_root = Path(repository_root).resolve()
    now_s = int(time.time()) if now_s is None else int(now_s)
    external_root = run_root / "external_fair"
    status_path = external_root / "status.json"
    status = _load_json(status_path)
    config = _load_json(repository_root / "config" / "v7_external_fair.json")

    authority = str(
        status.get("execution_authority")
        or config.get("execution_authority")
        or "SHADOW_ZERO_AUTHORITY"
    ).upper()
    paper_only = bool(status.get("paper_only", config.get("paper_only", True)))
    authenticated_execution = bool(
        status.get("authenticated_execution", config.get("authenticated_execution", False))
    )
    real_order_submission = bool(
        status.get("real_order_submission", config.get("real_order_submission", False))
    )

    status_sha = str(status.get("code_sha") or status.get("runtime_sha") or "")
    exact_sha_ok = bool(
        not status
        or (
            _SHA_RE.fullmatch(status_sha)
            and (not runtime_sha or status_sha == runtime_sha)
        )
    )
    required_markets = max(0, _integer(status.get("external_fair_required_markets"), 0))
    contract = _dict(status.get("contract"))
    reference = _dict(status.get("settlement_reference"))
    oracle = _dict(status.get("oracle"))
    external = _dict(status.get("external"))
    fair = _dict(status.get("fair"))
    actions_raw = _dict(status.get("actions"))
    counterfactual_actions_raw = _dict(status.get("counterfactual_actions"))
    purposes_raw = _dict(status.get("purposes"))
    cancel = _dict(status.get("cancel"))
    economics = _dict(status.get("economics"))
    latency = _dict(status.get("latency"))
    model = _dict(status.get("model"))
    tape = _dict(status.get("tape"))
    paper_router = _dict(status.get("paper_router"))
    blockers = sorted({str(value) for value in _list(status.get("blockers")) if str(value)})

    continuity = str(oracle.get("continuity") or "CONTINUITY_UNKNOWN").upper()
    if continuity not in _CONTINUITY:
        continuity = "CONTINUITY_UNKNOWN"

    calculated_ns = _integer(fair.get("calculated_monotonic_ns"), 0)
    valid_until_ns = _integer(fair.get("valid_until_monotonic_ns"), 0)
    fair_yes = _number(fair.get("yes"), 0.5)
    fair_lower = _number(fair.get("lower"), 0.0)
    fair_upper = _number(fair.get("upper"), 1.0)
    probability_order_ok = bool(0.0 <= fair_lower <= fair_yes <= fair_upper <= 1.0)

    actions: dict[str, int] = {}
    for action in _ACTIONS:
        actions[action] = max(0, _integer(actions_raw.get(action), 0))
    purposes: dict[str, int] = {}
    for purpose in _PURPOSES:
        purposes[purpose] = max(0, _integer(purposes_raw.get(purpose), 0))

    venue_rows: list[dict[str, Any]] = []
    for row in _list(external.get("venues")):
        if not isinstance(row, dict):
            continue
        venue_rows.append({
            "venue": str(row.get("venue") or "UNKNOWN").upper(),
            "healthy": bool(row.get("healthy", False)),
            "age_ns": max(0, _integer(row.get("age_ns"), 0)),
            "price": _number(row.get("price"), 0.0),
            "microprice": _number(row.get("microprice"), 0.0),
            "spread_bps": max(0.0, _number(row.get("spread_bps"), 0.0)),
            "weight": max(0.0, _number(row.get("weight"), 0.0)),
            "basis_bps": _number(row.get("basis_bps"), 0.0),
            "actionable_lead_ms": _number(row.get("actionable_lead_ms"), 0.0),
            "economic_lead_ms": _number(row.get("economic_lead_ms"), 0.0),
            "disabled": bool(row.get("disabled", False)),
        })

    hard_reasons: list[str] = []
    if status and not exact_sha_ok:
        hard_reasons.append("EXTERNAL_FAIR_SHA_MISMATCH")
    if status and (not paper_only or authenticated_execution or real_order_submission):
        hard_reasons.append("EXTERNAL_FAIR_PAPER_CONTRACT_INVALID")
    if authority not in _SHADOW_AUTHORITIES and required_markets > 0:
        if not bool(contract.get("verified", False)):
            hard_reasons.append("CONTRACT_UNVERIFIED")
        if not bool(contract.get("rules_hash_recognized", False)):
            hard_reasons.append("RULES_HASH_UNRECOGNIZED")
        if not bool(reference.get("valid", False)):
            hard_reasons.append("SETTLEMENT_REFERENCE_INVALID")
        if not bool(oracle.get("healthy", False)):
            hard_reasons.append("ORACLE_UNHEALTHY")
        if continuity == "CONTINUITY_UNKNOWN":
            hard_reasons.append("ORACLE_CONTINUITY_UNKNOWN")
        if not bool(external.get("healthy", False)):
            hard_reasons.append("EXTERNAL_STATE_UNHEALTHY")
        if not bool(fair.get("valid", False)):
            hard_reasons.append("FAIR_INVALID")
        if not probability_order_ok:
            hard_reasons.append("FAIR_INTERVAL_INVALID")
        if calculated_ns > 0 and valid_until_ns > 0 and valid_until_ns < calculated_ns:
            hard_reasons.append("FAIR_VALIDITY_WINDOW_INVALID")
        if not bool(tape.get("evidence_valid", True)):
            hard_reasons.append("EXTERNAL_TAPE_INVALID")
        hard_reasons.extend(blockers)

    champion = _model_pointer(
        external_root / "model_registry" / "fair_value_champion.json"
    )
    challenger = _model_pointer(
        external_root / "model_registry" / "fair_value_challenger.json"
    )

    return {
        "present": bool(status),
        "status_path": str(status_path),
        "status_age_seconds": (
            max(0.0, now_s - status_path.stat().st_mtime) if status_path.exists() else None
        ),
        "execution_authority": authority,
        "shadow_zero_authority": authority in _SHADOW_AUTHORITIES,
        "paper_only": paper_only,
        "authenticated_execution": authenticated_execution,
        "real_order_submission": real_order_submission,
        "runtime_sha": status_sha,
        "exact_sha_ok": exact_sha_ok,
        "external_fair_required_markets": required_markets,
        "contract": {
            "verified": bool(contract.get("verified", False)),
            "rules_hash_recognized": bool(contract.get("rules_hash_recognized", False)),
            "rules_hash": str(contract.get("rules_hash") or ""),
            "oracle_window_seconds": max(0, _integer(contract.get("oracle_window_seconds"), 0)),
        },
        "settlement_reference": {
            "valid": bool(reference.get("valid", False)),
            "value": _number(reference.get("value"), 0.0),
            "version": max(0, _integer(reference.get("version"), 0)),
        },
        "oracle": {
            "healthy": bool(oracle.get("healthy", False)),
            "value": _number(oracle.get("value"), 0.0),
            "age_ns": max(0, _integer(oracle.get("age_ns"), 0)),
            "continuity": continuity,
            "connection_epoch": max(0, _integer(oracle.get("connection_epoch"), 0)),
            "reconnects": max(0, _integer(oracle.get("reconnects"), 0)),
            "gaps": max(0, _integer(oracle.get("gaps"), 0)),
        },
        "external": {
            "healthy": bool(external.get("healthy", False)),
            "fresh_venue_count": max(0, _integer(external.get("fresh_venue_count"), 0)),
            "dispersion_bps": max(0.0, _number(external.get("dispersion_bps"), 0.0)),
            "age_ns": max(0, _integer(external.get("age_ns"), 0)),
            "venues": venue_rows,
        },
        "fair": {
            "valid": bool(fair.get("valid", False)),
            "yes": fair_yes,
            "lower": fair_lower,
            "upper": fair_upper,
            "probability_order_ok": probability_order_ok,
            "structural": _number(fair.get("structural"), 0.5),
            "calibrated": _number(fair.get("calibrated"), 0.5),
            "micro_logit_adjustment": _number(fair.get("micro_logit_adjustment"), 0.0),
            "pm_mid": _number(fair.get("pm_mid"), 0.5),
            "tte_seconds": max(0.0, _number(fair.get("tte_seconds"), 0.0)),
            "settlement_margin": _number(fair.get("settlement_margin"), 0.0),
            "settlement_sigma": max(0.0, _number(fair.get("settlement_sigma"), 0.0)),
            "calculated_monotonic_ns": calculated_ns,
            "valid_until_monotonic_ns": valid_until_ns,
        },
        "actions": actions,
        "counterfactual_actions": {
            action: max(0, _integer(counterfactual_actions_raw.get(action), 0))
            for action in _ACTIONS
        },
        "paper_router": {
            "active_candidates": max(0, _integer(paper_router.get("active_candidates"), 0)),
            "orders_submitted": max(0, _integer(paper_router.get("orders_submitted"), 0)),
            "fills": max(0, _integer(paper_router.get("fills"), 0)),
            "counterfactual_collection_enabled": bool(
                paper_router.get("counterfactual_collection_enabled", False)
            ),
            "counterfactual_candidates": max(
                0, _integer(paper_router.get("counterfactual_candidates"), 0)
            ),
            "counterfactual_fills": max(
                0, _integer(paper_router.get("counterfactual_fills"), 0)
            ),
            "counterfactual_open_positions": max(
                0, _integer(paper_router.get("counterfactual_open_positions"), 0)
            ),
            "book_requests": max(0, _integer(paper_router.get("book_requests"), 0)),
            "book_request_failures": max(0, _integer(paper_router.get("book_request_failures"), 0)),
            "book_parse_failures": max(0, _integer(paper_router.get("book_parse_failures"), 0)),
            "rejection_reasons": {
                str(reason): max(0, _integer(count, 0))
                for reason, count in _dict(paper_router.get("rejection_reasons")).items()
            },
            "last_decision": _dict(paper_router.get("last_decision")),
        },
        "purposes": purposes,
        "cancel": {
            "fair_shock": max(0, _integer(cancel.get("fair_shock"), 0)),
            "oracle_invalid": max(0, _integer(cancel.get("oracle_invalid"), 0)),
            "external_invalid": max(0, _integer(cancel.get("external_invalid"), 0)),
            "uncertainty_spike": max(0, _integer(cancel.get("uncertainty_spike"), 0)),
            "latency_p50_ms": max(0.0, _number(cancel.get("latency_p50_ms"), 0.0)),
            "latency_p99_ms": max(0.0, _number(cancel.get("latency_p99_ms"), 0.0)),
            "stale_exposure_ms": max(0.0, _number(cancel.get("stale_exposure_ms"), 0.0)),
            "would_fill": max(0, _integer(cancel.get("would_fill"), 0)),
            "would_markout": _number(cancel.get("would_markout"), 0.0),
        },
        "economics": {
            "maker_robust_ev": _number(economics.get("maker_robust_ev"), 0.0),
            "taker_robust_ev": _number(economics.get("taker_robust_ev"), 0.0),
            "realized_pnl": _number(economics.get("realized_pnl"), 0.0),
            "counterfactual_realized_pnl": _number(
                economics.get("counterfactual_realized_pnl"), 0.0
            ),
            "counterfactual_equity": _number(economics.get("counterfactual_equity"), 0.0),
            "terminal_pnl": _number(economics.get("terminal_pnl"), 0.0),
            "taker_fees": max(0.0, _number(economics.get("taker_fees"), 0.0)),
            "maker_fees": max(0.0, _number(economics.get("maker_fees"), 0.0)),
            "maker_rebates": _number(economics.get("maker_rebates"), 0.0),
            "liquidity_rewards": _number(economics.get("liquidity_rewards"), 0.0),
            "slippage": _number(economics.get("slippage"), 0.0),
            "markout": _number(economics.get("markout"), 0.0),
        },
        "model": {
            "champion": champion,
            "challenger": challenger,
            "mature": bool(model.get("mature", False)),
            "log_loss": _number(model.get("log_loss"), 0.0),
            "brier": _number(model.get("brier"), 0.0),
            "ece": _number(model.get("ece"), 0.0),
            "coverage": _number(model.get("coverage"), 0.0),
            "drift_score": max(0.0, _number(model.get("drift_score"), 0.0)),
            "training_end_ns": max(0, _integer(model.get("training_end_ns"), 0)),
        },
        "latency": {
            str(name): {
                str(q): max(0.0, _number(value, 0.0))
                for q, value in _dict(values).items()
                if str(q) in {"p50", "p90", "p95", "p99", "p99_9", "max"}
            }
            for name, values in latency.items()
            if isinstance(values, dict)
        },
        "tape": {
            "evidence_valid": bool(tape.get("evidence_valid", True)),
            "accepted": max(0, _integer(tape.get("accepted"), 0)),
            "written": max(0, _integer(tape.get("written"), 0)),
            "dropped": max(0, _integer(tape.get("dropped"), 0)),
        },
        "blockers": blockers,
        "hard_reasons": sorted(set(hard_reasons)),
        "healthy": not hard_reasons,
    }
