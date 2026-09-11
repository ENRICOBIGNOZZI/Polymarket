"""Prospective worst-case censor bounds for confirmatory profit endpoints.

The module is deliberately endpoint-specific. It never drops a censored unit,
never changes legacy v4 semantics, and never supplies execution permission.
The v5 behavior is opt-in inside the existing protocol-v1 schema.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

MODE = "WORST_CASE_LOWER_SUPPORT_IMPUTATION"
SCHEMA_V1 = "polymarket_v7_profit_experiment_protocol_v1"
PROTOCOL_ID = "permanent-profit-causes-20260911-v5"
SETTLEMENT_ENDPOINT = "selected_settlement_surplus_cost2_delay1000"
MAKER_ENDPOINT = "maker_join10_minus_join5_settlement_net_cost2"
BRIER_ENDPOINT = "model_brier_improvement_over_pm"
SETTLEMENT_RULE = "VERIFIED_Y_MINUS_ONE_MINUS_2_TIMES_MAX_BINARY_FEE_PLUS_FROZEN_RISK"
MAKER_RULE = "VERIFIED_Y_ARM_INTERVAL_FROM_FROZEN_QUOTE_CAP_AND_PRICE_SUPPORT_ZERO_ONE"
FROZEN_CENSOR_FRACTION = 0.05


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def policy(protocol: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact registered v5 censor policy, otherwise preserve legacy behavior."""
    if protocol.get("schema") != SCHEMA_V1 or protocol.get("protocol_id") != PROTOCOL_ID:
        return None
    confirm = (protocol.get("inference") or {}).get("confirmatory") or {}
    value = confirm.get("censoring")
    if not isinstance(value, dict) or value.get("mode") != MODE:
        return None
    if value.get("prospective_only") is not True or value.get("no_censor_dropping") is not True:
        raise ValueError("profit_censor_bounds:policy_safety")
    if value.get("terminal_records_required") is not True or value.get("verified_settlements_required") is not True:
        raise ValueError("profit_censor_bounds:terminal_evidence_required")
    caps = value.get("endpoint_max_censor_fraction")
    if not isinstance(caps, dict) or set(caps) != {SETTLEMENT_ENDPOINT, MAKER_ENDPOINT}:
        raise ValueError("profit_censor_bounds:cap_identity")
    for endpoint, cap in caps.items():
        if not finite(cap) or abs(float(cap) - FROZEN_CENSOR_FRACTION) > 1e-15:
            raise ValueError("profit_censor_bounds:cap_bounds:" + endpoint)
    return value


def fee_at(price: float, schedule: dict[str, Any]) -> float | None:
    if not finite(price) or not 0 <= float(price) <= 1 or not isinstance(schedule, dict):
        return None
    rate, exponent = schedule.get("rate"), schedule.get("exponent")
    if not finite(rate) or not finite(exponent) or not 0 <= rate <= 1 or not 0 < exponent <= 10:
        return None
    return float(rate) * (float(price) * (1.0 - float(price))) ** float(exponent)


def max_binary_fee(schedule: dict[str, Any]) -> float | None:
    """Exact maximum of rate * (p(1-p))**exponent over p in [0,1]."""
    if not isinstance(schedule, dict):
        return None
    rate, exponent = schedule.get("rate"), schedule.get("exponent")
    if not finite(rate) or not finite(exponent) or not 0 <= rate <= 1 or not 0 < exponent <= 10:
        return None
    return float(rate) * (0.25 ** float(exponent))


def settlement_y(row: dict[str, Any], settlements: dict[str, Any]) -> float | None:
    label = settlements.get(str(row.get("market_id") or ""))
    token = str(row.get("token_id") or "")
    if not isinstance(label, dict) or token not in (label.get("tokens") or []):
        return None
    winner = str(label.get("winning_token_id") or "")
    return float(winner == token)


def settlement_lower_support(selection: dict[str, Any], protocol: dict[str, Any], y: float) -> float | None:
    fee = max_binary_fee(selection.get("fee_schedule") or {})
    risk = (protocol.get("signal") or {}).get("execution_risk_per_share")
    if fee is None or not finite(risk) or risk < 0 or y not in (0.0, 1.0):
        return None
    return float(y) - 1.0 - 2.0 * (fee + float(risk))


