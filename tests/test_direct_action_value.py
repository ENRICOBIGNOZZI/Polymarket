import math

from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    FrictionPolicy,
    bounded_training_states,
    candidate_sizes,
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
    assert selected["size"] in (5.0, 10.0, 20.0, 1.0)
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
    assert receipt["calibration_state"] == "MARKET_BLOCK_TEMPORAL_CALIBRATION"



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



def test_training_state_cap_preserves_market_breadth_deterministically():
    rows = [
        row("m" + str(index + 700), signal=1.0 + (index % 3))
        for index in range(120)
    ]
    first = bounded_training_states(rows, 30)
    second = bounded_training_states(list(reversed(rows)), 30)
    assert len(first) == 30
    assert [r["decision_id"] for r in first] == [r["decision_id"] for r in second]
    assert len({r["market_id"] for r in first}) == 30

    model = DirectActionValueModel(
        size_grid=(1.0, 5.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        max_training_states=30,
    ).fit(rows)
    receipt = model.training_receipt
    assert receipt["training_states_total"] == 120
    assert receipt["training_states_used"] == 30
    assert receipt["training_markets_used"] == 30
