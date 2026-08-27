#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_execution_core import JointStateDistribution
from v7_graph_execution import (
    ActionPlan,
    GraphExecutionError,
    GraphLeg,
    Placement,
    PriceLevel,
    build_candidate_ledger_events,
    default_two_leg_plans,
    evaluate_plan,
    queue_decoupled_capacity,
    select_best_plan,
)


def leg(
    name: str,
    *,
    queue: float = 20.0,
    depth: float = 200.0,
    fair: float = 0.56,
    exchange: int = 1_000,
    receive: int = 1_100,
) -> GraphLeg:
    return GraphLeg(
        leg_id=name,
        market_id=f"m-{name}",
        event_id="event-1",
        token_id=f"t-{name}",
        side="BUY",
        weight=1.0,
        bid=0.49,
        ask=0.51,
        tick=0.01,
        queue_ahead=queue,
        bid_depth=(PriceLevel(0.49, depth), PriceLevel(0.48, depth)),
        ask_depth=(PriceLevel(0.51, depth), PriceLevel(0.52, depth)),
        fair_exit_price=fair,
        maker_fee_per_share=0.0,
        taker_fee_per_share=0.001,
        exit_fee_per_share=0.001,
        fee_source="gamma:fees_disabled_or_verified_schedule",
        adverse_markout_per_share=0.001,
        exchange_ts_ms=exchange,
        receive_ts_ms=receive,
        book_snapshot_id=f"snapshot-{name}",
        min_order=1.0,
    )


def joint(*, none: float = 0.10, left: float = 0.10, right: float = 0.10, full: float = 0.70, observations: int = 100) -> JointStateDistribution:
    return JointStateDistribution(2, {0: none, 1: left, 2: right, 3: full}, observations=observations)


def test_queue_ahead_never_grants_graph_capacity() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    distribution = joint()
    low_queue = [leg("a", queue=1.0), leg("b", queue=2.0, exchange=1001, receive=1101)]
    huge_queue = [leg("a", queue=100_000.0), leg("b", queue=1_000_000.0, exchange=1001, receive=1101)]
    low = queue_decoupled_capacity(low_queue, plan, distribution, risk_capital=10_000.0)
    high = queue_decoupled_capacity(huge_queue, plan, distribution, risk_capital=10_000.0)
    assert low == high
    assert low > 0.0


def test_capacity_tracks_executable_unwind_depth_not_queue() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    distribution = joint()
    thin = [leg("a", depth=20.0), leg("b", depth=20.0, exchange=1001, receive=1101)]
    deep = [leg("a", depth=200.0), leg("b", depth=200.0, exchange=1001, receive=1101)]
    thin_units = queue_decoupled_capacity(thin, plan, distribution, risk_capital=100_000.0)
    deep_units = queue_decoupled_capacity(deep, plan, distribution, risk_capital=100_000.0)
    assert deep_units > thin_units > 0.0


def test_same_marginals_do_not_substitute_for_joint_completion() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    legs = [leg("a"), leg("b", exchange=1001, receive=1101)]
    # Both distributions have marginal P(fill leg 1)=P(fill leg 2)=0.5.
    together = JointStateDistribution(2, {0: 0.5, 1: 0.0, 2: 0.0, 3: 0.5}, observations=100)
    apart = JointStateDistribution(2, {0: 0.0, 1: 0.5, 2: 0.5, 3: 0.0}, observations=100)
    assert queue_decoupled_capacity(legs, plan, together, risk_capital=1_000.0) > 0.0
    assert queue_decoupled_capacity(legs, plan, apart, risk_capital=1_000.0) == 0.0


def test_missing_empirical_joint_support_fails_closed() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    legs = [leg("a"), leg("b", exchange=1001, receive=1101)]
    try:
        evaluate_plan(legs, plan, joint(observations=3), decision_ts_ms=1200, risk_capital=100.0)
    except GraphExecutionError as exc:
        assert str(exc) == "insufficient_joint_state_observations"
    else:
        raise AssertionError("insufficient joint evidence must fail closed")


