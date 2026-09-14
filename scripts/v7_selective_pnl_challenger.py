#!/usr/bin/env python3
"""Zero-authority selective-PnL challenger for V7.

This module is deliberately outside the live execution path.  It converts the
five economic priorities of the September 2026 review into deterministic shadow
gates that can be frozen before the next forward window:

1. Maker quotes only when conservative execution alpha survives toxicity.
2. A separate directional lane requires a strong post-cost edge.
3. Entry latency has explicit prospective SLOs and per-trade expiry.
4. The frozen external-cancel rule preempts Maker risk.
5. Every decision is tagged with a predeclared economic regime.

It cannot submit orders, own capital, write the canonical ledger, or promote a
policy. Live integration requires a later, separately reviewed exact-SHA change
after the currently running forward experiment has finished.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

CONFIG_SCHEMA = "polymarket_v7_selective_pnl_challenger_v1"
OUTPUT_SCHEMA = "polymarket_v7_selective_pnl_challenger_report_v1"
EXECUTION_FEATURES = (
    "queue_ahead", "spread", "book_imbalance", "recent_aggressive_flow",
    "quote_lifetime_ms", "tte_seconds", "binance_shock_bp",
    "coinbase_shock_bp", "bybit_shock_bp", "cross_venue_disagreement_bp",
    "oracle_distance_bp", "volatility_bp", "latency_ms",
)


class ChallengerError(ValueError):
    pass


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ChallengerError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ChallengerError(name) from exc
    if not math.isfinite(number):
        raise ChallengerError(name)
    return number


def validate_config(config: dict[str, Any]) -> None:
    if (
        not isinstance(config, dict)
        or config.get("schema") != CONFIG_SCHEMA
        or config.get("version") != 1
        or config.get("paper_only") is not True
        or config.get("authenticated_execution") is not False
        or config.get("real_order_submission") is not False
        or config.get("real_capital_at_risk") is not False
        or config.get("execution_authority") != "ZERO_AUTHORITY_RESEARCH_ONLY"
        or config.get("automatic_promotion") is not False
    ):
        raise ChallengerError("config_authority")
    maker = config.get("maker_gate") or {}
    if (
        maker.get("quote_everywhere") is not False
        or maker.get("required_packet_evidence_status") != "MATURE"
        or maker.get("require_market_retained") is not True
        or maker.get("require_all_execution_features") is not True
        or finite(maker.get("minimum_fill_probability_lower"), "maker_fill_floor") != 0.0
        or not 0.0 < finite(maker.get("maximum_toxic_fill_probability_upper"), "maker_toxic_cap") < 1.0
        or finite(maker.get("minimum_conservative_make_ev"), "maker_ev_floor") != 0.0
    ):
        raise ChallengerError("config_maker_gate")
    cancel = maker.get("external_cancel") or {}
    if (
        cancel.get("rule_id") != "btc-m5-external-cancel-v1"
        or int(cancel.get("maximum_signal_age_ms") or 0) != 100
        or cancel.get("active_action") != "CANCEL_OR_WITHDRAW"
        or cancel.get("unknown_action") != "WITHDRAW"
    ):
        raise ChallengerError("config_external_cancel")
    directional = config.get("directional_lane") or {}
    if (
        directional.get("mode") != "SHADOW_ONLY"
        or directional.get("allowed_sides") != ["YES", "NO"]
        or finite(directional.get("primary_minimum_net_edge_after_2x_cost_per_share"), "directional_edge") != 0.01
        or finite(directional.get("cost_stress_multiplier"), "directional_stress") != 2.0
        or finite(directional.get("minimum_visible_depth_shares"), "directional_depth") < 5.0
        or finite(directional.get("maximum_decision_to_arrival_ms"), "directional_latency") <= 0.0
        or directional.get("arrival_revalidation_required") is not True
        or directional.get("verified_contract_binding_required") is not True
        or directional.get("fresh_external_features_required") is not True
        or directional.get("mature_model_required_for_primary") is not True
    ):
        raise ChallengerError("config_directional_lane")
    if directional.get("research_threshold_grid") != [0.005, 0.01, 0.03, 0.1]:
        raise ChallengerError("config_directional_grid")
    latency = config.get("latency") or {}
    if int(latency.get("candidate_scan_interval_ms") or 0) != 250:
        raise ChallengerError("config_scan_interval")
    if int(latency.get("synthetic_revalidation_sleep_ms", -1)) != 0:
        raise ChallengerError("config_synthetic_sleep")
    slo = latency.get("decision_to_arrival_slo_ms") or {}
    if [finite(slo.get(key), f"latency_{key}") for key in ("p50", "p90", "p99")] != [150.0, 250.0, 500.0]:
        raise ChallengerError("config_latency_slo")
    regimes = config.get("regimes") or {}
    required_regimes = {
        "tte_seconds", "external_disagreement_bp", "external_shock_abs_100ms_bp",
        "spread_ticks", "volatility_ticks", "oracle_distance_bp", "book_imbalance", "side",
    }
    if set(regimes) != required_regimes or regimes.get("side") != ["YES", "NO"]:
        raise ChallengerError("config_regimes")
    for name in required_regimes - {"side"}:
        edges = regimes[name]
        if not isinstance(edges, list) or len(edges) < 2:
            raise ChallengerError(f"config_regime_edges:{name}")
        numbers = [finite(value, f"config_regime_edges:{name}") for value in edges]
        if numbers != sorted(set(numbers)):
            raise ChallengerError(f"config_regime_edges:{name}")
    inference = config.get("forward_inference") or {}
    if (
        int(inference.get("minimum_independent_contracts") or 0) < 30
        or int(inference.get("bootstrap_draws") or 0) < 1000
        or inference.get("one_look_only") is not True
        or inference.get("no_threshold_change_after_start") is not True
        or inference.get("no_automatic_sizing_change") is not True
    ):
        raise ChallengerError("config_inference")
    canonical_hash(config)


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(config)
    return config


def _band(packet: dict[str, Any], name: str) -> tuple[float, float, float]:
    value = packet.get(name)
    if not isinstance(value, dict):
        raise ChallengerError(f"packet_{name}")
    lower = finite(value.get("lower"), f"packet_{name}_lower")
    point = finite(value.get("point"), f"packet_{name}_point")
    upper = finite(value.get("upper"), f"packet_{name}_upper")
    if not 0.0 <= lower <= point <= upper <= 1.0:
        raise ChallengerError(f"packet_{name}_bounds")
    return lower, point, upper


def maker_shadow_decision(row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed Maker gate. No result from this function is execution authority."""
    validate_config(config)
    policy = config["maker_gate"]
    reasons: list[str] = []
    cancel_state = str(row.get("external_cancel_state") or "UNKNOWN").upper()
    active_order = row.get("active_order") is True
    if cancel_state == "ACTIVE":
        return {
            "action": "CANCEL_SHADOW" if active_order else "WITHDRAW_SHADOW",
            "eligible": False,
            "reasons": ["EXTERNAL_CANCEL_ACTIVE"],
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        }
    if cancel_state != "CLEAR":
        reasons.append("EXTERNAL_CANCEL_STATE_UNKNOWN")
    if row.get("market_retained") is not True:
        reasons.append("MARKET_NOT_RETAINED")
    packet = row.get("execution_alpha")
    if not isinstance(packet, dict):
        reasons.append("EXECUTION_ALPHA_MISSING")
        packet = {}
    if packet.get("evidence_status") != policy["required_packet_evidence_status"]:
        reasons.append("EXECUTION_ALPHA_NOT_MATURE")
    features = packet.get("features") if isinstance(packet.get("features"), dict) else {}
    if policy["require_all_execution_features"] and any(features.get(name) is None for name in EXECUTION_FEATURES):
        reasons.append("EXECUTION_FEATURES_INCOMPLETE")
    try:
        fill_lower, _, _ = _band(packet, "fill_probability")
        _, _, toxic_upper = _band(packet, "toxic_fill_probability")
    except ChallengerError:
        fill_lower, toxic_upper = -1.0, 2.0
        reasons.append("EXECUTION_PROBABILITY_BANDS_INVALID")
    if fill_lower <= float(policy["minimum_fill_probability_lower"]):
        reasons.append("NO_POSITIVE_FILL_PROBABILITY_LOWER_BOUND")
    if toxic_upper > float(policy["maximum_toxic_fill_probability_upper"]):
        reasons.append("TOXICITY_TOO_HIGH")
    action_ev = packet.get("action_ev") if isinstance(packet.get("action_ev"), dict) else {}
    make = action_ev.get("MAKE") if isinstance(action_ev.get("MAKE"), dict) else {}
    try:
        conservative = finite(make.get("conservative"), "make_conservative_ev")
    except ChallengerError:
        conservative = -math.inf
        reasons.append("MAKE_CONSERVATIVE_EV_MISSING")
    if conservative <= float(policy["minimum_conservative_make_ev"]):
        reasons.append("MAKE_CONSERVATIVE_EV_NOT_POSITIVE")
    return {
        "action": "MAKE_SHADOW_ELIGIBLE" if not reasons else "WITHDRAW_SHADOW",
        "eligible": not reasons,
        "reasons": reasons,
        "fill_probability_lower": fill_lower if math.isfinite(fill_lower) else None,
        "toxic_fill_probability_upper": toxic_upper if math.isfinite(toxic_upper) else None,
        "conservative_make_ev": conservative if math.isfinite(conservative) else None,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
    }


