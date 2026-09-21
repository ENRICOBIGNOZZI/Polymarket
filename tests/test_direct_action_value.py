import json
import math

from research.walk_forward_v2.core import Ridge
from research.walk_forward_v3.bilateral import (
    build_bilateral_evidence,
    paired_l1_state,
)
from research.walk_forward_v3.risk_frontier import (
    empirical_var_cvar_from_losses,
    pareto_frontier,
    robust_pareto_frontier,
    scenario_risk_metrics,
    select_policy_under_risk_budget,
    nested_walk_forward_risk_frontier,
)
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    FrictionPolicy,
    action_execution_kernel,
    economics_from_execution_kernel,
    StreamingRidge,
    candidate_sizes,
    decision_action_sides,
    realized_action_economics,
    realized_action_value,
    residual_policy_friction,
    summarize_direct_action,
    merge_direct_action_summaries,
    bilateral_evidence_summary,
    configured_operational_latency_floor_ms,
    effective_signal_age_ms,
)


def row(market, *, signal=1.0, exit_bid=.55, depth=20.0, ask=.50, minimum=1.0):
    decision_ns = 1_789_921_800_000_000_000 + int(market.strip("m") or 0) * 1_000_000_000
    return {
        "decision_id": market + "-d",
        "market_id": market,
        "token_id": "yes-" + market,
        "asset": "BTC",
        "horizon": "M5",
        "decision_ns": decision_ns,
        "signal_age_ns": 1_000_000,
        "tte_ns": 110_000_000_000,
        "direction": 1,
        "signal_valid": True,
        "confirmed": True,
        "book_valid": True,
        "pretrigger": True,
        "bid": ask - .01,
        "ask": ask,
        "bid_quantity": depth,
        "quantity": depth,
        "minimum": minimum,
        "tick": .01,
        "fee_rate": 0.0,
        "fee_exponent": 1.0,
        "features": {
            "external.binance_return_100ms_bp": signal,
            "signal_age_ns": 1_000_000.0,
            "tte_ns": 110_000_000_000.0,
        },
        "arrivals": {
            "50": {
                "time_ns": decision_ns + 50_000_000,
                "bid": ask - .01,
                "ask": ask,
                "bid_quantity": depth,
                "quantity": depth,
                "epoch": 7,
            }
        },
        "targets": {
            "500": {
                "state": "OBSERVED",
                "arrival_bid": exit_bid,
                "arrival_ask": exit_bid + .01,
                "arrival_bid_quantity": depth,
                "arrival_quantity": depth,
                "observed_time_ns": decision_ns + 500_000_000,
            }
        },
    }



def bilateral_row(market="mb", *, no_depth=7.0):
    r = row(market, signal=1.0, depth=20.0, ask=.50)
    r["yes_token_id"] = "yes-" + market
    r["no_token_id"] = "no-" + market
    r["token_id"] = r["yes_token_id"]
    r["pair"] = {
        "state": "BILATERAL_EXECUTABLE_READY",
        "yes": {"bid": .49, "ask": .50, "bid_quantity": 20.0, "ask_quantity": 20.0},
        "no": {"bid": .48, "ask": .51, "bid_quantity": no_depth, "ask_quantity": no_depth},
    }
    r["arrivals"]["50"]["pair"] = {
        "state": "BILATERAL_EXECUTABLE_READY",
        "yes": {"bid": .49, "ask": .50, "bid_quantity": 20.0, "ask_quantity": 20.0},
        "no": {"bid": .48, "ask": .51, "bid_quantity": no_depth, "ask_quantity": no_depth},
    }
    r["targets"]["500"]["pair"] = {
        "state": "BILATERAL_EXECUTABLE_READY",
        "yes": {"bid": .45, "ask": .46, "bid_quantity": 20.0, "ask_quantity": 20.0},
        "no": {"bid": .56, "ask": .57, "bid_quantity": no_depth, "ask_quantity": no_depth},
    }
    return r

def test_candidate_sizes_obey_l1_depth_venue_minimum_and_notional_cap():
    r = row("m1", depth=100.0, ask=.50, minimum=5.0)
    sizes = candidate_sizes(
        r,
        size_grid=(1.0, 5.0, 10.0, 20.0, 40.0, 80.0),
        hard_order_notional=10.0,
        max_sizes=10,
    )
    assert sizes == [5.0, 10.0, 20.0]
    assert max(sizes) * r["ask"] <= 10.0


def test_realized_action_value_uses_total_net_pnl_not_per_share():
    r = row("m2", exit_bid=.55, depth=20.0)
    value, state = realized_action_value(
        r, size=10.0, horizon_ms=500, latency_ms=50,
        hard_order_notional=100.0)
    assert state == "OBSERVED_FULL_FILL"
    assert math.isclose(value, .50, abs_tol=1e-12)


def test_observed_no_fill_is_zero_but_missing_arrival_is_censored():
    r = row("m3")
    r["arrivals"]["50"]["ask"] = .51
    value, state = realized_action_value(
        r, size=5.0, horizon_ms=500, latency_ms=50)
    assert value == 0.0
    assert state == "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"

    r = row("m4")
    r["arrivals"].pop("50")
    value, state = realized_action_value(
        r, size=5.0, horizon_ms=500, latency_ms=50)
    assert value is None
    assert state.startswith("UNAVAILABLE")


