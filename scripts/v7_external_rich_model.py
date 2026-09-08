#!/usr/bin/env python3
"""Compact, frozen, receive-time-causal BTC M5 logistic model.

One existing fair owner consumes this module. No network, OMS, registry writes,
training, mutable weights or implicit deployment occur during inference.
Intervals are deliberately [0, 1]: a research point estimate is NOT a validated
conditional probability confidence bound. Only bounded PAPER probes may use it.
"""
from __future__ import annotations

import math
import re
from typing import Any

FAMILY = "btc_m5_rich_external_logit_v1"
FEATURE_SCHEMA = "btc-m5-rich-external-causal-v2"
HISTORY_SEMANTICS = "receive_time_bucketed_composite_v2"
MODEL_PREFIX = "btc-m5-rich-logit-"


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


def logit(p: float) -> float:
    p = min(1.0 - 1e-9, max(1e-9, p))
    return math.log(p / (1.0 - p))


FEATURE_NAMES = (
    "log_tte", "oracle_margin_bp", "spot_oracle_basis_bp", "spot_margin_bp",
    "terminal_fraction", "oracle_margin_time_scaled", "basis_terminal_interaction",
    "microprice_shift_bp", "dispersion_bp", "ofi", "trade_imbalance",
    "vol_fast_bp", "vol_medium_bp", "vol_slow_bp", "log_vol_ratio", "jump_score",
    "external_age_ms", "return_50ms_bp", "return_100ms_bp", "return_250ms_bp", "return_1s_bp", "return_5s_bp",
    "binance_perp_basis_bp", "bybit_perp_basis_bp", "deribit_perp_basis_bp",
    "binance_perp_microprice_basis_bp", "binance_perp_depth_imbalance_l20", "binance_perp_trade_imbalance",
    "bybit_perp_microprice_basis_bp", "bybit_perp_depth_imbalance_l10", "bybit_perp_trade_flow",
    "deribit_perp_book_basis_bp", "deribit_trade_flow",
    "binance_funding", "bybit_funding", "deribit_funding_8h",
    "binance_open_interest_log", "binance_oi_velocity", "bybit_open_interest_log", "deribit_open_interest_log",
    "deribit_atm_iv", "deribit_vol_term_slope", "deribit_put_call_skew", "deribit_butterfly",
    "deribit_nearest_future_basis_bp", "deribit_historical_volatility",
    "binance_liquidation_signed_btc_per_s", "binance_liquidation_total_btc_per_s",
    "bybit_liquidation_signed_btc_per_s", "bybit_liquidation_total_btc_per_s",
)


