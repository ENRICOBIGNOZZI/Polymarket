import math

from research.walk_forward_v3.direct_action import (
    EdgeSizingPolicy,
    FrictionPolicy,
    evaluate_direct_action_policy,
)
from research.walk_forward_v3.decomposed_action import (
    DecomposedActionValueModel,
)


def row(index, *, signal=2.0, fill=True, exit_bid=.56, depth=20.0):
    market = f"m{index}"
    decision_ns = 1_789_921_800_000_000_000 + index * 1_000_000_000
    ask = .50
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
        "bid": .49,
        "ask": ask,
        "bid_quantity": depth,
        "quantity": depth,
        "minimum": 1.0,
        "tick": .01,
        "fee_rate": 0.0,
        "fee_exponent": 1.0,
        "features": {
            "external.binance_return_100ms_bp": signal,
            "signal_age_ns": 1_000_000.0,
            "tte_ns": 110_000_000_000.0,
            "parent_shock_id": f"shock-{index}",
        },
        "parent_shock_id": f"shock-{index}",
        "arrivals": {
            "50": {
                "time_ns": decision_ns + 50_000_000,
                "bid": .49,
                "ask": ask if fill else .51,
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


def training_rows(count=60):
    values = []
    for index in range(count):
        positive = index % 2 == 0
        values.append(row(
            index,
            signal=2.0 if positive else -2.0,
            fill=positive,
            exit_bid=.56 if positive else .44,
        ))
    return values


def fitted_model():
    return DecomposedActionValueModel(
        size_grid=(1.0, 5.0, 10.0),
        action_horizons_ms=(500,),
        train_latencies_ms=(50,),
        hard_order_notional=20.0,
        max_sizes_per_state=3,
        streaming_batch_size=32,
        selection_calibration_mode="OFF",
        friction_policy=FrictionPolicy(uncertainty_aversion=0.0),
        conditional_calibration=True,
        conditional_calibration_min_markets=4,
        conditional_calibration_shrinkage=4.0,
    ).fit(training_rows())


def test_decomposed_heads_are_separate_and_paper_only():
    model = fitted_model()
    receipt = model.training_receipt
    assert receipt["paper_only"] is True
    assert receipt["authenticated_execution"] is False
    assert receipt["real_order_submission"] is False
    assert receipt["real_capital_at_risk"] is False
    assert receipt["automatic_promotion"] is False
    assert receipt["fill_head_rows"] > 0
    assert receipt["conditional_pnl_head_rows"] > 0
    assert receipt["conditional_pnl_head_rows"] < receipt["fill_head_rows"]
    assert receipt["model"] == (
        "LINEAR_FILL_PROBABILITY_X_CONDITIONAL_EXECUTABLE_CASH_PNL"
    )


def test_decomposed_score_exposes_bounded_fill_probability_and_cash_components():
    model = fitted_model()
    probe = row(1000, signal=2.0, fill=True, exit_bid=.56)
    scores, state = model.score_actions(
        probe, latency_ms=50, capital_budget=1000.0)
    assert state == "READY"
    assert scores
    for score in scores:
        assert 0.0 <= score["predicted_fill_probability"] <= 1.0
        assert math.isfinite(score["predicted_cash_pnl_given_fill"])
        assert math.isclose(
            score["predicted_total_net_cash_pnl"],
            score["predicted_fill_probability"]
            * score["predicted_cash_pnl_given_fill"],
            rel_tol=0.0,
            abs_tol=1e-12,
        )


def test_decomposed_edge_sizing_respects_depth_and_hard_notional():
    model = fitted_model()
    policy = EdgeSizingPolicy(
        context_count=2,
        knots=((0.0, 0.0), (0.0001, 1.0), (1.0, 1.0)),
    )
    probe = row(
        1001, signal=2.0, fill=True, exit_bid=.70, depth=12.0)
    selected = model.select_action_edge_sized(
        probe,
        latency_ms=50,
        available_capital=1000.0,
        capital_budget=1000.0,
        sizing_policy=policy,
    )
    if selected["action"] == "TRADE":
        assert selected["size"] <= 12.0 + 1e-12
        assert selected["notional"] <= 20.0 + 1e-12
        assert selected["desired_notional_after_constraints"] <= 20.0 + 1e-12
        assert selected["sizing_mode"] == (
            "DECOMPOSED_EDGE_CONTEXT_BUDGET")


def test_decomposed_model_runs_through_existing_sequential_replay():
    model = fitted_model()
    test_rows = [
        row(2000 + index,
            signal=2.0 if index % 2 == 0 else -2.0,
            fill=index % 2 == 0,
            exit_bid=.56 if index % 2 == 0 else .44)
        for index in range(12)
    ]
    outcomes = evaluate_direct_action_policy(
        model,
        test_rows,
        latency_ms=50,
        capital_budget=1000.0,
        entry_policy="ONE_ENTRY_PER_SHOCK",
        sizing_policy=EdgeSizingPolicy(
            context_count=4,
            knots=((0.0, 0.0), (0.001, .1), (.02, .5), (.1, 1.0)),
        ),
        max_market_exposure=250.0,
    )
    assert len(outcomes) == len(test_rows)
    assert all(
        outcome.get("entry_policy") == "ONE_ENTRY_PER_SHOCK"
        for outcome in outcomes
    )
    for outcome in outcomes:
        if outcome.get("action") == "TRADE":
            assert outcome["notional"] <= 20.0 + 1e-12
            assert outcome["value_model"] == (
                "DECOMPOSED_FILL_X_CONDITIONAL_CASH")