def test_direct_model_selects_size_from_action_value_and_keeps_no_trade():
    rows = []
    # Alternate regimes through time so fit/scale/calibration blocks all see
    # both positive and negative examples.
    for index in range(24):
        positive = index % 2 == 0
        rows.append(row(
            "m" + str(index + 10),
            signal=2.0 if positive else -2.0,
            exit_bid=.56 if positive else .44,
            depth=20.0,
        ))

    model = DirectActionValueModel(
        size_grid=(1.0, 5.0, 10.0, 20.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        hard_order_notional=100.0,
        max_sizes_per_state=4,
    ).fit(rows)

    good = row("m100", signal=2.0, exit_bid=.56, depth=20.0)
    selected = model.select_action(good, latency_ms=50)
    assert selected["action"] == "TRADE"
    assert good["minimum"] <= selected["size"] <= good["quantity"]
    assert selected["quantity_optimizer"] == "GLOBAL_PIECEWISE_POLYLOG_CRITICAL_POINTS"
    assert selected["calibrated_lower_value"] > 0

    bad = row("m101", signal=-2.0, exit_bid=.44, depth=20.0)
    rejected = model.select_action(bad, latency_ms=50)
    assert rejected["action"] == "NO_TRADE"
    assert rejected["calibrated_lower_value"] == 0.0


def test_model_receipt_explicitly_disclaims_mean_covariance_and_l2_impact():
    rows = [
        row("m" + str(index + 200), signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44)
        for index in range(90)
    ]
    model = DirectActionValueModel(
        size_grid=(1.0, 5.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
    ).fit(rows)
    receipt = model.training_receipt
    assert receipt["mean_covariance_estimation"] is False
    assert receipt["capacity_scope"] == "L1_ONLY_NO_COUNTERFACTUAL_IMPACT_BEYOND_VISIBLE_DEPTH"
    assert receipt["calibration_state"] == "TEMPORAL_MARKET_BLOCK_SPLIT_CONFORMAL"
    assert receipt["calibration_holdout_excluded_from_mean_fit"] is True
    assert receipt["mean_fit_scope"] == "PRE_CALIBRATION_MARKETS_ONLY"
    assert receipt["calibration_holdouts_disjoint"] is True
    assert (
        receipt["action_calibration_market_count"]
        == receipt["calibration_market_count"]
    )
    assert receipt["selection_calibration_mode"] == "PREQUENTIAL"
    assert receipt["selection_scores_oos_when_generated"] is True
    assert receipt["selection_score_blocks_may_enter_final_mean_fit"] is True
    assert receipt["selection_calibration_state"] in (
        "PREQUENTIAL_SELECTED_POLICY_ONE_SIDED",
        "INSUFFICIENT_PREQUENTIAL_SELECTION_CALIBRATION",
    )



def test_execution_friction_decomposition_reconciles_without_double_counting():
    r = row("m400", exit_bid=.55, depth=20.0)
    economics, state = realized_action_economics(
        r, size=10.0, horizon_ms=500, latency_ms=50)
    assert state == "OBSERVED_FULL_FILL"
    assert economics["frictions_embedded_in_cash_pnl"] is True
    # mid0=.495, future mid=.555, so ideal alpha=.60. Entry and exit
    # half-spreads are .05 each. Fees and latency drift are zero.
    assert math.isclose(economics["ideal_midpoint_alpha"], .60, abs_tol=1e-12)
    assert math.isclose(economics["decision_half_spread_cost"], .05, abs_tol=1e-12)
    assert math.isclose(economics["exit_half_spread_cost"], .05, abs_tol=1e-12)
    assert math.isclose(economics["latency_price_drift_cost"], 0.0, abs_tol=1e-12)
    assert math.isclose(economics["exit_liquidity_shortfall_cost"], 0.0, abs_tol=1e-12)
    assert economics["fully_exitable_at_horizon"] is True
    reconstructed = (
        economics["ideal_midpoint_alpha"]
        - economics["decision_half_spread_cost"]
        - economics["latency_price_drift_cost"]
        - economics["exit_half_spread_cost"]
        - economics["exit_liquidity_shortfall_cost"]
        - economics["total_fees"]
    )
    assert math.isclose(reconstructed, economics["cash_pnl"], abs_tol=1e-12)


def test_residual_policy_friction_penalizes_capital_and_existing_concentration():
    r = row("m401")
    action = {"notional": 50.0, "exit_horizon_ms": 500}
    policy = FrictionPolicy(
        capital_charge_bps_per_second=10.0,
        asset_concentration_lambda=.01,
        common_factor_concentration_lambda=.02,
        uncertainty_aversion=1.0,
    )
    flat = residual_policy_friction(
        action, r, portfolio_state={}, capital_budget=1000.0,
        friction_policy=policy)
    concentrated = residual_policy_friction(
        action, r,
        portfolio_state={
            "asset_signed_notional": {"BTC": 200.0},
            "common_factor_signed_notional": 400.0,
        },
        capital_budget=1000.0,
        friction_policy=policy)
    assert flat["capital_lock_penalty"] > 0
    assert concentrated["asset_concentration_penalty"] > flat["asset_concentration_penalty"]
    assert (
        concentrated["common_factor_concentration_penalty"]
        > flat["common_factor_concentration_penalty"]
    )
    assert concentrated["total_residual_friction"] > flat["total_residual_friction"]


def test_model_receipt_lists_execution_frictions_inside_target_and_residuals_outside():
    rows = [
        row("m" + str(index + 500), signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44)
        for index in range(90)
    ]
    model = DirectActionValueModel(
        size_grid=(1.0, 5.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        friction_policy=FrictionPolicy(
            capital_charge_bps_per_second=1.0,
            asset_concentration_lambda=.001,
            common_factor_concentration_lambda=.002,
        ),
    ).fit(rows)
    receipt = model.training_receipt
    embedded = set(receipt["execution_frictions_in_training_target"])
    assert "entry_taker_fee" in embedded
    assert "exit_taker_fee" in embedded
    assert "exit_l1_capacity_and_zero_value_residual_lower_bound" in embedded
    assert "fill_and_no_fill" in embedded
    assert "post_signal_latency_price_drift_via_arrival_book" in embedded
    assert receipt["residual_policy_frictions"]["capital_charge_bps_per_second"] == 1.0
    assert receipt["mean_covariance_estimation"] is False



def test_streaming_ridge_matches_materialized_ridge_exactly_on_small_problem():
    rows = []
    for index in range(60):
        features = {"a": float(index % 7)}
        if index % 4:
            features["b"] = float((index * 3) % 11)
        rows.append({
            "features": features,
            "target": 1.5 + .7 * features["a"] - .2 * features.get("b", 5.0),
        })
    names = ("a", "b")
    materialized = Ridge(names, ridge=3.0).fit(
        rows, lambda r: r["target"])
    streaming = StreamingRidge(
        names, ridge=3.0, batch_size=7).fit_factory(
            lambda: iter(rows), lambda r: r["target"])
    for name in names:
        assert math.isclose(
            materialized.center[name], streaming.center[name],
            rel_tol=1e-12, abs_tol=1e-12)
        assert math.isclose(
            materialized.scale[name], streaming.scale[name],
            rel_tol=1e-12, abs_tol=1e-12)
    for left, right in zip(materialized.beta, streaming.beta):
        assert math.isclose(
            float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
    for item in rows:
        assert math.isclose(
            materialized.predict(item), streaming.predict(item),
            rel_tol=1e-9, abs_tol=1e-9)


def test_direct_action_uses_all_precalibration_states_with_p_squared_memory():
    rows = [
        row("m" + str(index + 700), signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44)
        for index in range(120)
    ]
    model = DirectActionValueModel(
        size_grid=(1.0, 5.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        streaming_batch_size=11,
    ).fit(rows)
    receipt = model.training_receipt
    assert receipt["training_states_total"] == 120
    assert receipt["training_states_used"] == 96
    assert receipt["training_markets_used"] == 96
    assert receipt["calibration_market_count"] == 24
    assert receipt["action_calibration_market_count"] == 24
    assert receipt["selection_calibration_mode"] == "PREQUENTIAL"
    assert receipt["prequential_calibration_blocks"] == 2
    assert receipt["selection_prequential_score_markets"] >= 0
    assert receipt["selection_prequential_observed_markets"] >= 0
    assert receipt["calibration_holdouts_disjoint"] is True
    assert receipt["calibration_holdout_excluded_from_mean_fit"] is True
    assert receipt["training_state_cap"] is None
    assert receipt["action_targets"] >= 120
    assert receipt["matrix_strategy"] == (
        "ONE_PASS_SUFFICIENT_STATISTICS_THEN_P_X_P_NORMAL_EQUATIONS")
    assert receipt["gram_matrix_bytes"] == (
        receipt["design_dimension"] ** 2 * 8)
    assert receipt["gram_matrix_bytes"] < 1_000_000



def test_continuous_quantity_optimizer_finds_known_interior_global_maximum():
    import numpy as np

    r = row("m900", signal=1.0, depth=20.0, ask=.50, minimum=1.0)
    model = DirectActionValueModel(
        size_grid=(1.0, 20.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        friction_policy=FrictionPolicy(uncertainty_aversion=0.0),
    )
    model._configure_levels([r])

    class DummyModel:
        def __init__(self, names):
            self.names = tuple(names)
            self.center = {name: 0.0 for name in self.names}
            self.scale = {name: 1.0 for name in self.names}
            self.beta = np.zeros(1 + 2 * len(self.names), dtype=float)
            self.beta[1 + self.names.index("action.size")] = 1.0
            self.beta[1 + self.names.index("action.size2")] = -0.1

        def predict(self, record):
            features = record["features"]
            value = float(self.beta[0])
            for index, name in enumerate(self.names):
                raw = features.get(name)
                if isinstance(raw, (int, float)) and math.isfinite(raw):
                    value += float(self.beta[1 + index]) * float(raw)
                else:
                    value += float(self.beta[1 + len(self.names) + index])
            return value

    model.mean_model = DummyModel(model.model_feature_names)
    model.scale_model = None
    model.uncertainty_floor = 0.0
    model.calibration_multiplier = 1.0
    model.fitted = True

    selected = model.select_action(
        r, latency_ms=50, available_capital=100.0,
        capital_budget=1000.0)
    assert selected["action"] == "TRADE"
    assert math.isclose(selected["size"], 5.0, abs_tol=1e-8)
    assert math.isclose(
        selected["predicted_total_net_cash_pnl"], 2.5, abs_tol=1e-8)
    assert selected["quantity_optimizer"] == "GLOBAL_PIECEWISE_POLYLOG_CRITICAL_POINTS"


def test_continuous_quantity_optimizer_respects_available_capital_and_depth():
    import numpy as np

    r = row("m901", signal=1.0, depth=100.0, ask=.50, minimum=1.0)
    model = DirectActionValueModel(
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        friction_policy=FrictionPolicy(uncertainty_aversion=0.0),
    )
    model._configure_levels([r])

    class IncreasingModel:
        def __init__(self, names):
            self.names = tuple(names)
            self.center = {name: 0.0 for name in self.names}
            self.scale = {name: 1.0 for name in self.names}
            self.beta = np.zeros(1 + 2 * len(self.names), dtype=float)
            self.beta[1 + self.names.index("action.size")] = 1.0

        def predict(self, record):
            return float(record["features"]["action.size"])

    model.mean_model = IncreasingModel(model.model_feature_names)
    model.scale_model = None
    model.uncertainty_floor = 0.0
    model.calibration_multiplier = 1.0
    model.fitted = True

    # $3 available at ask=.50 => q <= 6 even though visible depth is 100.
    selected = model.select_action(
        r, latency_ms=50, available_capital=3.0,
        capital_budget=1000.0)
    assert selected["action"] == "TRADE"
    assert math.isclose(selected["size"], 6.0, abs_tol=1e-8)
    assert math.isclose(selected["notional"], 3.0, abs_tol=1e-8)



def bilateral_native_row(kind, *, horizon=None, quantities=True, pair_valid=True,
                         decision_ns=1_000_000_000, observed_ns=None,
                         yes_ask=4100, no_ask=6100):
    if observed_ns is None:
        observed_ns = (
            decision_ns if kind == 2
            else decision_ns + int(horizon or 0) * 1_000_000
        )
    value = {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True,
        "execution_authority": False,
        "kind": kind,
        "server_id": "server",
        "run_id": "run",
        "capture_id": "capture",
        "market_id": "market",
        "token_id": "yes-token",
        "asset": "BTC",
        "horizon": "M5",
        "repricing_origin_signal_version": 42,
        "signal_version": 42,
        "decision_monotonic_ns": decision_ns,
        "observed_monotonic_ns": observed_ns,
        "close_monotonic_ns": decision_ns + 5_000_000_000,
        "close_wall_ns": 1_800_000_000_000_000_000,
        "decision_wall_ns": 1_799_999_995_000_000_000,
        "signal_age_ns": 1_000_000,
        "tte_ns": 5_000_000_000,
        "direction": 1,
        "signal_valid": True,
        "confirmed_non_opposing": True,
        "book_valid": True,
        "repricing_pair_valid": pair_valid,
        "yes_bid_e4": 4000,
        "yes_ask_e4": yes_ask,
        "no_bid_e4": 5900,
        "no_ask_e4": no_ask,
        "minimum_order_microunits": 1_000_000,
        "fee_rate": 0.01,
        "fee_exponent": 1.0,
    }
    if kind == 6:
        value["repricing_horizon_ms"] = int(horizon)
    if quantities:
        value.update({
            "yes_bid_quantity": 3_000_000,
            "yes_ask_quantity": 4_000_000,
            "no_bid_quantity": 5_000_000,
            "no_ask_quantity": 6_000_000,
        })
    return value


def test_bilateral_l1_requires_depth_not_only_paired_prices():
    prices_only = bilateral_native_row(2, quantities=False)
    state = paired_l1_state(prices_only)
    assert state["state"] == "PRICES_ONLY"
    assert state["yes"]["ask"] == .41
    assert state["no"]["ask"] == .61
    assert state["yes"]["ask_quantity"] is None

    ready = paired_l1_state(bilateral_native_row(2, quantities=True))
    assert ready["state"] == "BILATERAL_EXECUTABLE_READY"
    assert ready["yes"]["ask_quantity"] == 4.0
    assert ready["no"]["bid_quantity"] == 5.0

    unavailable = paired_l1_state(
        bilateral_native_row(2, quantities=True, pair_valid=False))
    assert unavailable["state"] == "UNAVAILABLE"


def test_bilateral_builder_never_imputes_missing_future_depth(tmp_path):
    path = tmp_path / "bilateral.jsonl"
    rows = [
        bilateral_native_row(2, quantities=True),
        bilateral_native_row(6, horizon=100, quantities=False),
        bilateral_native_row(6, horizon=250, quantities=True),
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in rows),
        encoding="utf-8",
    )
    records, summary = build_bilateral_evidence([path])
    assert summary["origins"] == 1
    assert summary["origins_bilateral_ready"] == 1
    assert summary["records_with_bilateral_future"] == 1
    assert summary["missing_depth_is_never_imputed"] is True
    assert summary["counterfactual_side_executable"] is True
    assert len(records) == 1
    assert "100" not in records[0]["future"]
    assert records[0]["future"]["250"]["paired_l1"]["state"] == (
        "BILATERAL_EXECUTABLE_READY"
    )


def test_bilateral_builder_censors_origin_without_decision_depth(tmp_path):
    path = tmp_path / "bilateral-prices-only.jsonl"
    rows = [
        bilateral_native_row(2, quantities=False),
        bilateral_native_row(6, horizon=250, quantities=True),
    ]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in rows),
        encoding="utf-8",
    )
    records, summary = build_bilateral_evidence([path])
    assert records == []
    assert summary["origins"] == 1
    assert summary["origins_bilateral_ready"] == 0
    assert summary["counterfactual_side_executable"] is False


def test_bilateral_builder_rejects_conflicting_future_pair(tmp_path):
    path = tmp_path / "bilateral-conflict.jsonl"
    first = bilateral_native_row(6, horizon=250, quantities=True)
    second = bilateral_native_row(
        6, horizon=250, quantities=True, yes_ask=4200)
    rows = [bilateral_native_row(2, quantities=True), first, second]
    path.write_text(
        "".join(json.dumps(value) + "\n" for value in rows),
        encoding="utf-8",
    )
    import pytest
    with pytest.raises(ValueError, match="CONFLICTING_BILATERAL_LABEL"):
        build_bilateral_evidence([path])



def test_bilateral_realized_economics_uses_chosen_side_prices_and_depth():
    r = bilateral_row("m950", no_depth=7.0)
    assert decision_action_sides(r) == ("YES", "NO")

    no_value, no_state = realized_action_value(
        r, size=7.0, horizon_ms=500, latency_ms=50, side="NO")
    yes_value, yes_state = realized_action_value(
        r, size=7.0, horizon_ms=500, latency_ms=50, side="YES")
    assert no_state == "OBSERVED_FULL_FILL"
    assert yes_state == "OBSERVED_FULL_FILL"
    assert math.isclose(no_value, 7.0 * (.56 - .51), abs_tol=1e-12)
    assert math.isclose(yes_value, 7.0 * (.45 - .50), abs_tol=1e-12)
    assert no_value > 0 > yes_value

    # Opposite-side visible depth is 7 shares, so an 8-share NO action is not admissible.
    too_large, state = realized_action_value(
        r, size=8.0, horizon_ms=500, latency_ms=50, side="NO")
    assert too_large is None
    assert state == "INSUFFICIENT_DECISION_DEPTH"


def test_bilateral_quantity_candidates_use_side_specific_ask_and_capital():
    r = bilateral_row("m951", no_depth=20.0)
    # At NO ask=.51, $3 only funds 5.882... shares.
    sizes = candidate_sizes(
        r, side="NO", size_grid=(1.0, 5.0, 6.0, 10.0),
        hard_order_notional=100.0, available_capital=3.0, max_sizes=10)
    assert max(sizes) <= 3.0 / .51 + 1e-12
    assert any(math.isclose(q, 3.0 / .51, rel_tol=0, abs_tol=1e-12) for q in sizes)


def test_direct_policy_can_choose_no_side_when_its_learned_value_is_higher():
    import numpy as np

    r = bilateral_row("m952", no_depth=20.0)
    model = DirectActionValueModel(
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        friction_policy=FrictionPolicy(uncertainty_aversion=0.0),
    )
    model._configure_levels([r])

    class SideModel:
        def __init__(self, names):
            self.names = tuple(names)
            self.center = {name: 0.0 for name in self.names}
            self.scale = {name: 1.0 for name in self.names}
            self.beta = np.zeros(1 + 2 * len(self.names), dtype=float)
            # NO has side_sign=-1, so a negative coefficient makes NO superior.
            self.beta[1 + self.names.index("action.side_sign")] = -1.0
            self.beta[1 + self.names.index("action.size")] = .02

        def predict(self, record):
            value = float(self.beta[0])
            for index, name in enumerate(self.names):
                raw = record["features"].get(name)
                if isinstance(raw, (int, float)) and math.isfinite(raw):
                    value += float(self.beta[1 + index]) * float(raw)
                else:
                    value += float(self.beta[1 + len(self.names) + index])
            return value

    model.mean_model = SideModel(model.model_feature_names)
    model.scale_model = None
    model.uncertainty_floor = 0.0
    model.calibration_multiplier = 1.0
    model.fitted = True

    selected = model.select_action(
        r, latency_ms=50, available_capital=100.0, capital_budget=1000.0)
    assert selected["action"] == "TRADE"
    assert selected["side"] == "NO"
    assert math.isclose(
        selected["notional"], selected["size"] * .51, rel_tol=0, abs_tol=1e-10)


def test_legacy_state_without_bilateral_depth_never_invents_opposite_side():
    r = row("m953")
    assert decision_action_sides(r) == ("SELECTED",)



def _risk_outcome(index, pnl, *, censored=False, horizon_ms=500):
    return {
        "market_id": "risk-" + str(index),
        "asset": "BTC" if index % 2 == 0 else "ETH",
        "side": "YES" if index % 2 == 0 else "NO",
        "decision_ns": 1_000_000_000 + index * 2_000_000_000,
        "action": "TRADE",
        "size": 5.0,
        "notional": 2.5,
        "exit_horizon_ms": horizon_ms,
        "realized_pnl": None if censored else float(pnl),
        "censored_worst_case_pnl": -2.5 if censored else float(pnl),
        "censored_worst_case_loss_bound": 2.5 if censored else max(0.0, -float(pnl)),
        "replay_max_active_positions": 3,
        "replay_max_gross_notional": 75.0,
    }


def test_empirical_cvar_uses_upper_loss_tail_without_gaussian_assumption():
    result = empirical_var_cvar_from_losses([-4.0, -1.0, 2.0, 3.0], .95)
    assert result["var"] == 3.0
    assert result["cvar"] == 3.0
    assert result["tail_count"] == 1


def test_scenario_risk_metrics_cluster_exits_and_measure_dollar_drawdown():
    outcomes = [
        _risk_outcome(0, 1.0),
        _risk_outcome(1, -2.0),
        _risk_outcome(2, -3.0),
        _risk_outcome(3, 4.0),
    ]
    risk = scenario_risk_metrics(outcomes, block_ms=500)
    assert risk["state"] == "ALL_SELECTED_TRADES_OBSERVED"
    assert risk["scenario_blocks"] == 4
    assert risk["total_observed_net_pnl"] == 0.0
    assert risk["worst_scenario_pnl"] == -3.0
    assert risk["tail_loss"]["0.95"]["cvar"] == 3.0
    assert risk["max_drawdown"] == 5.0
    assert risk["max_active_positions"] == 3
    assert risk["max_gross_notional"] == 75.0
    assert risk["by_asset_pnl"]["BTC"] == -2.0
    assert risk["by_asset_pnl"]["ETH"] == 2.0


def test_scenario_risk_metrics_fail_closed_on_censored_selected_trade():
    outcomes = [
        _risk_outcome(0, 1.0),
        _risk_outcome(1, -1.0, censored=True),
    ]
    risk = scenario_risk_metrics(outcomes, block_ms=500)
    assert risk["state"] == "PARTIAL_CENSORED_NO_PROMOTION_CLAIM"
    assert risk["selected_trades"] == 2
    assert risk["observed_selected_trades"] == 1
    assert risk["censored_selected_trades"] == 1
    robust = risk["censored_worst_case"]
    assert robust["state"] == "READY"
    assert robust["bounded_censored_trades"] == 1
    assert math.isclose(
        robust["total_net_pnl_lower_bound"], -1.5, abs_tol=1e-12)
    assert robust["tail_loss"]["0.95"]["cvar"] >= 0


def test_pareto_frontier_keeps_more_pnl_only_when_tail_risk_is_not_worse():
    def entry(name, pnl, cvar, drawdown):
        return {
            "policy_id": name,
            "risk": {
                "state": "ALL_SELECTED_TRADES_OBSERVED",
                "total_observed_net_pnl": pnl,
                "max_drawdown": drawdown,
                "tail_loss": {"0.95": {"cvar": cvar}},
            },
        }

    entries = [
        entry("dominated", 8.0, 4.0, 5.0),
        entry("safe", 8.0, 2.0, 3.0),
        entry("profit", 12.0, 3.0, 4.0),
        {
            "policy_id": "censored",
            "risk": {
                "state": "PARTIAL_CENSORED_NO_PROMOTION_CLAIM",
                "total_observed_net_pnl": 100.0,
                "max_drawdown": 0.0,
                "tail_loss": {"0.95": {"cvar": 0.0}},
            },
        },
    ]
    frontier = set(pareto_frontier(entries))
    assert "dominated" not in frontier
    assert frontier == {"safe", "profit"}



def test_risk_budget_selection_requires_explicit_budget_and_uses_validation_only():
    frontier = {
        "entries": [
            {
                "policy_id": "loose",
                "risk": {
                    "state": "ALL_SELECTED_TRADES_OBSERVED",
                    "total_observed_net_pnl": 12.0,
                    "max_drawdown": 6.0,
                    "tail_loss": {"0.95": {"cvar": 5.0}},
                },
            },
            {
                "policy_id": "safe",
                "risk": {
                    "state": "ALL_SELECTED_TRADES_OBSERVED",
                    "total_observed_net_pnl": 8.0,
                    "max_drawdown": 2.0,
                    "tail_loss": {"0.95": {"cvar": 1.5}},
                },
            },
        ]
    }
    none = select_policy_under_risk_budget(frontier)
    assert none["state"] == "NO_RISK_BUDGET_NO_AUTOMATIC_SELECTION"
    assert none["policy_id"] is None

    chosen = select_policy_under_risk_budget(
        frontier, max_cvar95=2.0, max_drawdown=3.0)
    assert chosen["state"] == "VALIDATION_POLICY_SELECTED"
    assert chosen["policy_id"] == "safe"

    impossible = select_policy_under_risk_budget(
        frontier, max_cvar95=1.0, max_drawdown=1.0)
    assert impossible["state"] == "NO_POLICY_MEETS_VALIDATION_RISK_BUDGET"
    assert impossible["policy_id"] is None



def test_nested_risk_frontier_never_uses_outer_oos_to_choose_policy():
    rows = []
    for index in range(120):
        positive = index % 3 != 0
        item = row(
            "m" + str(index + 2000),
            signal=2.0 if positive else -2.0,
            exit_bid=.56 if positive else .44,
            depth=20.0,
        )
        item["information_end_ns"] = item["decision_ns"]
        item["label"] = None
        item["label_information_ns"] = None
        rows.append(item)

    grid = [
        {
            "policy_id": "baseline",
            "friction_policy": FrictionPolicy(
                uncertainty_aversion=1.0,
                asset_concentration_lambda=0.0,
                common_factor_concentration_lambda=0.0,
            ),
        },
        {
            "policy_id": "conservative",
            "friction_policy": FrictionPolicy(
                uncertainty_aversion=2.0,
                asset_concentration_lambda=.01,
                common_factor_concentration_lambda=.01,
            ),
        },
    ]
    result = nested_walk_forward_risk_frontier(
        rows,
        desired_folds=2,
        inner_desired_folds=2,
        policy_grid=grid,
        latency_ms=50,
        capital_budget=1000.0,
        model_kwargs={
            "size_grid": (1.0, 5.0),
            "action_horizons_ms": (500,),
            "train_latencies_ms": (50,),
            "streaming_batch_size": 32,
        },
    )
    assert result["state"] == "READY"
    assert result["selection_semantics"] == (
        "INNER_VALIDATION_ONLY_OUTER_OOS_NEVER_USED_TO_CHOOSE_RISK_POLICY")
    assert result["folds"]
    for fold in result["folds"]:
        selection = fold["selection"]
        if selection["state"] == "NO_RISK_BUDGET_NO_AUTOMATIC_SELECTION":
            assert selection["policy_id"] is None
            assert selection["qualification"] == (
                "VALIDATION_PARETO_SET_NO_SINGLE_SELECTION")


def test_execution_kernel_is_economically_identical_for_all_candidate_sizes():
    r = row("m980", exit_bid=.55, depth=20.0)
    # Arrival depth below decision depth creates both full and partial fills.
    r["arrivals"]["50"]["quantity"] = 6.0
    kernel, state = action_execution_kernel(
        r, horizon_ms=500, latency_ms=50)
    assert state == "OBSERVED_EXECUTABLE"
    assert kernel is not None

    for size in (1.0, 5.0, 6.0, 10.0, 20.0):
        direct, direct_state = realized_action_economics(
            r, size=size, horizon_ms=500, latency_ms=50)
        cached, cached_state = economics_from_execution_kernel(kernel, size)
        assert direct_state == cached_state
        assert direct.keys() == cached.keys()
        for key in direct:
            left, right = direct[key], cached[key]
            if isinstance(left, float):
                assert math.isclose(left, right, rel_tol=0, abs_tol=1e-12), key
            else:
                assert left == right, key


def test_execution_kernel_preserves_zero_chase_no_fill_for_every_size():
    r = row("m981", exit_bid=.55, depth=20.0)
    r["arrivals"]["50"]["ask"] = .51
    kernel, state = action_execution_kernel(
        r, horizon_ms=500, latency_ms=50)
    assert state == "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
    for size in (1.0, 5.0, 20.0):
        economics, observed = economics_from_execution_kernel(kernel, size)
        assert observed == "OBSERVED_NO_FILL_LIMIT_NOT_TOUCHED"
        assert economics["cash_pnl"] == 0.0
        assert economics["filled"] == 0.0



def test_continuous_q_summary_uses_distributions_not_unbounded_size_keys():
    outcomes = []
    for index in range(50):
        outcomes.append({
            "action": "TRADE",
            "asset": "BTC",
            "side": "YES" if index % 2 == 0 else "NO",
            "size": 1.0 + index / 7.0,
            "notional": 0.5 + index / 14.0,
            "exit_horizon_ms": 500 if index % 3 else 250,
            "realized_pnl": 0.01 * (index - 20),
            "policy_utility": 0.1,
            "total_residual_friction": 0.01,
            "uncertainty_penalty": 0.02,
            "replay_max_active_positions": 3,
            "replay_max_gross_notional": 12.0,
        })
    summary = summarize_direct_action(outcomes)
    assert "by_size" not in summary
    assert summary["selected_size_distribution"]["count"] == 50
    assert summary["selected_size_distribution"]["p50"] is not None
    assert summary["by_side"]["YES"]["trades"] == 25
    assert summary["by_side"]["NO"]["trades"] == 25


def test_fold_summary_merge_preserves_exact_counts_and_pnl():
    first = summarize_direct_action([
        {
            "action": "TRADE", "asset": "BTC", "side": "YES",
            "size": 5.0, "notional": 2.5, "exit_horizon_ms": 250,
            "realized_pnl": 0.4, "policy_utility": 0.2,
            "total_residual_friction": 0.01, "uncertainty_penalty": 0.02,
            "replay_max_active_positions": 1, "replay_max_gross_notional": 2.5,
        },
        {"action": "NO_TRADE", "realized_pnl": 0.0},
    ])
    second = summarize_direct_action([
        {
            "action": "TRADE", "asset": "ETH", "side": "NO",
            "size": 7.0, "notional": 3.5, "exit_horizon_ms": 500,
            "realized_pnl": -0.1, "policy_utility": 0.1,
            "total_residual_friction": 0.03, "uncertainty_penalty": 0.04,
            "replay_max_active_positions": 2, "replay_max_gross_notional": 5.0,
        },
    ])
    merged = merge_direct_action_summaries([first, second])
    assert merged["opportunities"] == 3
    assert merged["selected_trades"] == 2
    assert merged["no_trade"] == 1
    assert math.isclose(merged["total_observed_net_pnl"], 0.3, abs_tol=1e-12)
    assert math.isclose(merged["mean_observed_net_pnl"], 0.15, abs_tol=1e-12)
    assert merged["max_active_positions"] == 2
    assert merged["by_asset"]["BTC"]["trades"] == 1
    assert merged["by_asset"]["ETH"]["trades"] == 1



def test_exit_horizon_respects_observed_bid_capacity_and_values_residual_at_zero():
    r = row("m982", exit_bid=.55, depth=20.0)
    r["targets"]["500"]["arrival_bid_quantity"] = 4.0
    economics, state = realized_action_economics(
        r, size=10.0, horizon_ms=500, latency_ms=50)
    assert state == "OBSERVED_FULL_FILL"
    assert economics["filled"] == 10.0
    assert economics["exit_filled"] == 4.0
    assert economics["residual_inventory"] == 6.0
    assert economics["fully_exitable_at_horizon"] is False
    assert economics["residual_terminal_value_assumption"] == "ZERO_WORST_CASE"
    assert math.isclose(economics["cash_pnl"], -2.8, abs_tol=1e-12)
    # Future mid=.555; six unliquidated shares lose that entire executable-mark
    # reference under the static zero-residual lower-bound target.
    assert math.isclose(
        economics["exit_liquidity_shortfall_cost"], 6.0 * .555,
        abs_tol=1e-12)
    reconstructed = (
        economics["ideal_midpoint_alpha"]
        - economics["decision_half_spread_cost"]
        - economics["latency_price_drift_cost"]
        - economics["exit_half_spread_cost"]
        - economics["exit_liquidity_shortfall_cost"]
        - economics["total_fees"]
    )
    assert math.isclose(reconstructed, economics["cash_pnl"], abs_tol=1e-12)


def test_bilateral_exit_capacity_is_side_specific():
    r = bilateral_row("m983", no_depth=7.0)
    # Entry/arrival can execute five NO shares, but the future NO bid can
    # liquidate only three. This isolates exit capacity from entry capacity.
    r["targets"]["500"]["pair"]["no"]["bid_quantity"] = 3.0
    yes, yes_state = realized_action_economics(
        r, size=5.0, horizon_ms=500, latency_ms=50, side="YES")
    no, no_state = realized_action_economics(
        r, size=5.0, horizon_ms=500, latency_ms=50, side="NO")
    assert yes_state == no_state == "OBSERVED_FULL_FILL"
    assert yes["exit_filled"] == 5.0
    assert yes["residual_inventory"] == 0.0
    assert no["exit_filled"] == 3.0
    assert no["residual_inventory"] == 2.0
    assert no["fully_exitable_at_horizon"] is False



def test_bilateral_evidence_summary_never_promotes_prices_only_to_executable():
    legacy = row("m984")
    ready = bilateral_row("m985", no_depth=7.0)
    prices_only = bilateral_row("m986", no_depth=7.0)
    prices_only["pair"] = {
        "state": "PRICES_ONLY",
        "yes": {"bid": .49, "ask": .50, "bid_quantity": None, "ask_quantity": None},
        "no": {"bid": .48, "ask": .51, "bid_quantity": None, "ask_quantity": None},
    }
    result = bilateral_evidence_summary([legacy, ready, prices_only])
    states = result["decision_pair_states"]
    assert states["BILATERAL_EXECUTABLE_READY"] == 1
    assert states["PRICES_ONLY"] == 1
    assert states["UNAVAILABLE"] == 1
    assert math.isclose(result["bilateral_ready_decision_fraction"], 1 / 3)
    target = result["target_pair_states_by_horizon_ms"]["500"]
    assert target["BILATERAL_EXECUTABLE_READY"] == 2



def test_nested_risk_frontier_skips_inner_blocks_with_zero_executable_actions():
    rows = []
    for index in range(48):
        item = row(
            "m" + str(index + 3000),
            signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44,
            depth=20.0,
        )
        item["information_end_ns"] = item["decision_ns"]
        item["label"] = None
        item["label_information_ns"] = None
        # Earliest markets have valid states but no causal arrival/exit labels,
        # so an inner fold can be nonempty while yielding zero action targets.
        if index < 18:
            item["arrivals"] = {}
            item["targets"] = {}
        rows.append(item)

    result = nested_walk_forward_risk_frontier(
        rows,
        desired_folds=3,
        inner_desired_folds=2,
        policy_grid=[{
            "policy_id": "baseline",
            "friction_policy": FrictionPolicy(
                uncertainty_aversion=1.0,
                asset_concentration_lambda=0.0,
                common_factor_concentration_lambda=0.0,
            ),
        }],
        latency_ms=50,
        capital_budget=1000.0,
        model_kwargs={
            "size_grid": (1.0, 5.0),
            "action_horizons_ms": (500,),
            "train_latencies_ms": (50,),
            "streaming_batch_size": 16,
        },
    )
    assert result["state"] == "READY"
    assert result["folds"]
    assert any(
        (fold.get("selection") or {}).get("state")
        == "INSUFFICIENT_INNER_ACTION_TARGETS_NO_POLICY_SELECTION"
        for fold in result["folds"]
    )
    for fold in result["folds"]:
        selection = fold.get("selection") or {}
        if selection.get("state") == "INSUFFICIENT_INNER_ACTION_TARGETS_NO_POLICY_SELECTION":
            assert selection["policy_id"] is None
            assert selection["qualified_policy_ids"] == []
            assert fold["inner_fit_failures"]
            assert fold["outer_oos"]["state"] == "NOT_EVALUATED_NO_VALIDATION_MODEL"



def test_calibration_tail_cannot_change_frozen_mean_model_coefficients():
    rows = [
        row(
            "m" + str(index + 4000),
            signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44,
            depth=20.0,
        )
        for index in range(30)
    ]
    altered = json.loads(json.dumps(rows))
    # With 30 chronological markets, the final six are the 20% calibration
    # tail. Change only their realized outcomes; a valid split-conformal
    # implementation must not let those labels refit the deployment mean.
    for item in altered[-6:]:
        item["targets"]["500"]["arrival_bid"] = .98
        item["targets"]["500"]["arrival_ask"] = .99

    kwargs = dict(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        streaming_batch_size=16,
    )
    baseline = DirectActionValueModel(**kwargs).fit(rows)
    shocked = DirectActionValueModel(**kwargs).fit(altered)

    assert baseline.training_receipt[
        "calibration_holdout_excluded_from_mean_fit"] is True
    assert shocked.training_receipt[
        "calibration_holdout_excluded_from_mean_fit"] is True
    assert baseline.training_receipt["calibration_market_count"] == 6
    assert shocked.training_receipt["calibration_market_count"] == 6
    for left, right in zip(baseline.mean_model.beta, shocked.mean_model.beta):
        assert math.isclose(
            float(left), float(right), rel_tol=0.0, abs_tol=1e-12)



def test_post_argmax_calibration_is_one_sided_and_can_flip_trade_to_no_trade():
    import numpy as np

    rows = [
        row(
            "m" + str(index + 6000),
            signal=2.0,
            exit_bid=.40,
            depth=20.0,
        )
        for index in range(24)
    ]
    model = DirectActionValueModel(
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        friction_policy=FrictionPolicy(uncertainty_aversion=0.0),
    )
    model._configure_levels(rows)

    class OptimisticModel:
        def __init__(self, names):
            self.names = tuple(names)
            self.center = {name: 0.0 for name in self.names}
            self.scale = {name: 1.0 for name in self.names}
            self.beta = np.zeros(1 + 2 * len(self.names), dtype=float)
            self.beta[0] = 1.0
            self.beta[1 + self.names.index("action.size")] = .01

        def predict(self, record):
            value = float(self.beta[0])
            for index, name in enumerate(self.names):
                raw = record["features"].get(name)
                if isinstance(raw, (int, float)) and math.isfinite(raw):
                    value += float(self.beta[1 + index]) * float(raw)
                else:
                    value += float(
                        self.beta[1 + len(self.names) + index])
            return value

    model.mean_model = OptimisticModel(model.model_feature_names)
    model.scale_model = None
    model.uncertainty_floor = 0.0
    model.calibration_multiplier = 1.0
    model.selection_optimism_penalty = 0.0
    model.fitted = True

    result = model._calibrate_selected_policy(
        rows, {str(item["market_id"]) for item in rows})
    assert result["state"] == "TEMPORAL_MARKET_BLOCK_POST_ARGMAX_ONE_SIDED"
    assert result["observed_selected_markets"] >= 10
    assert result["observed_fraction"] == 1.0
    assert result["penalty"] > 0
    assert result["mean_optimism_after_action_conformal"] > 0

    probe = row("m6999", signal=2.0, exit_bid=.40, depth=20.0)
    before = model.select_action(probe, latency_ms=50)
    assert before["action"] == "TRADE"
    model.selection_optimism_penalty = result["penalty"]
    after = model.select_action(probe, latency_ms=50)
    assert after["action"] == "NO_TRADE"


def test_prequential_post_argmax_calibration_keeps_final20_for_action_conformal():
    rows = [
        row(
            "m" + str(index + 7000),
            signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44,
            depth=20.0,
        )
        for index in range(100)
    ]
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        streaming_batch_size=32,
    ).fit(rows)
    receipt = model.training_receipt
    assert receipt["training_states_total"] == 100
    assert receipt["training_states_used"] == 80
    assert receipt["fit_market_count"] == 60
    assert receipt["scale_market_count"] == 20
    assert receipt["calibration_market_count"] == 20
    assert receipt["action_calibration_market_count"] == 20
    assert receipt["selection_calibration_mode"] == "PREQUENTIAL"
    assert receipt["prequential_calibration_blocks"] == 2
    assert receipt["calibration_holdouts_disjoint"] is True
    assert receipt["selection_scores_oos_when_generated"] is True
    assert receipt["selection_score_blocks_may_enter_final_mean_fit"] is True
    assert receipt["calibration_semantics"] == (
        "MEAN_FIT_FIRST_80_PERCENT;"
        "ACTION_CONFORMAL_FINAL_20_PERCENT;"
        "POST_ARGMAX_OPTIMISM_PREQUENTIAL_ROLLING_OOS_WITHIN_FIRST_80"
    )
    blocks = (receipt["selection_calibration"] or {}).get("blocks") or []
    assert len(blocks) <= 2
    for block in blocks:
        assert block["train_markets"] > 0
        assert block["evaluation_markets"] > 0
        assert block["clone_calibration_state"] in (
            "TEMPORAL_MARKET_BLOCK_SPLIT_CONFORMAL",
            "INSUFFICIENT_CALIBRATION_ACTION_TARGETS",
            "INSUFFICIENT_MARKET_BLOCKS",
        )



def test_censored_trade_without_causal_loss_bound_keeps_robust_risk_unavailable():
    outcome = _risk_outcome(0, 1.0, censored=True)
    outcome.pop("censored_worst_case_pnl")
    outcome.pop("censored_worst_case_loss_bound")
    risk = scenario_risk_metrics([outcome], block_ms=500)
    assert risk["state"] == "PARTIAL_CENSORED_NO_PROMOTION_CLAIM"
    robust = risk["censored_worst_case"]
    assert robust["state"] == "UNAVAILABLE_MISSING_CAUSAL_LOSS_BOUND"
    assert robust["missing_bound_trades"] == 1
    assert robust["total_net_pnl_lower_bound"] is None


def test_robust_pareto_frontier_uses_lower_bound_without_promoting_censored_policy():
    def entry(name, lower_pnl, cvar, drawdown):
        return {
            "policy_id": name,
            "risk": {
                "state": "PARTIAL_CENSORED_NO_PROMOTION_CLAIM",
                "censored_worst_case": {
                    "state": "READY",
                    "total_net_pnl_lower_bound": lower_pnl,
                    "max_drawdown": drawdown,
                    "tail_loss": {"0.95": {"cvar": cvar}},
                },
            },
        }

    entries = [
        entry("dominated", -5.0, 4.0, 5.0),
        entry("safe", -3.0, 2.0, 3.0),
        entry("profit_floor", 1.0, 3.0, 4.0),
    ]
    robust = set(robust_pareto_frontier(entries))
    assert "dominated" not in robust
    assert robust == {"safe", "profit_floor"}
    # The ordinary promotion-grade Pareto remains empty because every policy
    # still has censored selected trades.
    assert pareto_frontier(entries) == []



def test_prequential_clone_disables_recursive_selection_calibration():
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        prequential_calibration_blocks=2,
    )
    clone = model._prequential_clone()
    assert clone.selection_calibration_mode == "OFF"
    assert clone.prequential_calibration_blocks == 2


def test_prequential_selection_scores_are_generated_on_later_market_blocks():
    rows = [
        row(
            "m" + str(index + 8000),
            signal=2.0 if index % 3 else -2.0,
            exit_bid=.56 if index % 3 else .44,
            depth=20.0,
        )
        for index in range(120)
    ]
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        streaming_batch_size=32,
        prequential_calibration_blocks=2,
    ).fit(rows)
    calibration = model.training_receipt["selection_calibration"]
    blocks = calibration.get("blocks") or []
    assert len(blocks) <= 2
    if len(blocks) == 2:
        assert blocks[1]["train_markets"] > blocks[0]["train_markets"]
    assert model.training_receipt["selection_scores_oos_when_generated"] is True
    assert model.training_receipt["selection_score_blocks_may_enter_final_mean_fit"] is True
    assert calibration["semantics"] == (
        "ROLLING_MARKET_BLOCK_OOS_SCORES;"
        "FINAL_MODEL_MAY_LATER_REFIT_ON_HISTORICAL_SCORE_BLOCKS;"
        "NOT_A_FINAL_MODEL_CONFORMAL_COVERAGE_CLAIM"
    )