def exact_settlement_value(
    label: dict[str, Any], selection: dict[str, Any], protocol: dict[str, Any], y: float
) -> float | None:
    """Validate an OBSERVED terminal value against the frozen fee/risk identity."""
    if not isinstance(label, dict) or label.get("state") != "OBSERVED":
        return None
    cut = label.get("book_cut") or {}
    ask, fee, risk = cut.get("best_ask"), label.get("fee_per_share"), label.get("risk_allowance_per_share")
    if not all(finite(v) for v in (ask, fee, risk)) or not 0 <= float(ask) <= 1 or y not in (0.0, 1.0):
        return None
    expected_fee = fee_at(float(ask), selection.get("fee_schedule") or {})
    frozen_risk = (protocol.get("signal") or {}).get("execution_risk_per_share")
    if expected_fee is None or not finite(frozen_risk) or float(frozen_risk) < 0:
        return None
    if abs(float(fee) - expected_fee) > 1e-10 or abs(float(risk) - float(frozen_risk)) > 1e-12:
        return None
    return float(y) - float(ask) - 2.0 * (expected_fee + float(frozen_risk))


def arm_quote_cap(arm: dict[str, Any]) -> float | None:
    for value in (arm.get("common_quote_quantity"), (arm.get("research_request") or {}).get("quantity")):
        if finite(value) and value >= 0:
            return float(value)
    return None


def exact_arm_net(arm: dict[str, Any], y: float, risk: float) -> float | None:
    if arm.get("state") not in ("OBSERVED", "FLOW_FILTER_ABSTAIN"):
        return None
    fills = arm.get("fills") if isinstance(arm.get("fills"), list) else []
    total = 0.0
    quantity = 0.0
    for fill in fills:
        q, price = fill.get("quantity"), fill.get("price")
        if not finite(q) or not finite(price) or q < 0 or not 0 <= price <= 1:
            return None
        quantity += float(q)
        total += float(q) * (float(y) - float(price))
    declared = arm.get("operational_filled_shares")
    if not finite(declared) or declared < 0 or abs(quantity - float(declared)) > 1e-9:
        return None
    return total - 2.0 * float(risk) * quantity


def arm_net_interval(arm: dict[str, Any], y: float, risk: float) -> tuple[float, float] | None:
    exact = exact_arm_net(arm, y, risk)
    if exact is not None:
        return exact, exact
    cap = arm_quote_cap(arm)
    if cap is None or y not in (0.0, 1.0) or not finite(risk) or risk < 0:
        return None
    # q can range from zero to the frozen quote cap and price lies in [0,1].
    lower_per_share = min(0.0, float(y) - 1.0 - 2.0 * float(risk))
    upper_per_share = max(0.0, float(y) - 2.0 * float(risk))
    return cap * lower_per_share, cap * upper_per_share


def maker_delta_lower_support(comparison: dict[str, Any], protocol: dict[str, Any], settlements: dict[str, Any]) -> tuple[float | None, bool]:
    market = str(comparison.get("market_id") or "")
    token = str(comparison.get("token_id") or "")
    label = settlements.get(market)
    if not isinstance(label, dict) or token not in (label.get("tokens") or []):
        return None, False
    y = float(str(label.get("winning_token_id") or "") == token)
    risk = (protocol.get("signal") or {}).get("execution_risk_per_share")
    if not finite(risk) or risk < 0:
        return None, False
    arms = {str(a.get("arm") or ""): a for a in comparison.get("arms", []) if isinstance(a, dict)}
    join5, join10 = arms.get("JOIN_5S"), arms.get("JOIN_10S")
    if not join5 or not join10:
        return None, False
    interval5 = arm_net_interval(join5, y, float(risk))
    interval10 = arm_net_interval(join10, y, float(risk))
    if interval5 is None or interval10 is None:
        return None, False
    censored = (
        join5.get("state") not in ("OBSERVED", "FLOW_FILTER_ABSTAIN")
        or join10.get("state") not in ("OBSERVED", "FLOW_FILTER_ABSTAIN")
    )
    return interval10[0] - interval5[1], censored


def _contract_means(values: dict[str, list[float]]) -> dict[str, float]:
    return {market: sum(rows) / len(rows) for market, rows in values.items() if rows}