def features(origin: dict[str, Any]) -> dict[str, float | None]:
    """Same transform for stored FORECAST and current receive-time feature cut.

    Pre-fix 256-event-ring returns are deliberately treated as unavailable,
    not as flat-price evidence. Optional absence is represented explicitly.
    """
    ext = origin.get("external_features") or {}
    o, k, spot, tte = (number(origin.get("oracle_value")), number(origin.get("reference_value")),
                        number(ext.get("composite_price")), number(origin.get("observed_tte_seconds")))
    if any(v is None for v in (o, k, spot, tte)) or min(o, k, spot) <= 0 or not 0 <= tte <= 301.5:
        raise ValueError("rich_model:mandatory_price_or_tte_missing")
    age = number(ext.get("age_ns"))
    if age is None or not 0 <= age <= 3_000_000_000:
        raise ValueError("rich_model:external_age_invalid")
    values: dict[str, float | None] = dict.fromkeys(FEATURE_NAMES)
    om, basis = 10000 * math.log(o / k), 10000 * math.log(spot / o)
    fraction = max(0.0, min(1.0, (60 - tte) / 60))
    values.update(log_tte=math.log1p(tte), oracle_margin_bp=om,
                  spot_oracle_basis_bp=basis, spot_margin_bp=om + basis,
                  terminal_fraction=fraction, oracle_margin_time_scaled=om / math.sqrt(1 + tte),
                  basis_terminal_interaction=basis * fraction, external_age_ms=age / 1e6)
    micro = number(ext.get("composite_microprice"))
    values["microprice_shift_bp"] = 10000 * math.log(micro / spot) if micro is not None and micro > 0 else None
    for target, source, scale in (
        ("dispersion_bp", "dispersion_bps", 1), ("ofi", "aggregate_ofi", 1),
        ("trade_imbalance", "aggregate_trade_imbalance", 1),
        ("vol_fast_bp", "realized_vol_fast", 10000),
        ("vol_medium_bp", "realized_vol_medium", 10000),
        ("vol_slow_bp", "realized_vol_slow", 10000),
    ):
        v = number(ext.get(source)); values[target] = scale * v if v is not None else None
    vf, vs = values["vol_fast_bp"], values["vol_slow_bp"]
    if vf is not None and vs is not None and vf >= 0 and vs > 0:
        values["log_vol_ratio"] = math.log1p(vf / vs)
    values["jump_score"] = number(ext.get("jump_score"))
    if ext.get("feature_semantics_version") == HISTORY_SEMANTICS:
        availability = ext.get("return_history_available") or {}
        for horizon in ("50ms", "100ms", "250ms", "1s", "5s"):
            v = number(ext.get("return_" + horizon))
            if availability.get(horizon) is True and v is not None:
                values["return_" + horizon + "_bp"] = 10000 * v
    context = origin.get("external_context") or ext.get("external_context") or {}
    received = number(context.get("observed_wall_ns"))
    observed = number(origin.get("observed_ms", origin.get("timestamp_ms")))
    observed_ns = number(origin.get("observed_wall_ns")) or (observed * 1e6 if observed is not None else 0)
    # Context was captured and made available before this feature cut. A future
    # context is rejected even though the core price features are otherwise valid.
    if context:
        if received is None or observed is None or received > observed_ns:
            raise ValueError("rich_model:future_context")
        for key in (
            "binance_perp_basis_bp", "bybit_perp_basis_bp", "deribit_perp_basis_bp",
            "binance_perp_microprice_basis_bp", "binance_perp_depth_imbalance_l20", "binance_perp_trade_imbalance",
            "bybit_perp_microprice_basis_bp", "bybit_perp_depth_imbalance_l10", "bybit_perp_trade_flow",
            "deribit_perp_book_basis_bp", "deribit_trade_flow",
            "binance_funding", "bybit_funding", "deribit_funding_8h",
            "binance_open_interest_log", "binance_oi_velocity", "bybit_open_interest_log", "deribit_open_interest_log",
            "deribit_atm_iv", "deribit_vol_term_slope", "deribit_put_call_skew", "deribit_butterfly",
            "deribit_nearest_future_basis_bp", "deribit_historical_volatility",
            "binance_liquidation_signed_btc_per_s", "binance_liquidation_total_btc_per_s",
            "bybit_liquidation_signed_btc_per_s", "bybit_liquidation_total_btc_per_s",
        ):
            values[key] = number((context.get("features") or {}).get(key))
    return values