def test_effective_signal_age_and_operational_latency_floor_are_distinct():
    r = row("m9100")
    r["signal_age_ns"] = 20_000_000
    r["paper_venue_delay_ns"] = 25_000_000
    r["paper_assumed_transport_delay_ns"] = 50_000_000

    assert math.isclose(
        configured_operational_latency_floor_ms(r), 75.0, abs_tol=1e-12)
    assert math.isclose(effective_signal_age_ms(r, 50), 70.0, abs_tol=1e-12)

    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        selection_calibration_mode="OFF",
    )
    model._configure_levels([r])
    action = model._action_record(
        r, size=5.0, horizon_ms=500, latency_ms=50)
    features = action["features"]
    assert math.isclose(
        features["state.effective_research_age_ms"], 70.0, abs_tol=1e-12)
    assert math.isclose(
        features["state.effective_operational_age_ms"], 95.0, abs_tol=1e-12)
    assert math.isclose(
        features["system.operational_latency_floor_ms"], 75.0, abs_tol=1e-12)
    assert features["system.operational_latency_floor_known"] == 1.0
    assert math.isclose(
        features["system.research_latency_minus_operational_floor_ms"],
        -25.0, abs_tol=1e-12)
    assert action["research_latency_operationally_supported"] is False


def test_unknown_operational_latency_never_claims_research_latency_is_supported():
    r = row("m9101")
    r["paper_venue_delay_ns"] = -1
    r["paper_assumed_transport_delay_ns"] = 50_000_000
    assert configured_operational_latency_floor_ms(r) is None

    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        selection_calibration_mode="OFF",
    )
    model._configure_levels([r])
    action = model._action_record(
        r, size=5.0, horizon_ms=500, latency_ms=50)
    assert action["operational_latency_floor_ms"] is None
    assert action["research_latency_operationally_supported"] is None