def bounded_primary(
    selections: dict[str, dict[str, Any]],
    delays: dict[tuple[str, int], dict[str, Any]],
    comparisons: list[dict[str, Any]],
    maker_anchor_ns: dict[str, int],
    manifest: dict[str, Any],
    settlements: dict[str, Any],
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Rebuild all three primaries without deleting terminally censored units."""
    protocol = manifest["protocol"]
    censor = policy(protocol)
    if censor is None:
        raise ValueError("profit_censor_bounds:protocol_not_v5")
    start, end = manifest["forward_start_ns"], manifest["confirmatory_end_ns"]
    brier: defaultdict[str, list[float]] = defaultdict(list)
    settlement_values: defaultdict[str, list[float]] = defaultdict(list)
    maker_values: defaultdict[str, list[float]] = defaultdict(list)
    endpoint = {
        BRIER_ENDPOINT: {"eligible_units": 0, "censored_units": 0, "support_imputed_units": 0, "support_unavailable_units": 0},
        SETTLEMENT_ENDPOINT: {"eligible_units": 0, "censored_units": 0, "support_imputed_units": 0, "support_unavailable_units": 0},
        MAKER_ENDPOINT: {"eligible_units": 0, "censored_units": 0, "support_imputed_units": 0, "support_unavailable_units": 0},
    }
    for key, row in sorted(selections.items(), key=lambda item: item[1]["origin_ns"]):
        if not start <= int(row["origin_ns"]) < end:
            continue
        y = settlement_y(row, settlements)
        endpoint[BRIER_ENDPOINT]["eligible_units"] += 1
        endpoint[SETTLEMENT_ENDPOINT]["eligible_units"] += 1
        if y is None:
            endpoint[BRIER_ENDPOINT]["support_unavailable_units"] += 1
            endpoint[SETTLEMENT_ENDPOINT]["support_unavailable_units"] += 1
            continue
        p, pm = row.get("model_probability"), row.get("pm_probability")
        if not all(finite(v) and 0 <= v <= 1 for v in (p, pm)):
            endpoint[BRIER_ENDPOINT]["support_unavailable_units"] += 1
        else:
            brier[str(row["market_id"])].append((float(pm) - y) ** 2 - (float(p) - y) ** 2)

        label = delays.get((key, 1000))
        if label is None:
            # A missing terminal record is not a censor and cannot be imputed.
            endpoint[SETTLEMENT_ENDPOINT]["support_unavailable_units"] += 1
            continue
        actual = exact_settlement_value(label, row, protocol, y)
        if actual is not None:
            settlement_values[str(row["market_id"])].append(actual)
            continue
        if label.get("state") == "OBSERVED":
            # Malformed observed economics is an identity failure, not censoring.
            endpoint[SETTLEMENT_ENDPOINT]["support_unavailable_units"] += 1
            continue
        endpoint[SETTLEMENT_ENDPOINT]["censored_units"] += 1
        lower = settlement_lower_support(row, protocol, y)
        if lower is None:
            endpoint[SETTLEMENT_ENDPOINT]["support_unavailable_units"] += 1
        else:
            settlement_values[str(row["market_id"])].append(lower)
            endpoint[SETTLEMENT_ENDPOINT]["support_imputed_units"] += 1

    comparisons_by_anchor: dict[str, dict[str, Any]] = {}
    for row in comparisons:
        anchor_id = str(row.get("anchor_record_id") or "")
        if not anchor_id:
            continue
        if anchor_id in comparisons_by_anchor and comparisons_by_anchor[anchor_id] != row:
            raise ValueError("profit_censor_bounds:conflicting_maker_comparison")
        comparisons_by_anchor[anchor_id] = row
    for anchor_id, origin_ns in sorted(maker_anchor_ns.items(), key=lambda item: item[1]):
        if not start <= int(origin_ns) < end:
            continue
        endpoint[MAKER_ENDPOINT]["eligible_units"] += 1
        row = comparisons_by_anchor.get(anchor_id)
        if row is None:
            endpoint[MAKER_ENDPOINT]["support_unavailable_units"] += 1
            continue
        lower, censored = maker_delta_lower_support(row, protocol, settlements)
        if censored:
            endpoint[MAKER_ENDPOINT]["censored_units"] += 1
        if lower is None:
            endpoint[MAKER_ENDPOINT]["support_unavailable_units"] += 1
            continue
        maker_values[str(row["market_id"])].append(lower)
        if censored:
            endpoint[MAKER_ENDPOINT]["support_imputed_units"] += 1

    caps = censor["endpoint_max_censor_fraction"]
    for key, item in endpoint.items():
        eligible = item["eligible_units"]
        fraction = item["censored_units"] / eligible if eligible else 0.0
        cap = 0.0 if key == BRIER_ENDPOINT else float(caps[key])
        item["censor_fraction"] = fraction
        item["max_censor_fraction"] = cap
        item["cap_pass"] = fraction <= cap + 1e-15
        item["support_complete"] = item["support_unavailable_units"] == 0
    endpoint[SETTLEMENT_ENDPOINT]["support_rule"] = SETTLEMENT_RULE
    endpoint[MAKER_ENDPOINT]["support_rule"] = MAKER_RULE
    endpoint[BRIER_ENDPOINT]["support_rule"] = "VERIFIED_SETTLEMENT_REQUIRED; DELAY_CENSORING_NOT_APPLICABLE"
    all_pass = all(item["cap_pass"] and item["support_complete"] for item in endpoint.values())
    return {
        BRIER_ENDPOINT: _contract_means(brier),
        SETTLEMENT_ENDPOINT: _contract_means(settlement_values),
        MAKER_ENDPOINT: _contract_means(maker_values),
    }, {
        "mode": MODE,
        "prospective_only": True,
        "no_censor_dropping": True,
        "endpoint_audit": endpoint,
        "all_endpoint_caps_and_support_pass": all_pass,
    }
