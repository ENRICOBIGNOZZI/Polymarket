import math

from research.walk_forward_v3.direct_action import (
    DirectActionValueModel,
    candidate_sizes,
    realized_action_value,
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
