"""Prospective worst-case censor bounds for confirmatory profit endpoints.

The module is deliberately endpoint-specific. It never drops a censored unit,
never changes a v1 protocol, and never supplies an execution permission.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

MODE = "WORST_CASE_LOWER_SUPPORT_IMPUTATION"
SCHEMA_V2 = "polymarket_v7_profit_experiment_protocol_v2"
SETTLEMENT_ENDPOINT = "selected_settlement_surplus_cost2_delay1000"
MAKER_ENDPOINT = "maker_join10_minus_join5_settlement_net_cost2"
BRIER_ENDPOINT = "model_brier_improvement_over_pm"
SETTLEMENT_RULE = "VERIFIED_Y_MINUS_ONE_MINUS_2_TIMES_MAX_BINARY_FEE_PLUS_FROZEN_RISK"
MAKER_RULE = "VERIFIED_Y_ARM_INTERVAL_FROM_FROZEN_QUOTE_CAP_AND_PRICE_SUPPORT_ZERO_ONE"


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def policy(protocol: dict[str, Any]) -> dict[str, Any] | None:
    if protocol.get("schema") != SCHEMA_V2:
        return None
    confirm = (protocol.get("inference") or {}).get("confirmatory") or {}
    value = confirm.get("censoring")
    if not isinstance(value, dict) or value.get("mode") != MODE:
        return None
    return value


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


def exact_settlement_value(label: dict[str, Any], y: float) -> float | None:
    if not isinstance(label, dict) or label.get("state") != "OBSERVED":
        return None
    cut = label.get("book_cut") or {}
    ask, fee, risk = cut.get("best_ask"), label.get("fee_per_share"), label.get("risk_allowance_per_share")
    if not all(finite(v) for v in (ask, fee, risk)) or y not in (0.0, 1.0):
        return None
    return float(y) - float(ask) - 2.0 * (float(fee) + float(risk))


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
    censored = join5.get("state") not in ("OBSERVED", "FLOW_FILTER_ABSTAIN") or join10.get("state") not in ("OBSERVED", "FLOW_FILTER_ABSTAIN")
    return interval10[0] - interval5[1], censored


def _contract_means(values: dict[str, list[float]]) -> dict[str, float]:
    return {market: sum(rows) / len(rows) for market, rows in values.items() if rows}


def bounded_primary(
    selections: dict[str, dict[str, Any]],
    delays: dict[tuple[str, int], dict[str, Any]],
    comparisons: list[dict[str, Any]],
    manifest: dict[str, Any],
    settlements: dict[str, Any],
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Rebuild all three primary endpoints with declared worst-case censor support."""
    protocol = manifest["protocol"]
    censor = policy(protocol)
    if censor is None:
        raise ValueError("profit_censor_bounds:protocol_not_v2")
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
        actual = exact_settlement_value(label or {}, y)
        if actual is not None:
            settlement_values[str(row["market_id"])].append(actual)
            continue
        endpoint[SETTLEMENT_ENDPOINT]["censored_units"] += 1
        lower = settlement_lower_support(row, protocol, y)
        if lower is None:
            endpoint[SETTLEMENT_ENDPOINT]["support_unavailable_units"] += 1
        else:
            settlement_values[str(row["market_id"])].append(lower)
            endpoint[SETTLEMENT_ENDPOINT]["support_imputed_units"] += 1

    for row in comparisons:
        origin_ns = int(row.get("origin_ms", 0) * 1_000_000) if row.get("origin_ms") else None
        # Comparison rows do not always carry origin_ms. The report passes only
        # comparisons selected by the fixed-window anchor population, so absence
        # here is acceptable; an explicit outside-window timestamp is not.
        if origin_ns is not None and not start <= origin_ns < end:
            continue
        endpoint[MAKER_ENDPOINT]["eligible_units"] += 1
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