def directional_shadow_decision(row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Prospective selective taker candidate; never submits or authorizes an order."""
    validate_config(config)
    policy = config["directional_lane"]
    reasons: list[str] = []
    side = str(row.get("side") or "").upper()
    if side not in policy["allowed_sides"]:
        reasons.append("SIDE_NOT_ALLOWED")
    if row.get("arrival_revalidated") is not True:
        reasons.append("ARRIVAL_NOT_REVALIDATED")
    if row.get("contract_verified") is not True:
        reasons.append("CONTRACT_BINDING_NOT_VERIFIED")
    if row.get("external_features_fresh") is not True:
        reasons.append("EXTERNAL_FEATURES_NOT_FRESH")
    if row.get("model_mature") is not True:
        reasons.append("MODEL_NOT_MATURE")
    try:
        probability = finite(row.get("model_probability"), "model_probability")
        ask = finite(row.get("best_ask"), "best_ask")
        fee = finite(row.get("fee_per_share"), "fee_per_share")
        risk = finite(row.get("risk_allowance_per_share"), "risk_allowance_per_share")
        depth = finite(row.get("visible_depth_shares"), "visible_depth_shares")
        latency = finite(row.get("decision_to_arrival_ms"), "decision_to_arrival_ms")
    except ChallengerError as exc:
        return {
            "action": "NOTHING_SHADOW", "eligible": False,
            "reasons": [f"INVALID_NUMERIC_INPUT:{exc}"],
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        }
    if not 0.0 <= probability <= 1.0 or not 0.0 < ask < 1.0 or min(fee, risk, depth, latency) < 0.0:
        reasons.append("NUMERIC_BOUNDS_INVALID")
    if depth < float(policy["minimum_visible_depth_shares"]):
        reasons.append("INSUFFICIENT_VISIBLE_DEPTH")
    if latency > float(policy["maximum_decision_to_arrival_ms"]):
        reasons.append("ENTRY_LATENCY_EXPIRED")
    stress = float(policy["cost_stress_multiplier"])
    net_edge = probability - ask - stress * (fee + risk)
    threshold = float(policy["primary_minimum_net_edge_after_2x_cost_per_share"])
    threshold_epsilon = 1e-12
    if net_edge + threshold_epsilon < threshold:
        reasons.append("PRIMARY_POST_COST_EDGE_BELOW_THRESHOLD")
    research_grid = {
        f"edge_ge_{threshold_value:g}": net_edge + threshold_epsilon >= float(threshold_value)
        for threshold_value in policy["research_threshold_grid"]
    }
    return {
        "action": "TAKE_SHADOW_ELIGIBLE" if not reasons else "NOTHING_SHADOW",
        "eligible": not reasons,
        "reasons": reasons,
        "net_edge_after_2x_cost_per_share": net_edge,
        "threshold_grid": research_grid,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
    }


def percentile(values: Iterable[float], probability: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    position = probability * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def latency_gate(values: Iterable[float], config: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    samples = [finite(value, "latency_sample") for value in values]
    metrics = {
        "p50": percentile(samples, 0.50),
        "p90": percentile(samples, 0.90),
        "p99": percentile(samples, 0.99),
    }
    slo = config["latency"]["decision_to_arrival_slo_ms"]
    passed = bool(samples) and all(metrics[key] is not None and metrics[key] <= float(slo[key]) for key in metrics)
    return {
        "samples": len(samples), "metrics_ms": metrics,
        "slo_ms": dict(slo), "pass": passed,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
    }


def regime_bucket(value: float, edges: list[float]) -> str:
    for index, (lower, upper) in enumerate(zip(edges, edges[1:])):
        if lower <= value < upper:
            return str(index)
    return "OUTSIDE_FIXED_GRID"


def regime_key(row: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    validate_config(config)
    regimes = config["regimes"]
    result: dict[str, str] = {}
    for name, edges in regimes.items():
        if name == "side":
            side = str(row.get("side") or "UNKNOWN").upper()
            result[name] = side if side in edges else "UNKNOWN"
            continue
        raw = row.get(name)
        if raw is None:
            result[name] = "UNKNOWN"
            continue
        try:
            value = finite(raw, name)
        except ChallengerError:
            result[name] = "UNKNOWN"
            continue
        result[name] = regime_bucket(value, [float(edge) for edge in edges])
    return result


def evaluate_rows(rows: Iterable[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    validate_config(config)
    actions: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    regimes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    latency_samples: list[float] = []
    records = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        records += 1
        kind = str(row.get("kind") or "").upper()
        if kind == "MAKER":
            decision = maker_shadow_decision(row, config)
        elif kind == "DIRECTIONAL":
            decision = directional_shadow_decision(row, config)
        elif kind == "LATENCY":
            try:
                latency_samples.append(finite(row.get("decision_to_arrival_ms"), "decision_to_arrival_ms"))
            except ChallengerError:
                reasons["INVALID_LATENCY_SAMPLE"] += 1
            continue
        else:
            reasons["UNKNOWN_RECORD_KIND"] += 1
            continue
        actions[decision["action"]] += 1
        for reason in decision["reasons"]:
            reasons[reason] += 1
        for name, bucket in regime_key(row, config).items():
            regimes[name][bucket] += 1
    return {
        "schema": OUTPUT_SCHEMA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "policy_sha256": canonical_hash(config),
        "records": records,
        "actions": dict(sorted(actions.items())),
        "reasons": dict(sorted(reasons.items())),
        "regimes": {name: dict(sorted(values.items())) for name, values in sorted(regimes.items())},
        "latency": latency_gate(latency_samples, config),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ChallengerError(f"jsonl_row_not_object:{number}")
            rows.append(value)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/v7_selective_pnl_challenger.json"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    report = evaluate_rows(read_jsonl(args.input), config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
