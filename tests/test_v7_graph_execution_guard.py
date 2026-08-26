#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_execution_guard as guard


def descriptor_session(*, edge: float = 0.02, notional: float = 100.0, target: float = 100.0,
                       burden: float = 2.0, quote_ticks: float = 0.0, origin: int = 1_000_000,
                       state_mask: int = 3, pnl_return: float = 0.01) -> dict:
    descriptor = {
        "descriptor_version": 2,
        "window_seconds": 180,
        "quote_policy": "maker_limit_ticks_from_bid",
        "expected_edge": edge,
        "max_notional": notional,
        "terminal_floor_reason": "verified_complete_nonaugmented_negrisk",
        "terminal_payout_floor_per_unit": 1.0,
        "terminal_payout_floor_cash": 1.0,
        "base_entry_cash": 0.98,
        "entry_fee_cash_1x": 0.012,
        "hold_seconds": 0,
        "legs": [
            {"key": "m1:YES", "target_shares": target, "required_flow_to_target": burden, "quote_ticks_from_bid": quote_ticks},
            {"key": "m2:YES", "target_shares": target, "required_flow_to_target": burden, "quote_ticks_from_bid": quote_ticks},
        ],
    }
    return {
        "evidence_version": 2,
        "signature": "GRAPH_RV|e|m1:YES|m2:YES",
        "strategy": "GRAPH_RV",
        "event_id": "e",
        "origin_received_ms": origin,
        "deadline_received_ms": origin + 180_000,
        "state_mask": state_mask,
        "full_mask": 3,
        "full_completion": state_mask == 3,
        "execution_descriptor": descriptor,
        "stress_return_on_notional": {"1x": pnl_return, "1.5x": pnl_return, "2x": pnl_return},
        "legs": [
            {"market_id": "m1", "side": "YES"},
            {"market_id": "m2", "side": "YES"},
        ],
    }


def current_session(**kwargs) -> dict:
    row = descriptor_session(**kwargs)
    row.pop("evidence_version", None)
    row.pop("stress_return_on_notional", None)
    row["max_notional"] = row["execution_descriptor"]["max_notional"]
    return row


def test_full_completion_uses_payout_minus_executed_entry_costs_under_stress():
    # 0.49 + 0.49 gross quotes imply +2%, but 0.006/share/leg verified fees
    # make the 2x executable terminal PnL negative.
    session = descriptor_session()
    session["execution_descriptor"].update({
        "terminal_payout_floor_cash": 1.0,
        "base_entry_cash": 0.98,
        "entry_fee_cash_1x": 0.012,
        "hold_seconds": 0,
    })
    assert abs(guard.full_completion_stress_pnl(session, 1.0, 0.0) - 0.008) < 1e-12
    assert abs(guard.full_completion_stress_pnl(session, 1.5, 0.0) - 0.002) < 1e-12
    assert abs(guard.full_completion_stress_pnl(session, 2.0, 0.0) + 0.004) < 1e-12


def test_capital_time_is_in_full_completion_economics():
    session = descriptor_session()
    session["execution_descriptor"].update({
        "terminal_payout_floor_cash": 1.0,
        "base_entry_cash": 0.90,
        "entry_fee_cash_1x": 0.0,
        "hold_seconds": 3600,
    })
    no_capital_cost = guard.full_completion_stress_pnl(session, 1.0, 0.0)
    with_capital_cost = guard.full_completion_stress_pnl(session, 1.0, 100.0)
    assert no_capital_cost is not None and with_capital_cost is not None
    assert with_capital_cost < no_capital_cost


def test_more_favorable_historical_edge_cannot_transport():
    current = current_session(edge=0.01)
    historical = descriptor_session(edge=0.03)
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert "historical_edge_too_favorable" in reasons


def test_smaller_or_easier_historical_execution_state_cannot_transport():
    current = current_session(notional=200.0, target=200.0, burden=4.0)
    historical = descriptor_session(notional=50.0, target=50.0, burden=0.5)
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert "historical_notional_too_small" in reasons
    assert any(reason.startswith("historical_target_too_small:") for reason in reasons)
    assert any(reason.startswith("historical_queue_burden_too_easy:") for reason in reasons)


