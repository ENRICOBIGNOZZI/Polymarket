#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_hard_arb_core as core


def _book(ask: float, shares: float = 100.0) -> dict[str, object]:
    return {
        "asks": [(ask, shares)],
        "bids": [(max(0.01, ask - 0.02), shares)],
        "ask": ask,
        "size": shares,
        "min_order": 1.0,
        "ask_depth": shares,
    }


def test_v7_hard_arb_core_self_test():
    assert core.self_test() == 0


def test_full_depth_plan_and_sizing_remain_post_cost():
    zero_fee = core.parse_fee_details({"feesEnabled": False})
    assert zero_fee is not None and zero_fee.enabled is False
    live = {"yes-a": _book(0.45), "yes-b": _book(0.45)}
    fees = {"yes-a": zero_fee, "yes-b": zero_fee}
    plan = core._plan(live, ["yes-a", "yes-b"], fees, 10.0, 0.0)
    assert plan is not None
    assert abs(plan[0] - 9.0) < 1e-12
    sized = core._size(
        live,
        ["yes-a", "yes-b"],
        fees,
        1.0,
        20.0,
        100.0,
        0.02,
        0.0,
    )
    assert sized is not None
    assert sized[0] > 19.99
    assert 1.0 - sized[1] / sized[0] > 0.02


def test_negative_post_cost_complete_set_is_rejected():
    zero_fee = core.parse_fee_details({"feesEnabled": False})
    assert zero_fee is not None
    live = {"yes-a": _book(0.51), "yes-b": _book(0.51)}
    fees = {"yes-a": zero_fee, "yes-b": zero_fee}
    assert core._size(
        live,
        ["yes-a", "yes-b"],
        fees,
        1.0,
        20.0,
        100.0,
        0.0002,
        0.0,
    ) is None


def test_unwind_book_walk_can_report_residual_without_assumed_fill():
    details = core.FeeDetails(True, 0.07, 1.0, True, "test")
    fill = core.walk_book_for_shares(
        [(0.49, 2.0)],
        5.0,
        details,
        buy=False,
        slippage_bps=5.0,
        require_full=False,
    )
    assert fill is not None
    assert fill.filled_shares == 2.0
    assert fill.complete is False
    assert fill.fee > 0.0


def test_hard_arb_runtime_closure_has_no_legacy_imports():
    core_source = (SCRIPTS / "v7_hard_arb_core.py").read_text(encoding="utf-8")
    guard_source = (SCRIPTS / "v7_hard_arb_guard.py").read_text(encoding="utf-8")
    combined = core_source + "\n" + guard_source
    assert "import v6_" not in combined
    assert "from v6_" not in combined
    assert "hard_legacy" not in combined
    assert "import v7_hard_arb_core as q" in guard_source
    assert "authenticated_execution\": False" in core_source
    assert "sequential_leg_revalidation\": True" in core_source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 hard-arb core tests")