def test_effective_signal_age_gate_fail_closed_before_action_scoring():
    import numpy as np

    r = row("m9102")
    r["signal_age_ns"] = 80_000_000
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        maximum_effective_signal_age_ms=100.0,
        selection_calibration_mode="OFF",
    )
    model._configure_levels([r])

    class FlatModel:
        def __init__(self, names):
            self.names = tuple(names)
            self.center = {name: 0.0 for name in names}
            self.scale = {name: 1.0 for name in names}
            self.beta = np.zeros(1 + 2 * len(names), dtype=float)
            self.beta[0] = 1.0

        def predict(self, record):
            return 1.0

    model.mean_model = FlatModel(model.model_feature_names)
    model.scale_model = None
    model.uncertainty_floor = 0.0
    model.calibration_multiplier = 1.0
    model.selection_optimism_penalty = 0.0
    model.fitted = True

    scored, state = model.score_actions(r, latency_ms=50)
    assert scored == []
    assert state == "EFFECTIVE_SIGNAL_AGE_GATE"


def test_prequential_clone_preserves_effective_signal_age_gate():
    model = DirectActionValueModel(
        maximum_effective_signal_age_ms=125.0,
        selection_calibration_mode="PREQUENTIAL",
    )
    clone = model._prequential_clone()
    assert clone.maximum_effective_signal_age_ms == 125.0
    assert clone.selection_calibration_mode == "OFF"



