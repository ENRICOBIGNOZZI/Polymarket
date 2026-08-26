#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_roundtrip_guard as guard


def evidence_row(*, origin: int, state: int = 3, ret: float = 0.01, edge: float = 0.01,
                 notional: float = 100.0, target: float = 100.0, burden: float = 2.0) -> dict:
    return {
        "evidence_version": 3,
        "signature": "GRAPH_RV|e|m1:YES|m2:YES",
        "origin_received_ms": origin,
        "state_mask": state,
        "full_mask": 3,
        "full_completion": state == 3,
        "stress_return_on_notional": {"1x": ret, "1.5x": ret, "2x": ret},
        "execution_descriptor": {
            "descriptor_version": 3,
            "window_seconds": 180,
            "quote_policy": "maker_limit_ticks_from_bid",
            "pnl_contract": "fixed_horizon_depth_aware_roundtrip_liquidation",
            "terminal_floor_reason": "not_used",
            "expected_edge": edge,
            "max_notional": notional,
            "legs": [
                {"key": "m1:YES", "target_shares": target, "required_flow_to_target": burden, "quote_ticks_from_bid": 0.0},
                {"key": "m2:YES", "target_shares": target, "required_flow_to_target": burden, "quote_ticks_from_bid": 0.0},
            ],
        },
        "legs": [{"market_id": "m1", "side": "YES"}, {"market_id": "m2", "side": "YES"}],
    }


def current_row(**kwargs) -> dict:
    row = evidence_row(origin=1_000_000, **kwargs)
    row.pop("evidence_version")
    row.pop("stress_return_on_notional")
    return row


def test_roundtrip_accounting_does_not_use_terminal_payout():
    pnl = guard.roundtrip_pnl_components(
        entry_price_cash=98.0,
        entry_fee_cash=1.0,
        exit_cash_before_fee=99.0,
        exit_fee_cash=0.5,
        capital_time_cash=0.25,
        cost_multiplier=2.0,
    )
    assert abs(pnl + 2.5) < 1e-12


def test_full_and_partial_states_share_same_pnl_contract():
    text = (ROOT / "scripts/v7_graph_roundtrip_guard_core.py").read_text(encoding="utf-8")
    assert "terminal_payout_floor_assumed\": False" in text
    assert "neg_risk_exactly_one_yes_assumed\": False" in text
    assert "full_and_partial_states_share_executable_liquidation_accounting\": True" in text
    assert "fixed_horizon_depth_aware_roundtrip_liquidation" in text
    assert "verified_terminal_payout_floor" not in text


def test_runtime_adapter_binds_core_counter_explicitly():
    text = (ROOT / "scripts/v7_graph_roundtrip_guard.py").read_text(encoding="utf-8")
    assert "core.Counter = Counter" in text
    assert "raise SystemExit(core.main())" in text


def test_v2_terminal_floor_evidence_cannot_enter_v3():
    current = current_row()
    legacy = evidence_row(origin=800_000)
    legacy["evidence_version"] = 2
    ok, reasons = guard.comparable_session(legacy, current)
    assert not ok
    assert reasons == ["legacy_evidence_schema"]


def test_easy_history_cannot_authorize_harder_current_state():
    current = current_row(edge=0.005, notional=200.0, target=200.0, burden=5.0)
    easy = [
        evidence_row(origin=1_000_000 + i * 200_000, edge=0.02, notional=50.0, target=50.0, burden=0.5, ret=0.5)
        for i in range(20)
    ]
    result = guard.evidence_for(current, easy, 4, 1000, 0.10)
    assert result["comparable_sessions"] == 0
    assert result["accepted"] is False


def test_joint_state_distribution_and_block_inference_remain_required():
    current = current_row(edge=0.02, notional=100.0, target=100.0, burden=2.0)
    states = [3, 1, 2, 0, 3, 1, 2, 0]
    rows = [
        evidence_row(origin=1_000_000 + i * 200_000, state=state, edge=0.015,
                     notional=105.0, target=105.0, burden=2.2, ret=0.01)
        for i, state in enumerate(states)
    ]
    result = guard.evidence_for(current, rows, 4, 1000, 0.10)
    assert result["joint_state_counts"] == {"0": 2, "1": 2, "2": 2, "3": 2}
    assert result["effective_nonoverlap_sessions"] == 8
    assert result["dependence_block_length"] >= 3
    assert result["accepted"] is True


def test_overlapping_sessions_do_not_satisfy_effective_minimum():
    current = current_row()
    rows = [evidence_row(origin=1_000_000 + i * 30_000) for i in range(10)]
    result = guard.evidence_for(current, rows, 4, 1000, 0.10)
    assert result["comparable_sessions"] == 10
    assert result["effective_nonoverlap_sessions"] == 2
    assert result["accepted"] is False


def test_negative_2x_executable_roundtrip_lower_bound_blocks():
    current = current_row(edge=0.02)
    rows = []
    for i in range(10):
        row = evidence_row(origin=1_000_000 + i * 200_000, edge=0.015, notional=105.0, target=105.0, burden=2.2)
        row["stress_return_on_notional"] = {"1x": 0.02, "1.5x": 0.005, "2x": -0.005}
        rows.append(row)
    result = guard.evidence_for(current, rows, 4, 1000, 0.10)
    assert result["stress"]["1x"]["block_bootstrap_lower_return"] > 0.0
    assert result["stress"]["2x"]["block_bootstrap_lower_return"] < 0.0
    assert result["accepted"] is False


def test_runtime_routes_graph_to_v3_roundtrip_guard():
    text = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "v7_graph_roundtrip_guard.py" in text
    assert "--state \"$RUN_ROOT/graph_roundtrip_state.json\"" in text
    graph_block = text[text.index("run_graph(){"):text.index("reap_stale_proxy")]
    assert "v7_graph_execution_guard.py" not in graph_block
    assert "v7_graph_forward_guard.py" not in graph_block


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 Graph round-trip tests")