def contextual_features(runtime: dict[str, Any], usdm: dict[str, Any], options: dict[str, Any],
                        now_ns: int, liquidation_rates: dict[str, float] | None = None) -> dict[str, Any]:
    """Receive-time snapshots only; no remote requests and no synthetic clocks.

    Fast derivative context TTL=3s; slow OI TTL=20s; options TTL=60s.
    Cumulative liquidation counters are recorded upstream, NOT mistaken for
    instantaneous trading flow. Expired or future observations remain absent.
    """
    out: dict[str, Any] = {"observed_wall_ns": now_ns, "features": {}, "sources": {}, "exclusions": []}
    def put(source: str, value: dict[str, Any], received: Any, ttl: int) -> bool:
        r = number(received)
        if r is None or not 0 < r <= now_ns or now_ns - r > ttl:
            out["exclusions"].append(source + ":STALE_FUTURE_OR_UNKNOWN_RECEIVE_TIME")
            return False
        out["sources"][source] = {"receive_wall_ns": int(r), "age_ns": int(now_ns - r)}
        return True
    pub = number(runtime.get("timestamp_ns"))
    if pub is not None and 0 < pub <= now_ns and now_ns - pub <= 3_000_000_000:
        for source, prefix in (("BINANCE_USDM", "binance"), ("BYBIT_LINEAR", "bybit"), ("DERIBIT", "deribit")):
            ctx = next((r for r in runtime.get("derivative_contexts", []) if r.get("venue") == source), {})
            age = number(ctx.get("age_ns")); mask = ctx.get("valid_mask", 0)
            if (ctx.get("healthy") is not True or ctx.get("gap") is not False or age is None or age < 0
                    or not isinstance(mask, int) or isinstance(mask, bool)):
                continue
            if not put(source, ctx, pub - age, 3_000_000_000):
                continue
            mark, index = number(ctx.get("mark_price")), number(ctx.get("index_price"))
            if mask & 3 == 3 and mark is not None and index is not None and min(mark, index) > 0:
                out["features"][prefix + "_perp_basis_bp"] = 10000 * math.log(mark / index)
            funding = number(ctx.get("funding_rate"))
            if prefix != "deribit" and mask & 4 and funding is not None:
                out["features"][prefix + "_funding"] = funding
    spot = number(runtime.get("composite_price"))
    if pub is not None and 0 < pub <= now_ns and now_ns - pub <= 3_000_000_000 and spot is not None and spot > 0:
        bu = runtime.get("binance_usdm") if isinstance(runtime.get("binance_usdm"), dict) else {}
        if bu.get("valid") is True:
            micro = number(bu.get("perp_microprice")); bid_depth = number(bu.get("perp_bid_depth_l20")); ask_depth = number(bu.get("perp_ask_depth_l20"))
            if micro is not None and micro > 0:
                out["features"]["binance_perp_microprice_basis_bp"] = 10000 * math.log(micro / spot)
            if bid_depth is not None and ask_depth is not None and bid_depth + ask_depth > 0:
                out["features"]["binance_perp_depth_imbalance_l20"] = (bid_depth - ask_depth) / (bid_depth + ask_depth)
            flow = number(bu.get("perp_trade_imbalance"))
            if flow is not None: out["features"]["binance_perp_trade_imbalance"] = flow
        by_l2 = runtime.get("bybit_linear_l2") if isinstance(runtime.get("bybit_linear_l2"), dict) else {}
        if by_l2.get("valid") is True:
            micro = number(by_l2.get("microprice"))
            if micro is not None and micro > 0:
                out["features"]["bybit_perp_microprice_basis_bp"] = 10000 * math.log(micro / spot)
            imbalance = number(by_l2.get("imbalance_l10"))
            if imbalance is not None: out["features"]["bybit_perp_depth_imbalance_l10"] = imbalance
        by = runtime.get("bybit_linear") if isinstance(runtime.get("bybit_linear"), dict) else {}
        if by.get("valid") is True:
            flow = number(by.get("signed_trade_flow")); oi = number(by.get("open_interest"))
            if flow is not None: out["features"]["bybit_perp_trade_flow"] = flow
            if oi is not None and oi >= 0: out["features"]["bybit_open_interest_log"] = math.log1p(oi)
        de = runtime.get("deribit") if isinstance(runtime.get("deribit"), dict) else {}
        if de.get("valid") is True:
            bid, ask = number(de.get("best_bid")), number(de.get("best_ask"))
            if bid is not None and ask is not None and min(bid, ask) > 0 and ask >= bid:
                out["features"]["deribit_perp_book_basis_bp"] = 10000 * math.log((0.5 * (bid + ask)) / spot)
            flow = number(de.get("signed_trade_flow")); oi = number(de.get("open_interest")); f8 = number(de.get("funding_8h"))
            if flow is not None: out["features"]["deribit_trade_flow"] = flow
            if oi is not None and oi >= 0: out["features"]["deribit_open_interest_log"] = math.log1p(oi)
            if f8 is not None: out["features"]["deribit_funding_8h"] = f8
    if liquidation_rates:
        for key in (
            "binance_liquidation_signed_btc_per_s", "binance_liquidation_total_btc_per_s",
            "bybit_liquidation_signed_btc_per_s", "bybit_liquidation_total_btc_per_s",
        ):
            value = number(liquidation_rates.get(key))
            if value is not None: out["features"][key] = value
    u = usdm.get("latest") or {}
    if usdm.get("state") == "OPERATIONAL" and put("BINANCE_OI_REST", u,
            (u.get("open_interest_request") or {}).get("local_receive_wall_ns"), 20_000_000_000):
        velocity = number(u.get("open_interest_velocity")); oi = number(u.get("open_interest"))
        if velocity is not None: out["features"]["binance_oi_velocity"] = velocity
        if oi is not None and oi >= 0: out["features"]["binance_open_interest_log"] = math.log1p(oi)
    opt = options.get("latest") or {}
    if options.get("state") == "OPERATIONAL" and options.get("option_surface_valid") is True and put(
            "DERIBIT_OPTIONS_REST", opt,
            (opt.get("option_summary_request") or {}).get("local_receive_wall_ns"), 60_000_000_000):
        surface = opt.get("option_surface_features") if isinstance(opt.get("option_surface_features"), dict) else {}
        mapping = {
            "deribit_atm_iv": "atm_iv", "deribit_vol_term_slope": "vol_term_slope_iv_per_day",
            "deribit_put_call_skew": "put_call_skew_moneyness_proxy", "deribit_butterfly": "butterfly_iv_moneyness_proxy",
        }
        for target, source in mapping.items():
            value = number(surface.get(source))
            if value is not None: out["features"][target] = value
        future = opt.get("nearest_future") if isinstance(opt.get("nearest_future"), dict) else {}
        value = number(future.get("basis_bps_to_perpetual"))
        if value is not None: out["features"]["deribit_nearest_future_basis_bp"] = value
        hist = opt.get("historical_volatility")
        if isinstance(hist, list) and len(hist) >= 2:
            value = number(hist[1])
            if value is not None: out["features"]["deribit_historical_volatility"] = value
    return out