def test_streaming_ridge_partial_pooling_penalty_shrinks_deviation_feature():
    rows = []
    for index in range(1, 40):
        x = float(index) / 10.0
        rows.append({
            "features": {
                "shared": x,
                "pool.asset_signal::BTC": x,
            },
            "target": 2.0 * x,
        })

    equal = StreamingRidge(
        ("shared", "pool.asset_signal::BTC"),
        ridge=2.0,
        batch_size=8,
        deviation_penalty_multiplier=1.0,
    ).fit_factory(lambda: iter(rows), lambda r: r["target"])
    pooled = StreamingRidge(
        ("shared", "pool.asset_signal::BTC"),
        ridge=2.0,
        batch_size=8,
        deviation_penalty_multiplier=16.0,
    ).fit_factory(lambda: iter(rows), lambda r: r["target"])

    equal_shared = float(equal.beta[1])
    equal_deviation = float(equal.beta[2])
    pooled_shared = float(pooled.beta[1])
    pooled_deviation = float(pooled.beta[2])

    assert math.isclose(
        abs(equal_shared), abs(equal_deviation), rel_tol=1e-7, abs_tol=1e-7)
    assert abs(pooled_deviation) < abs(pooled_shared)
    assert abs(pooled_deviation) < abs(equal_deviation)
    assert pooled.deviation_feature_count == 1