def test_stale_or_mixed_time_books_fail_closed() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    distribution = joint()
    stale = [leg("a", receive=100), leg("b", exchange=1001, receive=1101)]
    try:
        evaluate_plan(stale, plan, distribution, decision_ts_ms=1500, risk_capital=100.0, max_receive_age_ms=500)
    except GraphExecutionError as exc:
        assert str(exc) == "stale_book"
    else:
        raise AssertionError("stale books must fail closed")

    skewed = [leg("a", exchange=1_000, receive=1_100), leg("b", exchange=5_000, receive=1_101)]
    try:
        evaluate_plan(skewed, plan, distribution, decision_ts_ms=1_200, risk_capital=100.0, max_exchange_skew_ms=500)
    except GraphExecutionError as exc:
        assert str(exc) == "cross_leg_exchange_skew"
    else:
        raise AssertionError("mixed-time legs must fail closed")


def test_partial_unwind_loss_is_explicit_and_can_destroy_ev() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    legs = [leg("a", fair=0.53), leg("b", fair=0.53, exchange=1001, receive=1101)]
    distribution = joint(none=0.05, left=0.25, right=0.25, full=0.45)
    baseline = evaluate_plan(
        legs,
        plan,
        distribution,
        decision_ts_ms=1200,
        risk_capital=100.0,
        min_joint_completion_for_full_size=0.4,
    )
    punished = evaluate_plan(
        legs,
        plan,
        distribution,
        decision_ts_ms=1200,
        risk_capital=100.0,
        min_joint_completion_for_full_size=0.4,
        explicit_partial_unwind_penalty=10.0,
    )
    assert punished.expected_ev < baseline.expected_ev


def test_cost_stress_uses_same_frozen_action_and_size() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    legs = [leg("a"), leg("b", exchange=1001, receive=1101)]
    value = evaluate_plan(legs, plan, joint(), decision_ts_ms=1200, risk_capital=100.0)
    assert set(value.stress_ev) == {1.0, 1.5, 2.0}
    assert value.stress_ev[1.0] >= value.stress_ev[1.5] >= value.stress_ev[2.0]
    assert value.queue_used_for_capacity is False


def test_plan_selector_compares_entry_styles_by_total_ev() -> None:
    legs = [leg("a"), leg("b", exchange=1001, receive=1101)]
    maker = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    taker = ActionPlan("taker", (Placement.CROSS, Placement.CROSS))
    selected = select_best_plan(
        legs,
        [maker, taker],
        {"maker": joint(full=0.75, none=0.05, left=0.10, right=0.10), "taker": joint(full=0.90, none=0.02, left=0.04, right=0.04)},
        decision_ts_ms=1200,
        risk_capital=100.0,
    )
    assert selected is not None
    assert selected.plan.name in {"maker", "taker"}
    assert selected.expected_ev > 0.0


def test_default_frontier_contains_required_graph_action_families() -> None:
    plans = {plan.name for plan in default_two_leg_plans()}
    assert {"maker_maker_join", "maker_maker_improve", "maker_taker", "taker_maker", "taker_taker"} <= plans
    assert {"sequential_maker_taker", "sequential_taker_maker"} <= plans


def test_candidate_handoff_uses_canonical_ledger_contract_without_second_writer() -> None:
    plan = ActionPlan("maker", (Placement.JOIN, Placement.JOIN))
    legs = [leg("a"), leg("b", exchange=1001, receive=1101)]
    distribution = joint()
    value = evaluate_plan(legs, plan, distribution, decision_ts_ms=1200, risk_capital=100.0)
    events = build_candidate_ledger_events(
        legs,
        value,
        distribution,
        model_sha="a" * 40,
        decision_ts_ms=1200,
        opportunity_id="opp-1",
        candidate_id="candidate-1",
        bundle_id="bundle-1",
    )
    assert len(events) == 3
    assert events[0].strategy == "GRAPH_RV"
    assert events[0].predicted_fill_probability == distribution.probabilities[3]
    assert events[0].metadata["queue_used_for_capacity"] is False
    assert events[0].metadata["joint_state_observations"] == 100
    assert all(event.paper_only is True and event.authenticated_execution is False for event in events)
    assert all(event.metadata.get("queue_used_for_capacity", False) is False for event in events[1:])


def test_graph_execution_source_is_v7_only_and_never_owns_ledger_writer() -> None:
    source = (ROOT / "scripts" / "v7_graph_execution.py").read_text(encoding="utf-8")
    assert "v6_" not in source
    assert "CanonicalLedgerWriter" not in source
    assert "queue_ahead" in source
    assert "Queue ahead is deliberately absent" in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"PASS {len(tests)} graph execution tests")