def design(raw: dict[str, Any], parameters: dict[str, Any]) -> list[float]:
    values = [1.0]
    for name, mean, scale in zip(parameters["feature_names"], parameters["means"], parameters["scales"]):
        value = number(raw.get(name))
        # Train-mean imputation + missingness bit, never an unlabeled zero.
        values.extend((0.0 if value is None else max(-8.0, min(8.0, (value - mean) / scale)),
                       float(value is None)))
    return values


def validate_parameters(artifact: Any) -> None:
    artifact.validate()
    p = artifact.parameters
    names = p.get("feature_names")
    if artifact.family != FAMILY or artifact.feature_schema_version != FEATURE_SCHEMA:
        raise ValueError("rich_model:family_or_schema")
    if not isinstance(names, list) or not names or len(set(names)) != len(names) or any(n not in FEATURE_NAMES for n in names):
        raise ValueError("rich_model:feature_names")
    if p.get("offset") not in ("market", "none") or p.get("missing_policy") != "TRAIN_MEAN_AND_EXPLICIT_INDICATOR":
        raise ValueError("rich_model:offset_or_missing_policy")
    for name, length in (("means", len(names)), ("scales", len(names)), ("coefficients", 1 + 2 * len(names))):
        vals = p.get(name)
        if not isinstance(vals, list) or len(vals) != length or any(number(v) is None for v in vals):
            raise ValueError("rich_model:parameter_shape")
    if any(v <= 0 for v in p["scales"]):
        raise ValueError("rich_model:scale")
    if artifact.probability_interval_diagnostics.get("validated") is not False:
        raise ValueError("rich_model:research_interval_claim")


def predict(artifact: Any, raw: dict[str, Any], market_probability: float) -> float:
    p = artifact.parameters
    pm = number(market_probability)
    if pm is None or not 0 <= pm <= 1:
        raise ValueError("rich_model:market_probability")
    z = logit(pm) if p["offset"] == "market" else 0.0
    z += sum(a * b for a, b in zip(p["coefficients"], design(raw, p)))
    return min(1.0 - 1e-9, max(1e-9, sigmoid(z)))


def is_paper_learning_fair(fair: dict[str, Any], code_sha: str | None = None) -> bool:
    """A frozen PAPER research point estimate; never real-order authority."""
    return bool(
        fair.get("valid") is True and fair.get("paper_exploration_learned") is True
        and fair.get("research_model") is True
        and fair.get("research_model_state") == "FROZEN_INFERENCE_ONLY"
        and fair.get("real_money_authority") is False
        and fair.get("probability_interval_validated") is False
        and fair.get("lower") == 0.0 and fair.get("upper") == 1.0
        and fair.get("family") == FAMILY
        and fair.get("inference_state") == "VALID_PAPER_LEARNED_PROBE"
        and fair.get("authority") == "SHADOW"
        and (fair.get("uses_polymarket_price_as_feature") is not True
             or fair.get("market_prior_causal_cut_valid") is True)
        and re.fullmatch(r"[0-9a-f]{64}", str(fair.get("probability_model_hash", "")))
        and str(fair.get("probability_model_id", "")).startswith(MODEL_PREFIX)
    )
