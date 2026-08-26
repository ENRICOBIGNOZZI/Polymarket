#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import lf_v7_graph_evidence_transport_audit as transport
import v7_graph_forward_guard as guard


def test_signature_is_stable_and_event_scoped():
    rows = [
        {"strategy": "GRAPH_RV", "event_id": "e", "market_id": "b", "side": "YES"},
        {"strategy": "GRAPH_RV", "event_id": "e", "market_id": "a", "side": "NO"},
    ]
    assert guard.signature(rows) == "GRAPH_RV|e|a:NO|b:YES"


def test_bootstrap_lower_is_minimum_for_tiny_samples():
    assert guard.bootstrap_lower([1.0, -2.0, 3.0, 4.0], 1, 400, 0.1) == -2.0


def test_evidence_requires_full_completion_and_positive_all_stresses():
    completed = []
    for i in range(4):
        completed.append({
            "signature": "s",
            "full_completion": i == 0,
            "stress_pnl": {"1x": 1.0, "1.5x": 0.5, "2x": 0.1},
        })
    result = guard.evidence_for("s", completed, 4, 200, 0.1)
    assert result["accepted"] is True
    completed[2]["stress_pnl"]["2x"] = -0.1
    result = guard.evidence_for("s", completed, 4, 200, 0.1)
    assert result["accepted"] is False


def test_no_full_completion_never_routes():
    completed = [
        {"signature": "s", "full_completion": False, "stress_pnl": {"1x": 1.0, "1.5x": 1.0, "2x": 1.0}}
        for _ in range(10)
    ]
    assert guard.evidence_for("s", completed, 4, 200, 0.1)["accepted"] is False


def test_structural_signature_can_transport_positive_history_to_incomparable_current_state():
    signature = "GRAPH_RV|event|a:YES|b:YES"
    completed = [
        {
            "signature": signature,
            "window_seconds": 180,
            "expected_edge": 0.02,
            "required_flow": [100.0, 100.0],
            "target_shares": [10.0, 10.0],
            "full_completion": True,
            "stress_pnl": {"1x": 1.0, "1.5x": 0.8, "2x": 0.5},
        }
        for _ in range(4)
    ]
    current = {
        "signature": signature,
        "window_seconds": 180,
        "expected_edge": 0.00005,
        "required_flow": [1000.0, 1000.0],
        "target_shares": [50.0, 50.0],
    }
    assert guard.evidence_for(signature, completed, 4, 200, 0.1)["accepted"] is True
    assert transport.transportable_sessions(current, completed) == []


def test_transportability_accepts_only_weakly_easier_higher_edge_current_state():
    signature = "GRAPH_RV|event|a:YES|b:YES"
    historical = {
        "signature": signature,
        "window_seconds": 180,
        "expected_edge": 0.02,
        "required_flow": [100.0, 120.0],
        "target_shares": [10.0, 12.0],
    }
    easier = {
        "signature": signature,
        "window_seconds": 180,
        "expected_edge": 0.03,
        "required_flow": [50.0, 100.0],
        "target_shares": [5.0, 10.0],
    }
    lower_edge = dict(easier, expected_edge=0.01)
    harder_queue = dict(easier, required_flow=[101.0, 100.0])
    larger_target = dict(easier, target_shares=[11.0, 10.0])
    wrong_horizon = dict(easier, window_seconds=300)
    assert transport.transportable_session(easier, historical) is True
    assert transport.transportable_session(lower_edge, historical) is False
    assert transport.transportable_session(harder_queue, historical) is False
    assert transport.transportable_session(larger_target, historical) is False
    assert transport.transportable_session(wrong_horizon, historical) is False


def test_source_contract_is_dual_clock_and_prospective():
    text = (ROOT / "scripts/v7_graph_forward_guard.py").read_text(encoding="utf-8")
    assert "origin_received_ms" in text
    assert "origin_event_ms" in text
    assert "deadline_received_ms" in text
    assert "deadline_event_ms" in text
    assert "no_current_book_historical_replay" in text
    assert "positive_bootstrap_lower_before_route" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} graph forward guard tests")