def test_partial_pooling_features_are_asset_horizon_and_age_specific():
    btc = row("m9200", signal=2.0)
    btc["asset"] = "BTC"
    eth = row("m9201", signal=-1.0)
    eth["asset"] = "ETH"
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500, 1000),
        train_latencies_ms=(50,),
        partial_pooling_enabled=True,
        partial_pooling_penalty_multiplier=8.0,
        selection_calibration_mode="OFF",
    )
    model._configure_levels([btc, eth])

    action = model._action_record(
        btc, size=5.0, horizon_ms=500, latency_ms=50)
    features = action["features"]
    assert features["pool.asset_signal::BTC"] == 2.0
    assert features["pool.asset_signal::ETH"] == 0.0
    assert features["pool.asset_exit::BTC::500"] == 1.0
    assert features["pool.asset_exit::BTC::1000"] == 0.0
    assert features["pool.asset_exit::ETH::500"] == 0.0
    assert features["pool.asset_effective_age::BTC"] > 0
    assert features["pool.asset_effective_age::ETH"] == 0.0


def test_direct_model_receipt_declares_partial_pooling_shrinkage():
    rows = []
    for index in range(40):
        item = row(
            "m" + str(index + 9300),
            signal=2.0 if index % 2 == 0 else -2.0,
            exit_bid=.56 if index % 2 == 0 else .44,
        )
        item["asset"] = "BTC" if index % 3 else "ETH"
        rows.append(item)

    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        partial_pooling_enabled=True,
        partial_pooling_penalty_multiplier=8.0,
        selection_calibration_mode="OFF",
        streaming_batch_size=16,
    ).fit(rows)
    pooling = model.training_receipt["partial_pooling"]
    assert pooling["enabled"] is True
    assert pooling["deviation_penalty_multiplier"] == 8.0
    assert pooling["deviation_feature_count"] > 0
    assert pooling["deviation_prefix"] == "pool."


