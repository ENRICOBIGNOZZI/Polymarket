#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_transport_guard as guard


def session(*, edge: float = 0.02, notional: float = 100.0, target: float = 100.0,
            burden: float = 2.0, quote_ticks: float = 0.0, pnl: float = 1.0) -> dict:
    return {
        "signature": "GRAPH_RV:event:yes-no",
        "full_completion": True,
        "stress_pnl": {"1x": pnl, "1.5x": pnl, "2x": pnl},
        "transport": {
            "descriptor_version": 1,
            "window_seconds": 180,
            "quote_policy": "maker_limit_ticks_from_bid",
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
                    "key": "m2:NO",
                    "target_shares": target,
                    "required_flow_to_target": burden,
                    "quote_ticks_from_bid": quote_ticks,
                },
            ],
        },
    }


def test_comparable_or_harder_history_is_allowed():
    current = session(edge=0.02, notional=100.0, target=100.0, burden=2.0)
    historical = session(edge=0.015, notional=105.0, target=105.0, burden=2.2)
    ok, reasons = guard.comparable_session(historical, current)
    assert ok, reasons


def test_more_favorable_historical_edge_is_rejected():
    current = session(edge=0.02)
    historical = session(edge=0.03)
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert "historical_edge_too_favorable" in reasons


def test_smaller_historical_size_is_rejected():
    current = session(notional=100.0, target=100.0)
    historical = session(notional=70.0, target=70.0)
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert "historical_notional_too_small" in reasons
    assert any(reason.startswith("historical_target_too_small:") for reason in reasons)


def test_easier_historical_queue_burden_is_rejected():
    current = session(burden=3.0)
    historical = session(burden=1.0)
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert any(reason.startswith("historical_queue_burden_too_easy:") for reason in reasons)


def test_quote_policy_and_horizon_must_match():
    current = session()
    historical = session()
    historical["transport"]["window_seconds"] = 60
    historical["transport"]["quote_policy"] = "other"
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert "horizon" in reasons
    assert "quote_policy" in reasons


def test_descriptorless_legacy_evidence_fails_closed():
    current = session()
    historical = {"signature": current["signature"], "full_completion": True, "stress_pnl": {"1x": 5.0, "1.5x": 5.0, "2x": 5.0}}
    ok, reasons = guard.comparable_session(historical, current)
    assert not ok
    assert reasons == ["descriptor_missing"]


def test_evidence_uses_only_state_comparable_sessions():
    current = session(edge=0.02, notional=100.0, target=100.0, burden=2.0)
    comparable = [session(edge=0.015, notional=105.0, target=105.0, burden=2.2, pnl=1.0) for _ in range(4)]
    easy = [session(edge=0.04, notional=40.0, target=40.0, burden=0.5, pnl=100.0) for _ in range(4)]
    result = guard.evidence_for(current, comparable + easy, min_sessions=4, reps=100, quantile=0.10)
    assert result["structural_sessions"] == 8
    assert result["comparable_sessions"] == 4
    assert result["accepted"] is True
    assert result["rejected_by_transport"]["historical_edge_too_favorable"] == 4
    assert result["rejected_by_transport"]["historical_notional_too_small"] == 4


def test_easy_positive_history_cannot_authorize_harder_current_state():
    current = session(edge=0.01, notional=200.0, target=200.0, burden=4.0)
    easy = [session(edge=0.05, notional=50.0, target=50.0, burden=0.5, pnl=100.0) for _ in range(20)]
    result = guard.evidence_for(current, easy, min_sessions=4, reps=100, quantile=0.10)
    assert result["comparable_sessions"] == 0
    assert result["accepted"] is False


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 Graph transport tests")
