#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_forward_guard as guard
from test_v7_graph_transport_guard import *  # noqa: F401,F403
from test_v7_graph_execution_guard import *  # noqa: F401,F403
from test_v7_graph_roundtrip_guard import *  # noqa: F401,F403


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
    print(f"ok {len(tests)} graph forward/transport/executable/roundtrip guard tests")
