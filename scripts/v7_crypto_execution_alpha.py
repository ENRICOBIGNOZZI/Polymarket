#!/usr/bin/env python3
"""Unified PAPER-only execution-alpha and crypto market-selection layer.

This module does not own OMS, capital, inventory or execution authority.  It
compares already-causal CRYPTO_SETTLEMENT_ENGINE opportunity envelopes on one
objective: conservative expected change in account wealth.  The same layer
also ranks market windows so scarce quote/capital budget is concentrated on the
best observable opportunities instead of spread uniformly across the universe.

Rich execution-alpha packets are optional during migration.  When present they
carry the exact feature cut used to estimate fillability, fill-conditioned
markout and toxic-fill risk.  When absent, ranking uses only fields already
validated by the canonical opportunity envelope and explicitly reports missing
components; no missing signal is silently zero-imputed into execution authority.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CONFIG_SCHEMA = "polymarket_v7_crypto_execution_alpha_config_v1"
PACKET_SCHEMA = "polymarket_v7_execution_alpha_packet_v1"
NEW_RISK_ACTIONS = {"MAKE", "TAKE"}
RISK_ACTIONS = {"CANCEL", "WITHDRAW"}
FEATURE_NAMES = (
    "queue_ahead",
    "spread",
    "book_imbalance",
    "recent_aggressive_flow",
    "quote_lifetime_ms",
    "tte_seconds",
    "binance_shock_bp",
    "coinbase_shock_bp",
    "bybit_shock_bp",
    "cross_venue_disagreement_bp",
    "oracle_distance_bp",
    "volatility_bp",
    "latency_ms",
)
SELECTION_COMPONENTS = (
    "predictability",
    "mispricing",
    "spread_capture",
    "depth",
    "flow",
    "latency",
    "tte",
    "fillability",
    "adverse_selection",
)


class ExecutionAlphaError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ExecutionAlphaError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ExecutionAlphaError(name) from exc
    if not math.isfinite(number):
        raise ExecutionAlphaError(name)
    return number


def _optional_finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def load_config(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_config(value)
    return value


def validate_config(value: dict[str, Any]) -> None:
    if (
        not isinstance(value, dict)
        or value.get("schema") != CONFIG_SCHEMA
        or value.get("version") != 1
        or value.get("paper_only") is not True
        or value.get("authenticated_execution") is not False
        or value.get("real_order_submission") is not False
        or value.get("real_capital_at_risk") is not False
        or value.get("automatic_promotion") is not False
        or value.get("decision_owner") != "CRYPTO_SETTLEMENT_ENGINE"
        or value.get("objective") != "MAX_CONSERVATIVE_EXPECTED_CHANGE_IN_ACCOUNT_WEALTH"
        or value.get("risk_actions_preempt_alpha") is not True
    ):
        raise ExecutionAlphaError("config_identity_or_safety")
    if set(value.get("action_space") or []) != {"MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"}:
        raise ExecutionAlphaError("config_action_space")
    execution = value.get("execution_alpha")
    if not isinstance(execution, dict):
        raise ExecutionAlphaError("config_execution_alpha")
    if tuple(execution.get("required_feature_groups") or []) != FEATURE_NAMES:
        raise ExecutionAlphaError("config_feature_contract")
    selection = value.get("market_selection")
    if not isinstance(selection, dict) or selection.get("enabled") is not True:
        raise ExecutionAlphaError("config_market_selection")
    fraction = _finite(selection.get("top_fraction"), "config_top_fraction")
    if not 0.0 < fraction <= 1.0:
        raise ExecutionAlphaError("config_top_fraction")
    if int(selection.get("minimum_candidate_markets") or 0) < 1 or int(selection.get("minimum_markets_retained") or 0) < 1:
        raise ExecutionAlphaError("config_market_selection_counts")
    weights = selection.get("score_weights")
    if not isinstance(weights, dict) or set(weights) != set(SELECTION_COMPONENTS):
        raise ExecutionAlphaError("config_market_selection_weights")
    total = 0.0
    for name in SELECTION_COMPONENTS:
        weight = _finite(weights[name], f"config_weight:{name}")
        if weight < 0.0:
            raise ExecutionAlphaError(f"config_weight_negative:{name}")
        total += weight
    if abs(total - 1.0) > 1e-9:
        raise ExecutionAlphaError("config_market_selection_weights_sum")
    exploration = value.get("paper_exploration")
    if (
        not isinstance(exploration, dict)
        or exploration.get("enabled") is not True
        or exploration.get("authority") != "PAPER_EXPLORATION"
        or exploration.get("promotion_credit") is not False
        or exploration.get("assignment_before_outcome_required") is not True
        or not isinstance(exploration.get("strata"), list)
        or not exploration["strata"]
    ):
        raise ExecutionAlphaError("config_paper_exploration")
    attribution = value.get("pnl_attribution")
    if not isinstance(attribution, dict) or attribution.get("required") is not True:
        raise ExecutionAlphaError("config_pnl_attribution")


def validate_probability_band(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, dict) or set(value) != {"lower", "point", "upper"}:
        raise ExecutionAlphaError(f"{name}:fields")
    lower = _finite(value["lower"], f"{name}:lower")
    point = _finite(value["point"], f"{name}:point")
    upper = _finite(value["upper"], f"{name}:upper")
    if not 0.0 <= lower <= point <= upper <= 1.0:
        raise ExecutionAlphaError(f"{name}:bounds")
    return lower, point, upper


def validate_execution_alpha_packet(packet: Any, *, decision_ns: int, action: str) -> dict[str, Any]:
    if not isinstance(packet, dict) or set(packet) != {
        "schema", "model_id", "model_hash", "feature_receive_timestamp_ns",
        "features", "fill_probability", "markout_per_share",
        "toxic_fill_probability", "action_ev", "selected_action", "evidence_status",
    }:
        raise ExecutionAlphaError("packet_fields")
    if packet.get("schema") != PACKET_SCHEMA:
        raise ExecutionAlphaError("packet_schema")
    model_id = packet.get("model_id")
    model_hash = str(packet.get("model_hash") or "")
    if not isinstance(model_id, str) or not model_id or len(model_hash) != 64 or any(ch not in "0123456789abcdef" for ch in model_hash):
        raise ExecutionAlphaError("packet_model_identity")
    receive_ns = packet.get("feature_receive_timestamp_ns")
    if isinstance(receive_ns, bool) or not isinstance(receive_ns, int) or receive_ns <= 0 or receive_ns > decision_ns:
        raise ExecutionAlphaError("packet_feature_clock")
    features = packet.get("features")
    if not isinstance(features, dict) or set(features) != set(FEATURE_NAMES):
        raise ExecutionAlphaError("packet_features")
    for name in FEATURE_NAMES:
        raw = features[name]
        if raw is not None:
            _finite(raw, f"packet_feature:{name}")
    validate_probability_band(packet.get("fill_probability"), "packet_fill_probability")
    validate_probability_band(packet.get("toxic_fill_probability"), "packet_toxic_fill_probability")
    markout = packet.get("markout_per_share")
    if not isinstance(markout, dict) or set(markout) != {"lower", "point", "upper"}:
        raise ExecutionAlphaError("packet_markout_fields")
    lower = _finite(markout["lower"], "packet_markout_lower")
    point = _finite(markout["point"], "packet_markout_point")
    upper = _finite(markout["upper"], "packet_markout_upper")
    if lower > point or point > upper:
        raise ExecutionAlphaError("packet_markout_bounds")
    action_ev = packet.get("action_ev")
    if not isinstance(action_ev, dict) or set(action_ev) != {"MAKE", "TAKE", "CANCEL", "NOTHING"}:
        raise ExecutionAlphaError("packet_action_ev")
    for name, row in action_ev.items():
        if not isinstance(row, dict) or set(row) != {"point", "conservative"}:
            raise ExecutionAlphaError(f"packet_action_ev:{name}:fields")
        point_ev = _finite(row["point"], f"packet_action_ev:{name}:point")
        conservative = _finite(row["conservative"], f"packet_action_ev:{name}:conservative")
        if conservative > point_ev + 1e-12:
            raise ExecutionAlphaError(f"packet_action_ev:{name}:ordering")
    if packet.get("selected_action") != action:
        raise ExecutionAlphaError("packet_selected_action_mismatch")
    if packet.get("evidence_status") not in {"MATURE", "IMMATURE", "MISSING"}:
        raise ExecutionAlphaError("packet_evidence_status")
    return json.loads(json.dumps(packet, sort_keys=True))


def _first_leg_price(raw: dict[str, Any]) -> float:
    plan = raw.get("execution_plan") if isinstance(raw.get("execution_plan"), dict) else {}
    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
    if not legs or not isinstance(legs[0], dict):
        return 0.5
    return _clamp(_optional_finite(legs[0].get("limit_price")) or 0.5)


def _feature(raw: dict[str, Any], name: str) -> float | None:
    packet = raw.get("execution_alpha")
    if not isinstance(packet, dict):
        return None
    features = packet.get("features")
    if not isinstance(features, dict):
        return None
    return _optional_finite(features.get(name))


def _probability_lower(raw: dict[str, Any], field: str) -> float | None:
    packet = raw.get("execution_alpha")
    if not isinstance(packet, dict):
        return None
    band = packet.get(field)
    if not isinstance(band, dict):
        return None
    value = _optional_finite(band.get("lower"))
    return _clamp(value) if value is not None else None


def selection_components(raw: dict[str, Any]) -> dict[str, Any]:
    fair = raw.get("fair_value") if isinstance(raw.get("fair_value"), dict) else {}
    lower = _clamp(_optional_finite(fair.get("lower")) or 0.0)
    point = _clamp(_optional_finite(fair.get("point")) or 0.5)
    upper = _clamp(_optional_finite(fair.get("upper")) or 1.0)
    fair_width = max(0.0, upper - lower)
    predictability = _clamp(1.0 - fair_width)

    price = _first_leg_price(raw)
    mispricing = _clamp(abs(point - price) / 0.10)

    spread = _feature(raw, "spread")
    spread_capture = _clamp((spread or 0.0) / 0.05) if spread is not None else 0.0

    capacity = raw.get("capacity") if isinstance(raw.get("capacity"), dict) else {}
    size = max(0.0, _optional_finite(capacity.get("executable_size")) or 0.0)
    depth = _clamp(math.log1p(size) / math.log(101.0))

    aggressive_flow = _feature(raw, "recent_aggressive_flow")
    flow = _clamp(1.0 - math.exp(-abs(aggressive_flow) / 10.0)) if aggressive_flow is not None else 0.0

    latency_ms = _feature(raw, "latency_ms")
    if latency_ms is None:
        latency = raw.get("latency") if isinstance(raw.get("latency"), dict) else {}
        arrival_ns = max(0.0, _optional_finite(latency.get("arrival_ns")) or 0.0)
        latency_ms = arrival_ns / 1_000_000.0
    latency_score = _clamp(math.exp(-max(0.0, latency_ms) / 250.0))

    tte = _feature(raw, "tte_seconds")
    tte_score = _clamp(1.0 - math.exp(-max(0.0, tte) / 45.0)) if tte is not None else 0.0

    fill_lower = _probability_lower(raw, "fill_probability")
    if fill_lower is None:
        fillability = 1.0 if raw.get("action") == "TAKE" else 0.0
    else:
        fillability = fill_lower

    toxic_lower = _probability_lower(raw, "toxic_fill_probability")
    costs = raw.get("cost_vector") if isinstance(raw.get("cost_vector"), dict) else {}
    adverse = max(0.0, _optional_finite(costs.get("adverse_markout")) or 0.0)
    adverse_selection = (
        _clamp(1.0 - toxic_lower)
        if toxic_lower is not None
        else _clamp(math.exp(-adverse / 0.01))
    )

    missing = [
        name for name in (
            "spread", "recent_aggressive_flow", "tte_seconds",
            "queue_ahead", "book_imbalance", "quote_lifetime_ms",
            "binance_shock_bp", "coinbase_shock_bp", "bybit_shock_bp",
            "cross_venue_disagreement_bp", "oracle_distance_bp", "volatility_bp",
        )
        if _feature(raw, name) is None
    ]
    return {
        "predictability": predictability,
        "mispricing": mispricing,
        "spread_capture": spread_capture,
        "depth": depth,
        "flow": flow,
        "latency": latency_score,
        "tte": tte_score,
        "fillability": fillability,
        "adverse_selection": adverse_selection,
        "missing_execution_features": missing,
    }


def market_score(raw: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    components = selection_components(raw)
    weights = config["market_selection"]["score_weights"]
    score = sum(float(weights[name]) * float(components[name]) for name in SELECTION_COMPONENTS)
    return {
        "score": _clamp(score),
        "components": components,
        "score_semantics": config["market_selection"]["score_semantics"],
    }


def crypto_market_key(raw: dict[str, Any]) -> str:
    context = raw.get("crypto_context") if isinstance(raw.get("crypto_context"), dict) else {}
    return "|".join((
        str(context.get("asset") or "UNKNOWN"),
        str(context.get("horizon") or "UNKNOWN"),
        str(raw.get("market_id") or "UNKNOWN"),
    ))


@dataclass(frozen=True)
class SelectionResult:
    retained_replay_keys: frozenset[str]
    diagnostics: dict[str, Any]


def select_crypto_markets(raw_candidates: Iterable[dict[str, Any]], config: dict[str, Any]) -> SelectionResult:
    validate_config(config)
    rows = [
        raw for raw in raw_candidates
        if isinstance(raw, dict)
        and raw.get("engine_id") == "CRYPTO_SETTLEMENT_ENGINE"
        and raw.get("action") in NEW_RISK_ACTIONS
    ]
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for raw in rows:
        grouped.setdefault(crypto_market_key(raw), []).append((raw, market_score(raw, config)))
    markets: list[dict[str, Any]] = []
    for key, candidates in grouped.items():
        best = max(
            candidates,
            key=lambda item: (
                float(item[1]["score"]),
                float(item[0].get("conservative_expected_wealth_change") or 0.0),
                str(item[0].get("deterministic_replay_key") or ""),
            ),
        )
        action_competition = {
            str(raw.get("action")): max(
                float(raw.get("conservative_expected_wealth_change") or 0.0),
                float(current.get("conservative_expected_wealth_change") or -math.inf),
            )
            for raw, _score in candidates
            for current in [next((x for x, _ in candidates if x.get("action") == raw.get("action")), raw)]
        }
        markets.append({
            "market_key": key,
            "score": float(best[1]["score"]),
            "best_replay_key": str(best[0].get("deterministic_replay_key") or ""),
            "best_action": str(best[0].get("action") or "NOTHING"),
            "best_conservative_ev": float(best[0].get("conservative_expected_wealth_change") or 0.0),
            "action_competition": action_competition,
            "components": best[1]["components"],
            "candidate_count": len(candidates),
        })
    markets.sort(key=lambda row: (-row["score"], -row["best_conservative_ev"], row["market_key"]))
    selection = config["market_selection"]
    count = len(markets)
    if count == 0:
        retain_count = 0
    elif count < int(selection["minimum_candidate_markets"]):
        retain_count = count
    else:
        retain_count = max(
            int(selection["minimum_markets_retained"]),
            int(math.ceil(count * float(selection["top_fraction"]))),
        )
        retain_count = min(count, retain_count)
    retained_markets = {row["market_key"] for row in markets[:retain_count]}
    retained_replay_keys = frozenset(
        str(raw.get("deterministic_replay_key") or "")
        for raw in rows if crypto_market_key(raw) in retained_markets
    )
    return SelectionResult(
        retained_replay_keys=retained_replay_keys,
        diagnostics={
            "schema": "polymarket_v7_crypto_execution_alpha_selection_v1",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "market_count": count,
            "retained_market_count": retain_count,
            "top_fraction": float(selection["top_fraction"]),
            "retained_market_keys": sorted(retained_markets),
            "markets": markets,
        },
    )


def information_rank(raw: dict[str, Any], config: dict[str, Any]) -> tuple[float, float, str]:
    """Rank bounded PAPER probes by information value, then economic point EV."""
    validate_config(config)
    exploration = raw.get("exploration") if isinstance(raw.get("exploration"), dict) else {}
    base = max(0.0, _optional_finite(exploration.get("information_score")) or 0.0)
    uncertainty = raw.get("uncertainty") if isinstance(raw.get("uncertainty"), dict) else {}
    lower = _optional_finite(uncertainty.get("lower_bound"))
    upper = _optional_finite(uncertainty.get("upper_bound"))
    width = max(0.0, (upper or 0.0) - (lower or 0.0)) if lower is not None and upper is not None else 0.0
    components = selection_components(raw)
    missing_share = len(components["missing_execution_features"]) / max(1, len(FEATURE_NAMES))
    score = base * (1.0 + min(1.0, width) + 0.25 * missing_share)
    point = max(0.0, _optional_finite(exploration.get("point_expected_wealth_change")) or 0.0)
    return score, point, str(raw.get("deterministic_replay_key") or "")


def action_competition(raw_candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Expose MAKE/TAKE competition without inventing an execution plan."""
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in raw_candidates:
        if not isinstance(raw, dict) or raw.get("engine_id") != "CRYPTO_SETTLEMENT_ENGINE":
            continue
        action = str(raw.get("action") or "")
        if action not in {"MAKE", "TAKE", "CANCEL", "WITHDRAW", "NOTHING"}:
            continue
        key = crypto_market_key(raw)
        current = grouped.setdefault(key, {}).get(action)
        if current is None or float(raw.get("conservative_expected_wealth_change") or 0.0) > float(current.get("conservative_expected_wealth_change") or 0.0):
            grouped[key][action] = raw
    output: dict[str, Any] = {}
    for key, actions in grouped.items():
        ordered = sorted(
            actions.items(),
            key=lambda item: (
                -float(item[1].get("conservative_expected_wealth_change") or 0.0),
                item[0],
            ),
        )
        output[key] = {
            "actions": {
                action: {
                    "conservative_ev": float(raw.get("conservative_expected_wealth_change") or 0.0),
                    "replay_key": str(raw.get("deterministic_replay_key") or ""),
                }
                for action, raw in sorted(actions.items())
            },
            "best_action": ordered[0][0] if ordered else "NOTHING",
            "best_conservative_ev": float(ordered[0][1].get("conservative_expected_wealth_change") or 0.0) if ordered else 0.0,
        }
    return output
