#!/usr/bin/env python3
from __future__ import annotations

import sys
import json
import tempfile
import time
from argparse import Namespace
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


def test_hard_arb_native_source_has_no_v6_runtime_dependency():
    text = (SCRIPTS / "v7_hard_arb_guard.py").read_text(encoding="utf-8")
    assert "import v6_" not in text
    assert "from v6_" not in text
    assert "v7_market_common" in text
    assert "runtime_dependency" not in text
    assert '"aborting_mark": "full_depth_executable_liquidation_net_exit_fee_fail_closed"' in text


def test_configured_shared_bus_never_falls_back_to_rest():
    sha = "d" * 40
    now = int(time.time() * 1000)
    raw = {
        "schema": "polymarket_v7_shared_market_state_v1", "timestamp_ms": now,
        "snapshot_id": "atomic-1", "generation": 1, "model_sha": sha,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False,
        "books": [{
            "token_id": "yes-a", "market_id": "m", "condition_id": "c",
            "event_id": "e", "outcome": "YES", "exchange_ts_ms": now - 20,
            "receive_ts_ms": now - 10, "state_version": 1, "lineage_epoch": 1,
            "lineage_continuous": True, "provenance": "WEBSOCKET",
            "tick_size": .01, "min_order_size": 1,
            "bids": [{"price": .49, "size": 10}],
            "asks": [{"price": .50, "size": 10}], "fee_verified": True,
            "fee_rate": 0, "fee_exponent": 1, "fee_taker_only": True,
        }],
    }
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "shared.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        args = Namespace(shared_state=path, model_sha=sha, max_shared_publish_age_ms=2500)
        stats: dict[str, object] = {}
        selected = hard.execution_books(args, "https://clob.invalid", ["yes-a"], stats)
        assert selected["yes-a"]["bus_snapshot_id"] == "atomic-1"
        assert stats["shared_state_reads"] == 1
        # A missing token is a fail-closed bus rejection, not a REST fetch.
        assert hard.execution_books(args, "https://clob.invalid", ["missing"], stats) == {}
        assert stats.get("rest_execution_book_reads", 0) == 0


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 hard-arb executable-mark tests")
