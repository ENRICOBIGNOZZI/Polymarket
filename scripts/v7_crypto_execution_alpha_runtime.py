#!/usr/bin/env python3
"""Zero-authority runtime bridge for unified CRYPTO_SETTLEMENT_ENGINE execution alpha.

Consumes the exact causal CLOB snapshot already fetched by the canonical PAPER
router. It never calls Polymarket, never submits an order and never writes the
canonical ledger. Mature positive MAKE proposals may enter the existing global
coordinator inbox; TAKE remains owned by the existing arrival-revalidated router.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

try:
    from v7_crypto_execution_alpha import (
        CancelEvidence, MakerEvidence, MarketState, OutcomeBook, TakerEvidence,
        aggregate_attribution, evaluate_market, market_selection_value,
    )
    from v7_opportunity import OpportunityEnvelope, OpportunityError
except ModuleNotFoundError:  # package import under unittest
    from scripts.v7_crypto_execution_alpha import (
        CancelEvidence, MakerEvidence, MarketState, OutcomeBook, TakerEvidence,
        aggregate_attribution, evaluate_market, market_selection_value,
    )
    from scripts.v7_opportunity import OpportunityEnvelope, OpportunityError


STATUS_SCHEMA = "polymarket_v7_crypto_execution_alpha_runtime_v1"
CANCEL_REPORT_SCHEMA = "polymarket_v7_btc_m5_external_cancel_forward_report_v3"
CANCEL_SIGNAL_SCHEMA = "polymarket_v7_btc_m5_external_cancel_live_signal_v1"
CANCEL_RULE_SHA = "9e8c7e6a1d7e4a87cd9977396bcbbb228f96b4e35e4a34e84e1514e9e9630254"


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float:
    if trials <= 0 or successes < 0 or successes > trials:
        return 0.0
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = p + z * z / (2.0 * trials)
    radius = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * trials)) / trials)
    return max(0.0, min(1.0, (centre - radius) / denom))


def tte_execution_risk(policy: dict[str, Any], tte: float) -> float:
    # Consume the exact checked-in taker policy used by the canonical PAPER router.
    # The `execution` fallback exists only for small isolated test fixtures.
    execution = policy.get("taker") if isinstance(policy.get("taker"), dict) else (
        policy.get("execution") if isinstance(policy.get("execution"), dict) else {}
    )
    buckets = execution.get("tte_bucket_policy") if isinstance(execution.get("tte_bucket_policy"), list) else []
    for row in buckets:
        if not isinstance(row, dict):
            continue
        minimum = finite(row.get("minimum_seconds"), -1.0)
        maximum = finite(row.get("maximum_seconds"), -1.0)
        if minimum <= tte <= maximum:
            return max(0.0, finite(row.get("execution_risk_per_share"), 0.0))
    return max(0.0, finite(execution.get("base_execution_risk_per_share"), 0.0))


def cancel_evidence(report: dict[str, Any], signal: dict[str, Any]) -> tuple[CancelEvidence, list[str]]:
    reasons: list[str] = []
    bootstrap = report.get("bootstrap95_market_cluster_500ms_improvement")
    lower_markout = 0.0
    if isinstance(bootstrap, list) and len(bootstrap) == 2:
        lower_markout = max(0.0, finite(bootstrap[0], 0.0))
    markets = int(report.get("market_count") or 0)
    avoidable = int(report.get("avoidable_fill_events") or 0)
    episodes = int(report.get("episode_count") or 0)
    stress = finite(report.get("stress_3x_queue_200ms_cancel_improvement_per_share"), 0.0)
    mature = (
        report.get("schema") == CANCEL_REPORT_SCHEMA
        and report.get("state") == "PASS"
        and report.get("rule_sha256") == CANCEL_RULE_SHA
        and markets >= 30 and avoidable >= 50 and episodes > 0
        and lower_markout > 0.0 and stress > 0.0
    )
    if not mature:
        reasons.append("EXTERNAL_CANCEL_FORWARD_EVIDENCE_PENDING")
    direction = str(signal.get("direction") or "NONE")
    stale_sides = signal.get("stale_sides") if isinstance(signal.get("stale_sides"), list) else []
    expected_stale = (
        ["YES_SELL", "NO_BUY"] if direction == "UP"
        else ["YES_BUY", "NO_SELL"] if direction == "DOWN"
        else []
    )
    signal_semantics_valid = (
        signal.get("schema") == CANCEL_SIGNAL_SCHEMA
        and signal.get("rule_sha256") == CANCEL_RULE_SHA
        and signal.get("paper_only") is True
        and signal.get("authenticated_execution") is False
        and signal.get("real_order_submission") is False
        and signal.get("execution_authority") == "SIGNAL_ONLY_ZERO_AUTHORITY"
        and signal.get("receive_time_causal") is True
        and signal.get("shock_source") == "BINANCE_SPOT_TRADES"
        and int(signal.get("shock_window_ms") or 0) == 100
        and abs(finite(signal.get("minimum_absolute_log_return_bp"), -1.0) - 0.3) <= 1e-12
        and signal.get("confirmation_source") == "COINBASE_SPOT_TOP_OF_BOOK"
        and signal.get("confirmation") == "NON_OPPOSING"
        and int(signal.get("trigger_cooldown_ms") or 0) == 250
        and int(signal.get("evaluation_tick_ms") or 0) == 25
        and signal.get("history_valid") is True
        and signal.get("threshold_crossed") is True
        and signal.get("confirmation_non_opposing") is True
        and signal.get("cooldown_blocked") is False
        and direction in {"UP", "DOWN"}
        and stale_sides == expected_stale
        and int(signal.get("trigger_monotonic_ns") or 0) > 0
        and int(signal.get("evaluated_monotonic_ns") or 0) >= int(signal.get("trigger_monotonic_ns") or 0)
    )
    signal_valid = mature and signal_semantics_valid and signal.get("active") is True
    if mature and not signal_valid:
        reasons.append("CANONICAL_EXTERNAL_CANCEL_SIGNAL_INACTIVE_OR_MISSING")
    probability_lower = wilson_lower(avoidable, episodes) if mature else 0.0
    return CancelEvidence(
        signal_active=signal_valid,
        mandatory_risk_cancel=signal_valid and signal.get("mandatory_risk_cancel") is True,
        quote_size=max(0.0, finite(signal.get("active_quote_size_shares"), 0.0)) if signal_valid else 0.0,
        avoidable_fill_probability_lower=probability_lower,
        avoided_adverse_loss_lower_per_share=lower_markout if mature else 0.0,
        cancel_cost=max(0.0, finite(signal.get("cancel_cost"), 0.0)) if signal_valid else 0.0,
        mature=mature,
    ), reasons


def outcome_book(raw: dict[str, Any], outcome: str) -> OutcomeBook:
    return OutcomeBook(
        outcome=outcome,
        token_id=str(raw.get("token_id") or ""),
        bid=finite(raw.get("best_bid"), -1.0),
        ask=finite(raw.get("best_ask"), -1.0),
        bid_size=max(0.0, finite(raw.get("best_bid_size"), 0.0)),
        ask_size=max(0.0, finite(raw.get("best_ask_size"), 0.0)),
    )


def build_state(
    root: Path, *, comparison_size_shares: float,
    external_policy: dict[str, Any], cancel_report: dict[str, Any], cancel_signal: dict[str, Any],
) -> tuple[MarketState | None, list[str], dict[str, Any]]:
    blockers: list[str] = []
    runtime = load(root / "control" / "runtime_status.json")
    fair_status = load(root / "external_fair" / "status.json")
    router = load(root / "external_fair" / "paper_router_status.json")
    engine = load(root / "control" / "crypto_settlement_engine_snapshot.json")
    maker_model = load(root / "micro_maker" / "execution_model.json")
    if (
        runtime.get("schema") != "polymarket_v7_runtime_status_v3"
        or runtime.get("paper_only") is not True
        or runtime.get("authenticated_execution") is not False
        or runtime.get("real_order_submission") is not False
    ):
        blockers.append("RUNTIME_SAFETY_IDENTITY_INVALID")
    if (
        fair_status.get("paper_only") is not True
        or fair_status.get("authenticated_execution") is not False
        or fair_status.get("real_order_submission") is not False
    ):
        blockers.append("EXTERNAL_FAIR_SAFETY_IDENTITY_INVALID")
    fair = fair_status.get("fair") if isinstance(fair_status.get("fair"), dict) else {}
    market = fair_status.get("market") if isinstance(fair_status.get("market"), dict) else {}
    contract = fair_status.get("contract") if isinstance(fair_status.get("contract"), dict) else {}
    reference = fair_status.get("settlement_reference") if isinstance(fair_status.get("settlement_reference"), dict) else {}
    live = router.get("live_market") if isinstance(router.get("live_market"), dict) else {}
    books = live.get("execution_alpha_books") if isinstance(live.get("execution_alpha_books"), dict) else {}
    if live.get("valid") is not True or set(books) != {"YES", "NO"}:
        blockers.append("CAUSAL_COMPLEMENT_BOOK_SNAPSHOT_MISSING")
    if fair.get("valid") is not True:
        blockers.append("SETTLEMENT_FAIR_INVALID")
    if contract.get("verified") is not True or reference.get("valid") is not True:
        blockers.append("SETTLEMENT_BINDING_UNVERIFIED")
    if blockers:
        return None, blockers, {"runtime": runtime, "fair": fair_status, "router": router}

    maker_snapshot = engine.get("maker_execution") if isinstance(engine.get("maker_execution"), dict) else {}
    global_group = ((maker_model.get("groups") or {}).get("GLOBAL")
                    if isinstance(maker_model.get("groups"), dict) else {}) or {}
    maker_mature = (
        maker_snapshot.get("valid") is True
        and maker_snapshot.get("execution_model_mature") is True
        and maker_snapshot.get("markout_model_mature") is True
        and maker_model.get("economically_mature") is True
    )
    maker = MakerEvidence(
        reach_probability_lower=max(0.0, finite(maker_snapshot.get("reach_probability_lower"), 0.0)) if maker_mature else 0.0,
        fill_given_reach_probability_lower=max(0.0, finite(maker_snapshot.get("fill_given_reach_probability_lower"), 0.0)) if maker_mature else 0.0,
        fill_probability_point=max(0.0, min(1.0, finite(global_group.get("fill_probability"), 0.0))),
        adverse_markout_upper_per_share=max(
            0.0,
            finite(maker_snapshot.get("adverse_markout_upper_per_share"),
                   finite(global_group.get("adverse_markout_per_share"), 0.002)),
        ),
        toxic_fill_probability_upper=1.0 if not maker_mature else 0.5,
        rebate_per_share=0.0,
        rebate_authoritative=False,
        cancel_latency_risk_per_share=0.0,
        inventory_cost_per_share=0.0,
        cancel_cost_per_quote=0.0,
        capital_cost_per_quote=0.0,
        mature=maker_mature,
    )

    account = router.get("paper_exploration_account") if isinstance(router.get("paper_exploration_account"), dict) else {}
    orders = int(account.get("orders_submitted") or 0)
    fills = int(account.get("fills") or 0)
    taker_mature = (
        account.get("complete") is True and orders >= 50 and fills >= 50
        and fills <= orders and int(account.get("terminal_nonfills") or 0) == orders - fills
    )
    tte = max(0.0, finite(fair.get("tte_seconds"), 0.0))
    taker = TakerEvidence(
        fill_probability_lower=wilson_lower(fills, orders) if taker_mature else 0.0,
        slippage_per_share=0.0,
        latency_risk_per_share=tte_execution_risk(external_policy, tte),
        unwind_loss_per_share=0.0,
        capital_cost_per_trade=0.0,
        mature=taker_mature,
    )
    cancel, cancel_reasons = cancel_evidence(cancel_report, cancel_signal)
    blockers.extend(cancel_reasons)
    state = MarketState(
        market_id=str(market.get("market_id") or live.get("market_id") or ""),
        event_id=str(market.get("event_id") or ""),
        asset="BTC", horizon="M5",
        fair_lower_yes=finite(fair.get("lower"), 0.0),
        fair_point_yes=finite(fair.get("yes"), 0.5),
        fair_upper_yes=finite(fair.get("upper"), 1.0),
        yes=outcome_book(books["YES"], "YES"),
        no=outcome_book(books["NO"], "NO"),
        fee_schedule=market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {},
        maker=maker, taker=taker, cancel=cancel,
        target_size=max(1e-9, comparison_size_shares), tte_seconds=tte,
        settlement_verified=contract.get("verified") is True and reference.get("valid") is True,
        fair_mature=(fair_status.get("model") or {}).get("mature") is True,
        source_snapshot_identity=str(live.get("snapshot_id") or ""),
    )
    return state, blockers, {"runtime": runtime, "fair": fair_status, "router": router, "engine": engine}


def make_envelope(state: MarketState, report: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    selected = report.get("selected_action") if isinstance(report.get("selected_action"), dict) else {}
    if (
        selected.get("action") != "MAKE"
        or selected.get("evidence_mature") is not True
        or finite(selected.get("conservative_expected_wealth_change"), 0.0) <= 0.0
    ):
        return None
    runtime = context["runtime"]
    engine = context["engine"]
    crypto = engine.get("crypto_context") if isinstance(engine.get("crypto_context"), dict) else {}
    book = state.yes if selected.get("outcome") == "YES" else state.no
    decision_ns = time.time_ns()
    source_books = ((context["router"].get("live_market") or {}).get("execution_alpha_books") or {})
    source_ns = sorted({
        int((row.get("exchange_ts_ms") or 0) * 1_000_000)
        for row in source_books.values() if isinstance(row, dict) and int(row.get("exchange_ts_ms") or 0) > 0
    } | {
        int((row.get("receive_ts_ms") or 0) * 1_000_000)
        for row in source_books.values() if isinstance(row, dict) and int(row.get("receive_ts_ms") or 0) > 0
    })
    if not source_ns or max(source_ns) > decision_ns:
        return None
    attribution = selected.get("attribution") if isinstance(selected.get("attribution"), dict) else {}
    replay_key = (
        f"crypto-execution-alpha:MAKE:{state.market_id}:{selected.get('outcome')}:"
        f"{state.source_snapshot_identity}"
    )
    envelope = {
        "schema": "polymarket_v7_opportunity_envelope_v1", "version": 1,
        "model_sha": str(runtime.get("model_sha") or ""),
        "config_hash": str(runtime.get("config_hash") or ""),
        "policy_hash": str(runtime.get("policy_hash") or ""),
        "run_id": str(runtime.get("run_id") or ""),
        "source_snapshot_identity": state.source_snapshot_identity,
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "component_provenance": ["crypto_settlement_fair", "professional_maker"],
        "market_id": state.market_id, "event_id": state.event_id,
        "contract_id": book.token_id,
        "mapping_identity": str(crypto.get("settlement_semantic_hash") or ""),
        "crypto_context": {
            "asset": str(crypto.get("asset") or "BTC"),
            "horizon": str(crypto.get("horizon") or "M5"),
            "contract_family": str(crypto.get("contract_family") or "BTC_USD_UPDOWN_5M"),
            "settlement_semantic_hash": str(crypto.get("settlement_semantic_hash") or ""),
            "authority": "PAPER_EXPLORATION", "research_only": False,
        },
        "action": "MAKE", "side": book.outcome,
        "decision_receive_timestamp_ns": decision_ns,
        "source_event_timestamps_ns": source_ns,
        "fair_value": {
            "lower": state.fair_lower_yes if book.outcome == "YES" else 1.0 - state.fair_upper_yes,
            "point": state.fair_point_yes if book.outcome == "YES" else 1.0 - state.fair_point_yes,
            "upper": state.fair_upper_yes if book.outcome == "YES" else 1.0 - state.fair_lower_yes,
        },
        "conservative_expected_wealth_change": float(selected["conservative_expected_wealth_change"]),
        "execution_alpha": {
            "schema": "polymarket_v7_execution_alpha_packet_v1",
            "action": "MAKE",
            "evidence_status": "MATURE",
            "fill_probability": {
                "lower": float(selected["expected_fill_probability"]),
                "point": max(
                    float(selected["expected_fill_probability"]),
                    min(1.0, float(state.maker.fill_probability_point)),
                ),
                "upper": max(
                    float(selected["expected_fill_probability"]),
                    min(1.0, float(state.maker.fill_probability_point)),
                ),
            },
            "queue_ahead_shares": max(0.0, float(book.bid_size)),
            "action_ev": {
                "conservative": float(selected["conservative_expected_wealth_change"]),
                "point": float(selected["point_expected_wealth_change"]),
            },
            "attribution": {name: float(attribution.get(name, 0.0)) for name in (
                "settlement_alpha", "spread_capture", "rebate", "fees", "slippage",
                "adverse_selection", "latency", "inventory", "unwind", "cancel", "capital",
            )},
        },
        "cost_vector": {
            "fee": max(0.0, -finite(attribution.get("fees"), 0.0)),
            "slippage": max(0.0, -finite(attribution.get("slippage"), 0.0)),
            "unwind_loss": max(0.0, -finite(attribution.get("unwind"), 0.0)),
            "capital_cost": max(0.0, -finite(attribution.get("capital"), 0.0)),
            "latency_cost": max(0.0, -finite(attribution.get("latency"), 0.0)),
            "adverse_markout": max(0.0, -finite(attribution.get("adverse_selection"), 0.0)),
            "rebate": max(0.0, finite(attribution.get("rebate"), 0.0)),
        },
        "cost_authority": {
            "fee": "CONSERVATIVE_BOUND", "slippage": "CONSERVATIVE_BOUND",
            "unwind_loss": "CONSERVATIVE_BOUND", "capital_cost": "CONSERVATIVE_BOUND",
            "latency_cost": "CONSERVATIVE_BOUND", "adverse_markout": "CONSERVATIVE_BOUND",
            "rebate": "AUTHORITATIVE" if finite(attribution.get("rebate"), 0.0) > 0.0 else "CONSERVATIVE_ZERO",
        },
        "uncertainty": {"lower_bound": -1.0, "upper_bound": 1.0, "status": "MATURE"},
        "calibration_status": "MATURE",
        "latency": {
            "profile_id": str((engine.get("latency") or {}).get("profile_version") or "maker-evidence"),
            "profile_valid": (engine.get("latency") or {}).get("valid") is True,
            "economic_percentile": "p99", "arrival_ns": max(
                1, int(max(0.0, finite((engine.get("latency") or {}).get("maker_place_p99_seconds"), 0.0)) * 1_000_000_000)
            ),
        },
        "capacity": {"executable_size": float(selected["size"]), "depth_provenance": state.source_snapshot_identity},
        "execution_plan": {
            "atomic_unit_id": replay_key, "execution_style": "SINGLE_LEG",
            "legs": [{
                "leg_id": f"maker-{book.token_id}", "market_id": state.market_id,
                "contract_id": book.token_id, "token_id": book.token_id, "side": "BUY",
                "target_quantity": float(selected["size"]), "limit_price": float(selected["price"]),
                "fee_authority": "CONSERVATIVE_BOUND",
            }],
            "partial_fill_plan": "CANCEL_REMAINDER", "timeout_ms": 1000, "unwind_plan": "NONE",
        },
        "inventory_delta": float(selected["size"]),
        "portfolio_exposure_delta": float(selected["capital_at_risk"]),
        "settlement": {
            "definition": "registry-verified crypto settlement binding",
            "source": "REGISTRY_VERIFIED_CHAINLINK_TWAP_60S", "verified": True,
        },
        "eligible": True,
        "reasons": ["UNIFIED_EXECUTION_ALPHA_MAKE", "PAPER_EXPLORATION_ONLY", "NO_AUTOMATIC_PROMOTION"],
        "deterministic_replay_key": replay_key,
        "expires_at_ns": decision_ns + 500_000_000,
    }
    try:
        OpportunityEnvelope.parse(envelope)
    except (OpportunityError, ValueError):
        return None
    return envelope


def bucket(value: float, cuts: tuple[float, ...], names: tuple[str, ...]) -> str:
    for cut, name in zip(cuts, names):
        if value <= cut:
            return name
    return names[-1]


def probe_recommendation(state: MarketState, report: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    if report.get("maker_information_probe_recommended") is not True:
        return None
    point = (
        report.get("maker_information_probe_candidate")
        if isinstance(report.get("maker_information_probe_candidate"), dict) else {}
    )
    if point.get("action") != "MAKE":
        return None
    fair_status = context["fair"]
    external = fair_status.get("external") if isinstance(fair_status.get("external"), dict) else {}
    book = state.yes if point.get("outcome") == "YES" else state.no
    return {
        "schema": "polymarket_v7_crypto_execution_alpha_maker_probe_recommendation_v1",
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "promotion_credit": False, "market_id": state.market_id, "event_id": state.event_id,
        "source_snapshot_identity": state.source_snapshot_identity,
        "candidate": point,
        "context": {
            "queue_bucket": bucket(book.bid_size, (10.0, 50.0, 200.0), ("Q0_10", "Q10_50", "Q50_200", "Q200_PLUS")),
            "spread_bucket": bucket(book.spread, (0.005, 0.02, 0.05), ("S_TIGHT", "S_MEDIUM", "S_WIDE", "S_VERY_WIDE")),
            "tte_bucket": bucket(state.tte_seconds, (15.0, 60.0, 180.0), ("T0_15", "T15_60", "T60_180", "T180_PLUS")),
            "volatility_bucket": bucket(abs(finite(external.get("realized_vol_fast"), 0.0)), (1e-5, 5e-5, 2e-4), ("V_LOW", "V_MEDIUM", "V_HIGH", "V_EXTREME")),
            "activity_bucket": "A_CRYPTO_M5_LIVE",
            "quote_lifetime_bucket": "L_500MS_CONTROL",
        },
        "recommendation": "ROUTE_TO_EXISTING_PRE_REGISTERED_MAKER_PROBE_DESIGN",
        "reason": "POINT_MAKE_VALUE_POSITIVE_BUT_CONSERVATIVE_FILL_EVIDENCE_IMMATURE",
    }


def process_cut(
    root: Path, *, external_policy_path: Path, cancel_report_path: Path | None,
    cancel_signal_path: Path | None, comparison_size_shares: float,
) -> dict[str, Any]:
    policy = load(external_policy_path)
    report_path = cancel_report_path or root / "control" / "btc_m5_external_cancel_forward_report.json"
    signal_path = cancel_signal_path or root / "external_fair" / "external_cancel_signal.json"
    cancel_report = load(report_path)
    cancel_signal = load(signal_path)
    state, blockers, context = build_state(
        root, comparison_size_shares=comparison_size_shares,
        external_policy=policy, cancel_report=cancel_report, cancel_signal=cancel_signal,
    )
    output_root = root / "crypto_execution_alpha"
    if state is None:
        status = {
            "schema": STATUS_SCHEMA, "timestamp_ns": time.time_ns(),
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "state": "FAIL_CLOSED",
            "blockers": blockers, "report": None,
        }
        atomic_json(output_root / "status.json", status)
        return status
    report = evaluate_market(state)
    report["market_selection_value"] = market_selection_value(report)
    report["attribution_total"] = aggregate_attribution([report])
    envelope = make_envelope(state, report, context)
    runtime_state = load(output_root / "state.json")
    published = False
    if envelope is not None and runtime_state.get("last_published_replay_key") != envelope["deterministic_replay_key"]:
        target = root / "opportunities" / "inbox" / (
            f"{time.time_ns() // 1_000_000}.crypto-execution-alpha."
            f"{hashlib.sha256(envelope['deterministic_replay_key'].encode()).hexdigest()[:16]}.json"
        )
        atomic_json(target, envelope)
        runtime_state["last_published_replay_key"] = envelope["deterministic_replay_key"]
        published = True
    recommendation = probe_recommendation(state, report, context)
    if recommendation is not None and runtime_state.get("last_probe_snapshot") != state.source_snapshot_identity:
        append_jsonl(output_root / "maker_probe_recommendations.jsonl", recommendation)
        runtime_state["last_probe_snapshot"] = state.source_snapshot_identity
    atomic_json(output_root / "state.json", runtime_state)
    evidence = {
        "schema": "polymarket_v7_crypto_execution_alpha_decision_v1",
        "timestamp_ns": time.time_ns(), "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "report": report, "blockers": blockers,
        "make_opportunity_published": published,
        "take_proposal_owner": "EXISTING_ARRIVAL_REVALIDATED_EXTERNAL_FAIR_ROUTER",
        "maker_execution_owner": "EXISTING_PROFESSIONAL_MAKER_RUNTIME",
        "cancel_activation_policy": "FROZEN_FORWARD_PASS_PLUS_CANONICAL_LIVE_SIGNAL_REQUIRED",
    }
    append_jsonl(output_root / "decisions.jsonl", evidence)
    status = {
        "schema": STATUS_SCHEMA, "timestamp_ns": evidence["timestamp_ns"],
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "state": "RUNNING",
        "blockers": blockers, "report": report,
        "make_opportunity_published": published,
        "maker_probe_recommended": recommendation is not None,
    }
    atomic_json(output_root / "status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--external-policy", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--cancel-report", type=Path)
    parser.add_argument("--cancel-signal", type=Path)
    parser.add_argument("--comparison-size-shares", type=float, default=5.0)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    if not args.loop:
        print(json.dumps(process_cut(
            args.run_root, external_policy_path=args.external_policy,
            cancel_report_path=args.cancel_report, cancel_signal_path=args.cancel_signal,
            comparison_size_shares=args.comparison_size_shares,
        ), sort_keys=True))
        return 0
    while True:
        try:
            process_cut(
                args.run_root, external_policy_path=args.external_policy,
                cancel_report_path=args.cancel_report, cancel_signal_path=args.cancel_signal,
                comparison_size_shares=args.comparison_size_shares,
            )
        except Exception as exc:
            atomic_json(args.run_root / "crypto_execution_alpha" / "status.json", {
                "schema": STATUS_SCHEMA, "timestamp_ns": time.time_ns(), "paper_only": True,
                "authenticated_execution": False, "real_order_submission": False,
                "state": "FAIL_CLOSED", "blockers": [f"UNHANDLED:{type(exc).__name__}:{exc}"],
                "report": None,
            })
        time.sleep(max(0.05, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
