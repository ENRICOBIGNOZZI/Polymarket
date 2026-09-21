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
    scenario_risk_metrics,
    select_policy_under_risk_budget,
)
from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    FrictionPolicy,
    StreamingRidge,
    candidate_sizes,
    decision_action_sides,
    realized_action_economics,
    realized_action_value,
    residual_policy_friction,
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
                "quantity": depth,
                "epoch": 7,
            }
        },
        "targets": {
            "500": {
                "state": "OBSERVED",
                "arrival_bid": exit_bid,
                "arrival_ask": exit_bid + .01,
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
    assert receipt["calibration_state"] == "TEMPORAL_MARKET_BLOCK_CONFORMAL"



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
    reconstructed = (
        economics["ideal_midpoint_alpha"]
        - economics["decision_half_spread_cost"]
        - economics["latency_price_drift_cost"]
        - economics["exit_half_spread_cost"]
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


def test_direct_action_uses_all_training_states_with_p_squared_memory():
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
    assert receipt["training_states_used"] == 120
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