def test_legacy_state_blind_evidence_fails_closed():
    current = current_session()
    legacy = {"signature": current["signature"], "stress_pnl": {"1x": 10.0, "1.5x": 10.0, "2x": 10.0}}
    ok, reasons = guard.comparable_session(legacy, current)
    assert not ok
    assert reasons == ["legacy_evidence_schema"]


def test_overlapping_windows_do_not_count_as_independent_sessions():
    rows = [descriptor_session(origin=1_000_000 + 30_000 * i) for i in range(10)]
    effective = guard.effective_nonoverlap_sessions(rows, 180)
    assert effective == 2
    assert guard.dependence_block_length(rows, 180) >= 6


def test_block_bootstrap_counterexample_is_more_conservative_than_iid_scale():
    values = [1.0] * 10 + [1.0] * 10 + [0.5] * 10 + [-1.0] * 10
    lower = guard.circular_block_bootstrap_lower(
        values, seed=20260826, reps=5000, quantile=0.10, block_length=10
    )
    assert lower <= 0.0


def test_evidence_uses_joint_states_and_normalized_returns():
    current = current_session(edge=0.02, notional=200.0, target=100.0, burden=2.0)
    rows = []
    states = [3, 1, 2, 0, 3, 1, 2, 0]
    for i, state in enumerate(states):
        rows.append(descriptor_session(
            edge=0.015,
            notional=205.0,
            target=105.0,
            burden=2.2,
            origin=1_000_000 + i * 200_000,
            state_mask=state,
            pnl_return=0.01,
        ))
    result = guard.evidence_for(current, rows, min_sessions=4, reps=1000, quantile=0.10)
    assert result["comparable_sessions"] == 8
    assert result["effective_nonoverlap_sessions"] == 8
    assert result["joint_state_counts"] == {"0": 2, "1": 2, "2": 2, "3": 2}
    assert result["accepted"] is True
    assert abs(result["stress"]["2x"]["transported_mean_pnl_current_notional"] - 2.0) < 1e-12


def test_easy_profitable_history_cannot_authorize_harder_current_candidate():
    current = current_session(edge=0.00005, notional=200.0, target=200.0, burden=5.0)
    easy = [
        descriptor_session(edge=0.02, notional=50.0, target=50.0, burden=0.5,
                           origin=1_000_000 + i * 200_000, pnl_return=1.0)
        for i in range(20)
    ]
    result = guard.evidence_for(current, easy, min_sessions=4, reps=1000, quantile=0.10)
    assert result["comparable_sessions"] == 0
    assert result["accepted"] is False


def test_negative_2x_lower_bound_blocks_route_even_when_1x_is_positive():
    current = current_session(edge=0.02, notional=100.0, target=100.0, burden=2.0)
    rows = []
    for i in range(10):
        row = descriptor_session(edge=0.015, notional=105.0, target=105.0, burden=2.2,
                                 origin=1_000_000 + i * 200_000, pnl_return=0.02)
        row["stress_return_on_notional"] = {"1x": 0.02, "1.5x": 0.005, "2x": -0.005}
        rows.append(row)
    result = guard.evidence_for(current, rows, min_sessions=4, reps=1000, quantile=0.10)
    assert result["stress"]["1x"]["block_bootstrap_lower_return"] > 0.0
    assert result["stress"]["2x"]["block_bootstrap_lower_return"] < 0.0
    assert result["accepted"] is False


def test_runtime_contract_never_uses_quoted_expected_edge_as_observed_pnl():
    text = (ROOT / "scripts/v7_graph_execution_guard.py").read_text(encoding="utf-8")
    assert "quoted_expected_edge_is_not_observed_pnl" in text
    assert "full_completion_payout_minus_executed_entry_costs" in text
    assert "chronological_circular_block_bootstrap" in text
    assert "stress_return_on_notional" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 Graph executable/dependence tests")