def test_partial_pooling_multiplier_below_one_is_rejected():
    try:
        DirectActionValueModel(partial_pooling_penalty_multiplier=.5)
    except ValueError as exc:
        assert "partial pooling penalty multiplier" in str(exc)
    else:
        raise AssertionError("expected invalid pooling multiplier to fail")



def test_bilateral_support_gate_requires_training_market_support():
    rows = [
        bilateral_row("m" + str(index + 9400), no_depth=10.0)
        for index in range(4)
    ]
    probe = bilateral_row("m9499", no_depth=10.0)

    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        minimum_bilateral_opposite_side_markets=5,
        selection_calibration_mode="OFF",
    )
    model._configure_levels(rows)
    support = model._bilateral_support_summary(rows)
    assert support["bilateral_markets"] == 4
    assert support["yes_supported_markets"] == 4
    assert support["no_supported_markets"] == 4
    assert support["opposite_side_ready"] is False

    model.bilateral_support_markets = 4
    assert model._eligible_action_sides(probe) == ("YES",)
    model.bilateral_support_markets = 5
    assert model._eligible_action_sides(probe) == ("YES", "NO")


def test_bilateral_support_gate_never_blocks_selected_side():
    legacy = row("m9500")
    model = DirectActionValueModel(
        minimum_bilateral_opposite_side_markets=100,
        selection_calibration_mode="OFF",
    )
    model.bilateral_support_markets = 0
    assert model._eligible_action_sides(legacy) == ("SELECTED",)


