#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_hard_arb_guard as hard


def _abort_state(shares: float = 5.0) -> dict[str, object]:
    return {
        "bundle": {
            "legs": [
                {
                    "token": "yes-a",
                    "shares": shares,
                    "raw_market": {"conditionId": "condition-a"},
                }
            ]
        }
    }


def test_abort_mark_fails_closed_when_visible_depth_cannot_liquidate_full_size():
    old_fetch = hard.fetch_books
    old_resolve = hard.resolve_fee_details
    try:
        hard.fetch_books = lambda _clob, _tokens, _stats=None: {
            "yes-a": {"bids": [(0.49, 2.0)]}
        }
        hard.resolve_fee_details = lambda *_args, **_kwargs: hard.FeeDetails(
            0.0, 1.0, True, True, "test:verified-free"
        )
        stats: dict[str, int] = {}
        value = hard.executable_abort_mark(
            "https://clob.invalid",
            _abort_state(5.0),
            5.0,
            stats,
        )
        assert value == 0.0
    finally:
        hard.fetch_books = old_fetch
        hard.resolve_fee_details = old_resolve


def test_abort_mark_uses_full_depth_executable_liquidation_value():
    old_fetch = hard.fetch_books
    old_resolve = hard.resolve_fee_details
    try:
        hard.fetch_books = lambda _clob, _tokens, _stats=None: {
            "yes-a": {"bids": [(0.49, 3.0), (0.48, 4.0)]}
        }
        hard.resolve_fee_details = lambda *_args, **_kwargs: hard.FeeDetails(
            0.0, 1.0, True, True, "test:verified-free"
        )
        stats: dict[str, int] = {}
        value = hard.executable_abort_mark(
            "https://clob.invalid",
            _abort_state(5.0),
            5.0,
            stats,
        )
        top_of_book_mark = 5.0 * 0.49
        expected_raw = 3.0 * 0.49 + 2.0 * 0.48
        expected_stressed = expected_raw * (1.0 - 5.0 / 10000.0)
        assert abs(value - expected_stressed) < 1e-12
        assert value < top_of_book_mark
    finally:
        hard.fetch_books = old_fetch
        hard.resolve_fee_details = old_resolve


def test_hard_arb_native_source_has_no_superseded_runtime_dependency():
    text = (SCRIPTS / "v7_hard_arb_guard.py").read_text(encoding="utf-8")
    assert "import v7_" not in text
    assert "from v7_" not in text
    assert "hard_legacy" not in text
    assert "micro_legacy" not in text
    assert "v7_market_common" in text
    assert '"legacy_runtime_dependency": False' in text
    assert '"aborting_mark": "full_depth_executable_liquidation_net_exit_fee_fail_closed"' in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 hard-arb executable-mark tests")
