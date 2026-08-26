#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_execution_guard as helper


def descriptor_session(
    *,
    edge: float = 0.02,
    notional: float = 100.0,
    target: float = 100.0,
    burden: float = 2.0,
    quote_ticks: float = 0.0,
    origin: int = 1_000_000,
) -> dict:
    descriptor = {
        "descriptor_version": 2,
        "window_seconds": 180,
        "quote_policy": "maker_limit_ticks_from_bid",
        "pnl_contract": "fixed_horizon_depth_aware_roundtrip_liquidation",
        "expected_edge": edge,
        "max_notional": notional,
        "legs": [
            {
                "key": "m1:YES",
                "target_shares": target,
                "required_flow_to_target": burden,
                "quote_ticks_from_bid": quote_ticks,
            },
            {
                "key": "m2:YES",
                "target_shares": target,
                "required_flow_to_target": burden,
                "quote_ticks_from_bid": quote_ticks,
            },
        ],
    }
    return {
        "evidence_version": 2,
        "signature": "GRAPH_RV|e|m1:YES|m2:YES",
        "origin_received_ms": origin,
        "execution_descriptor": descriptor,
    }


def current_session(**kwargs) -> dict:
    row = descriptor_session(**kwargs)
    row.pop("evidence_version", None)
    return row


def test_helper_surface_contains_no_alternate_terminal_economics():
    text = (ROOT / "scripts/v7_graph_execution_guard.py").read_text(encoding="utf-8")
    assert "terminal_payout_floor_cash" not in text
    assert "full_completion_stress_pnl" not in text
    assert "verified_terminal_payout_floor" not in text
    assert "helper-only module" in text
    assert "canonical runtime owner is v7_graph_roundtrip_guard.py" in text


def test_more_favorable_historical_edge_cannot_transport():
    current = current_session(edge=0.01)
    historical = descriptor_session(edge=0.03)
    ok, reasons = helper.comparable_session(historical, current)
    assert not ok
    assert "historical_edge_too_favorable" in reasons


def test_smaller_or_easier_historical_execution_state_cannot_transport():
    current = current_session(notional=200.0, target=200.0, burden=4.0)
    historical = descriptor_session(notional=50.0, target=50.0, burden=0.5)
    ok, reasons = helper.comparable_session(historical, current)
    assert not ok
    assert "historical_notional_too_small" in reasons
    assert any(reason.startswith("historical_target_too_small:") for reason in reasons)
    assert any(reason.startswith("historical_queue_burden_too_easy:") for reason in reasons)


def test_legacy_state_blind_evidence_fails_closed():
    current = current_session()
    legacy = {"signature": current["signature"]}
    ok, reasons = helper.comparable_session(legacy, current)
    assert not ok
    assert reasons == ["legacy_evidence_schema"]


def test_quote_policy_or_pnl_contract_mismatch_fails_transport():
    current = current_session()
    historical = descriptor_session()
    historical["execution_descriptor"]["quote_policy"] = "different"
    historical["execution_descriptor"]["pnl_contract"] = "different"
    ok, reasons = helper.comparable_session(historical, current)
    assert not ok
    assert "quote_policy" in reasons
    assert "pnl_contract" in reasons


def test_overlapping_windows_do_not_count_as_independent_sessions():
    rows = [descriptor_session(origin=1_000_000 + 30_000 * i) for i in range(10)]
    effective = helper.effective_nonoverlap_sessions(rows, 180)
    assert effective == 2
    assert helper.dependence_block_length(rows, 180) >= 6


def test_block_bootstrap_counterexample_is_dependence_conservative():
    values = [1.0] * 10 + [1.0] * 10 + [0.5] * 10 + [-1.0] * 10
    lower = helper.circular_block_bootstrap_lower(
        values, seed=20260826, reps=5000, quantile=0.10, block_length=10
    )
    assert lower <= 0.0


def test_dual_clock_fill_simulation_requires_both_clocks_inside_window():
    session = {
        "origin_received_ms": 1_000,
        "deadline_received_ms": 2_000,
        "origin_event_ms": 1_000,
        "deadline_event_ms": 2_000,
        "legs": [
            {
                "token": "a",
                "limit_price": 0.50,
                "queue_ahead": 2.0,
                "target_shares": 3.0,
            }
        ],
    }
    tape = [
        {"token": "a", "side": "SELL", "price": 0.49, "size": 100.0, "received_ms": 900, "event_ms": 1_500},
        {"token": "a", "side": "SELL", "price": 0.49, "size": 100.0, "received_ms": 1_500, "event_ms": 900},
        {"token": "a", "side": "SELL", "price": 0.49, "size": 5.0, "received_ms": 1_500, "event_ms": 1_500},
    ]
    legs, filled, mask, full_mask = helper._simulate_fills(session, tape)
    assert len(legs) == 1
    assert filled == [3.0]
    assert mask == full_mask == 1


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} neutral Graph execution-state helper tests")