def test_bilateral_support_receipt_uses_training_only_markets():
    rows = [
        bilateral_row("m" + str(index + 9510), no_depth=10.0)
        for index in range(12)
    ]
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        minimum_bilateral_opposite_side_markets=8,
        selection_calibration_mode="OFF",
        streaming_batch_size=16,
    ).fit(rows)
    support = model.training_receipt["bilateral_side_support"]
    assert support["bilateral_markets"] == 12
    assert support["minimum_required_markets"] == 8
    assert support["opposite_side_ready"] is True


def test_negative_bilateral_support_threshold_is_rejected():
    try:
        DirectActionValueModel(minimum_bilateral_opposite_side_markets=-1)
    except ValueError as exc:
        assert "minimum bilateral opposite-side markets" in str(exc)
    else:
        raise AssertionError("expected negative bilateral support threshold to fail")



def test_support_examples_distinguish_censoring_no_fill_and_full_round_trip():
    full = row("m9600", exit_bid=.56, depth=20.0)
    censored = row("m9601", exit_bid=.56, depth=20.0)
    censored["targets"] = {}
    no_fill = row("m9602", exit_bid=.56, depth=20.0)
    no_fill["arrivals"]["50"]["ask"] = .51

    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        support_heads_enabled=True,
        selection_calibration_mode="OFF",
    )
    model._configure_levels([full, censored, no_fill])

    examples = list(model._iter_support_examples(
        [full, censored, no_fill]))
    by_market = {}
    for item in examples:
        by_market.setdefault(item["market_id"], []).append(item)

    assert by_market["m9600"][0]["evidence_support_target"] == 1.0
    assert by_market["m9600"][0]["full_execution_target"] == 1.0
    assert by_market["m9601"][0]["evidence_support_target"] == 0.0
    assert by_market["m9601"][0]["full_execution_target"] == 0.0
    assert by_market["m9602"][0]["evidence_support_target"] == 1.0
    assert by_market["m9602"][0]["full_execution_target"] == 0.0


def test_support_gate_can_reject_action_without_redefining_pnl_target():
    import numpy as np

    r = row("m9603", exit_bid=.56, depth=20.0)
    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        minimum_evidence_support_probability=.7,
        minimum_full_execution_probability=.6,
        selection_calibration_mode="OFF",
    )
    model._configure_levels([r])

    class ConstantModel:
        def __init__(self, names, value):
            self.names = tuple(names)
            self.value = float(value)
            self.center = {name: 0.0 for name in names}
            self.scale = {name: 1.0 for name in names}
            self.beta = np.zeros(1 + 2 * len(names), dtype=float)
            self.beta[0] = self.value

        def predict(self, record):
            return self.value

    model.mean_model = ConstantModel(model.model_feature_names, 1.0)
    model.scale_model = None
    model.uncertainty_floor = 0.0
    model.calibration_multiplier = 1.0
    model.selection_optimism_penalty = 0.0
    model.evidence_support_model = ConstantModel(
        model.model_feature_names, .65)
    model.full_execution_model = ConstantModel(
        model.model_feature_names, .90)
    model.fitted = True

    scored, state = model.score_actions(r, latency_ms=50)
    assert scored == []
    assert state == "ACTION_SUPPORT_GATE"

    model.minimum_evidence_support_probability = .6
    scored, state = model.score_actions(r, latency_ms=50)
    assert state == "READY"
    assert scored
    assert scored[0]["evidence_support_probability"] == .65
    assert scored[0]["full_execution_probability"] == .90
    assert scored[0]["support_eligible"] is True
    assert scored[0]["predicted_total_net_cash_pnl"] == 1.0


def test_support_heads_are_fitted_on_precalibration_training_rows():
    rows = []
    for index in range(40):
        item = row(
            "m" + str(index + 9610),
            exit_bid=.56 if index % 2 == 0 else .44,
            depth=20.0,
        )
        if index % 5 == 0:
            item["targets"] = {}
        rows.append(item)

    model = DirectActionValueModel(
        size_grid=(5.0,),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        support_heads_enabled=True,
        selection_calibration_mode="OFF",
        streaming_batch_size=16,
    ).fit(rows)
    receipt = model.training_receipt["action_support_heads"]
    assert receipt["enabled"] is True
    assert receipt["state"] == "READY"
    assert receipt["used_as_economic_pnl"] is False
    assert receipt["role"] == "SELECTION_SUPPORT_GATE_ONLY"
    assert model.evidence_support_model is not None
    assert model.full_execution_model is not None


def test_support_probability_thresholds_are_bounded():
    for kwargs in (
        {"minimum_evidence_support_probability": 1.1},
        {"minimum_full_execution_probability": -0.1},
    ):
        try:
            DirectActionValueModel(**kwargs)
        except ValueError as exc:
            assert "probability" in str(exc)
        else:
            raise AssertionError("expected invalid support threshold to fail")
