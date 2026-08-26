#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_execution_core import (
    JointStateDistribution,
    MakerState,
    RestingOrder,
    TapeTrade,
    allocate_shared_trade_capacity,
    economically_complete,
    forward_fill_eligible,
    joint_bundle_ev,
    maker_fill_conditioned_ev,
    quote_improvement_is_economic,
    structural_terminal_floor,
)


def maker(**overrides):
    values = dict(
        side="BUY",
        limit_price=0.40,
        fair_exit_price=0.43,
        queue_ahead=20.0,
        own_size=10.0,
        compatible_flow=40.0,
        flow_horizon_seconds=60.0,
        ofi=0.5,
        imbalance=0.4,
        microprice=0.415,
        midpoint=0.41,
        displayed_depth=100.0,
        entry_fee_per_share=0.0,
        exit_fee_per_share=0.002,
        slippage_per_share=0.001,
        adverse_markout_per_share=0.002,
        partial_unwind_loss_per_share=0.01,
        expected_partial_fraction=0.10,
        capital_usd=4.0,
        capital_time_rate_per_second=0.0,
        expected_rest_seconds=30.0,
        latency_seconds=0.1,
    )
    values.update(overrides)
    return MakerState(**values)


def test_fill_probability_is_not_fill_quality():
    benign = maker(ofi=0.8, imbalance=0.7, microprice=0.418)
    toxic = maker(ofi=-1.0, imbalance=-1.0, microprice=0.395)
    a = maker_fill_conditioned_ev(benign)
    b = maker_fill_conditioned_ev(toxic)
    assert a.fill_probability > b.fill_probability
    assert a.expected_value > b.expected_value
    assert a.toxicity_score < b.toxicity_score


def test_quote_improvement_must_pay_incremental_tick():
    current = maker(queue_ahead=100.0, compatible_flow=15.0, limit_price=0.40)
    good = maker(queue_ahead=0.0, compatible_flow=15.0, limit_price=0.401, fair_exit_price=0.435)
    bad = maker(queue_ahead=0.0, compatible_flow=15.0, limit_price=0.41, fair_exit_price=0.415)
    assert quote_improvement_is_economic(current, good)
    assert not quote_improvement_is_economic(current, bad)


def test_joint_states_are_explicit_not_product_of_marginals():
    distribution = JointStateDistribution(2, {0: 0.80, 1: 0.10, 2: 0.10, 3: 0.0}, observations=100)
    ev = joint_bundle_ev(distribution, {0: 0.0, 1: -0.30, 2: -0.30, 3: 1.0})
    assert ev.expected_value == -0.06
    assert ev.full_completion_component == 0.0
    assert ev.partial_state_component == -0.06


def test_missing_joint_state_economics_fail_closed():
    distribution = JointStateDistribution(2, {0: 0.7, 1: 0.1, 2: 0.1, 3: 0.1})
    try:
        joint_bundle_ev(distribution, {0: 0.0, 3: 1.0})
    except ValueError as exc:
        assert "missing explicit unwind" in str(exc)
    else:
        raise AssertionError("partial states must have explicit unwind economics")


def test_public_trade_capacity_is_conserved_across_orders():
    trade = TapeTrade("t1", "token", "SELL", 0.40, 110.0, 2_000, 2_100)
    orders = [
        RestingOrder("a", "token", "BUY", 0.40, 100.0, 10.0, 1_000, 1_000),
        RestingOrder("b", "token", "BUY", 0.40, 100.0, 10.0, 1_100, 1_100),
    ]
    fills = allocate_shared_trade_capacity(trade, orders)
    assert sum(fills.values()) == 10.0
    assert fills["a"] == 10.0
    assert fills["b"] == 0.0


def test_backfilled_old_trade_cannot_fill_new_order():
    order = RestingOrder("o", "token", "BUY", 0.40, 0.0, 10.0, 10_000, 10_000)
    old_event_received_late = TapeTrade("t", "token", "SELL", 0.39, 10.0, 9_000, 11_000)
    fresh = TapeTrade("u", "token", "SELL", 0.39, 10.0, 10_500, 10_600)
    assert not forward_fill_eligible(old_event_received_late, order, 12_000, 12_000)
    assert forward_fill_eligible(fresh, order, 12_000, 12_000)


def test_operational_completion_is_not_economic_completion():
    prices = [0.74, 0.15, 0.09]
    units = 60.0 / sum(prices)
    shares = [units, 0.75 * units, 0.75 * units]
    costs = [shares[i] * prices[i] for i in range(3)]
    payout_matrix = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    floor = structural_terminal_floor(payout_matrix, shares, costs)
    assert floor < -10.0
    assert not economically_complete(payout_matrix, shares, costs)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 execution core tests")
