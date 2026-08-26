#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
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
from v7_execution_ledger import (
    CanonicalLedgerWriter,
    LedgerContractError,
    LedgerEvent,
    LedgerOwnershipError,
    canonical_ledger_path,
    load_events,
)


LEDGER_SHA = "a" * 40


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


def ledger_candidate(**overrides):
    values = dict(
        event_type="CANDIDATE",
        strategy="MICRO_TAKER",
        model_sha=LEDGER_SHA,
        recorded_ts_ms=1_030,
        receive_ts_ms=1_010,
        exchange_ts_ms=1_000,
        decision_ts_ms=1_020,
        book_snapshot_id="book-1",
        market_id="m1",
        candidate_id="c1",
        bid=0.49,
        ask=0.51,
        bid_depth=100.0,
        ask_depth=80.0,
        predicted_alpha=0.02,
        predicted_fill_probability=1.0,
        expected_ev=0.005,
        intended_action="BUY_TAKER",
        intended_size=10.0,
    )
    values.update(overrides)
    return LedgerEvent(**values)


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
    assert abs(ev.expected_value + 0.06) < 1e-12
    assert abs(ev.full_completion_component) < 1e-12
    assert abs(ev.partial_state_component + 0.06) < 1e-12


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
    assert abs(sum(fills.values()) - 10.0) < 1e-12
    assert abs(fills["a"] - 10.0) < 1e-12
    assert abs(fills["b"]) < 1e-12


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


def test_canonical_ledger_is_paper_only_and_receive_time_causal():
    ledger_candidate().validate()
    for overrides in ({"authenticated_execution": True}, {"paper_only": False}):
        try:
            ledger_candidate(**overrides).validate()
        except LedgerContractError as exc:
            assert "safety:not_paper_only" in str(exc)
        else:
            raise AssertionError("ledger safety boundary must fail closed")

    try:
        ledger_candidate(decision_ts_ms=1_000, receive_ts_ms=1_010).validate()
    except LedgerContractError as exc:
        assert "decision_before_receive" in str(exc)
    else:
        raise AssertionError("decision must not precede the observed receive clock")


def test_canonical_ledger_has_one_exact_sha_writer():
    with tempfile.TemporaryDirectory() as tmp:
        path = canonical_ledger_path(Path(tmp))
        first = CanonicalLedgerWriter(path, writer_id="paper-runtime", model_sha=LEDGER_SHA)
        second = CanonicalLedgerWriter(path, writer_id="other-runtime", model_sha=LEDGER_SHA)
        first.acquire()
        try:
            try:
                second.acquire()
            except LedgerOwnershipError as exc:
                assert "ledger_already_owned" in str(exc)
            else:
                raise AssertionError("a second canonical ledger writer must fail closed")
            first.append(ledger_candidate())
            try:
                first.append(ledger_candidate(model_sha="b" * 40))
            except LedgerContractError as exc:
                assert "model_sha:mixed_sha" in str(exc)
            else:
                raise AssertionError("mixed-SHA execution evidence must fail closed")
        finally:
            first.close()

        events = load_events(path, expected_model_sha=LEDGER_SHA)
        assert len(events) == 1
        assert events[0].event_type == "CANDIDATE"


def test_canonical_ledger_types_fill_final_and_markout_evidence():
    fill = LedgerEvent(
        event_type="FILL",
        strategy="GRAPH_RV",
        model_sha=LEDGER_SHA,
        recorded_ts_ms=2_000,
        order_id="ord-1",
        filled_size=2.0,
        fee=0.01,
        slippage=0.02,
        markouts={"1s": -0.01, "10s": 0.0, "45s": 0.01, "60s": 0.02, "300s": 0.03},
    )
    fill.validate()
    LedgerEvent(
        event_type="FINAL",
        strategy="GRAPH_RV",
        model_sha=LEDGER_SHA,
        recorded_ts_ms=2_100,
        final_pnl=-0.25,
        capital_duration_ms=0,
    ).validate()

    try:
        LedgerEvent(
            event_type="FILL",
            strategy="GRAPH_RV",
            model_sha=LEDGER_SHA,
            recorded_ts_ms=2_000,
            order_id="ord-1",
            filled_size=0.0,
        ).validate()
    except LedgerContractError as exc:
        assert "fill:missing_positive_size" in str(exc)
    else:
        raise AssertionError("zero-size fill must fail closed")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 execution core tests")
