#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_maker_cancel_latency as cancel
import v7_micro_maker_worker as maker


def base_order() -> dict:
    return {
        "market_id": "m1",
        "created_event_ms": 1_000,
        "created_received_ms": 1_050,
        "created_ts": 1,
        "token_id": "tok",
        "remaining_shares": 5.0,
        "limit_price": 0.40,
    }


def test_cancel_request_has_latency_and_grace() -> None:
    order = base_order()
    cancel.request_cancel(order, processing_ms=10_000, latency_ms=100, grace_ms=30_000, reason="CANCEL_TTL")
    assert order["order_state"] == "CANCEL_PENDING"
    assert order["cancel_requested_received_ms"] == 10_000
    assert order["cancel_effective_event_ms"] == 10_100
    assert order["cancel_finalize_received_ms"] == 40_100


def test_fill_before_effective_cancel_remains_valid() -> None:
    order = base_order()
    cancel.request_cancel(order, processing_ms=10_000, latency_ms=100, grace_ms=30_000, reason="CANCEL_TTL")
    row = {"timestamp": "10.05", "received_ms": "10120"}
    assert cancel.causal_fill_eligible(row, order, processing_ms=10_120, ttl_seconds=1)
    row_after = {"timestamp": "10.2", "received_ms": "10250"}
    assert not cancel.causal_fill_eligible(row_after, order, processing_ms=10_250, ttl_seconds=1)


def test_delayed_pre_cancel_trade_is_allowed_during_grace() -> None:
    order = base_order()
    cancel.request_cancel(order, processing_ms=10_000, latency_ms=100, grace_ms=30_000, reason="CANCEL_TTL")
    row = {"timestamp": "10.09", "received_ms": "25000"}
    assert cancel.causal_fill_eligible(row, order, processing_ms=25_000, ttl_seconds=1)


def test_cancel_aware_delete_intercepts_only_requested_cancel() -> None:
    request = {"reason": ""}

    def on_cancel(_key: str, order: dict) -> bool:
        if not request["reason"]:
            return False
        cancel.request_cancel(order, processing_ms=10_000, latency_ms=100, grace_ms=30_000, reason=request["reason"])
        request["reason"] = ""
        return True

    orders = cancel.CancelAwareOrders({"m1": base_order()}, on_cancel=on_cancel)
    request["reason"] = "CANCEL_TTL"
    del orders["m1"]
    assert "m1" in orders
    assert orders["m1"]["order_state"] == "CANCEL_PENDING"
    request["reason"] = ""
    del orders["m1"]
    assert "m1" not in orders


def test_finalize_waits_for_latency_plus_grace_and_syncs_exports() -> None:
    with tempfile.TemporaryDirectory() as temp:
        run_dir = Path(temp)
        state_path = run_dir / "state.json"
        status_path = run_dir / "status.json"
        orders_path = run_dir / "maker_orders.csv"
        order = base_order()
        cancel.request_cancel(order, processing_ms=10_000, latency_ms=100, grace_ms=30_000, reason="CANCEL_TTL")
        state_path.write_text(json.dumps({"orders": {"m1": order}, "resting_orders": 1}), encoding="utf-8")
        status_path.write_text(json.dumps({"orders": {"m1": order}, "resting_orders": 1}), encoding="utf-8")
        with orders_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["market_id", "token_id"])
            writer.writeheader()
            writer.writerow({"market_id": "m1", "token_id": "tok"})
        assert cancel.finalize_due_cancels(run_dir, processing_ms=40_099) == []
        rows = cancel.finalize_due_cancels(run_dir, processing_ms=40_100)
        assert len(rows) == 1
        state = json.loads(state_path.read_text(encoding="utf-8"))
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert state["orders"] == {} and status["orders"] == {}
        assert state["resting_orders"] == 0 and status["resting_orders"] == 0
        with orders_path.open(newline="", encoding="utf-8") as handle:
            assert list(csv.DictReader(handle)) == []


def test_execution_state_requires_market_and_book_continuity_before_replay() -> None:
    state = {
        "orders": {"m1": {"market_id": "m1", "token_id": "tok1"}},
        "positions": {"m2": {"market_id": "m2", "token_id": "tok2"}},
        "markout_watch": {"w3": {"market_id": "m3", "token_id": "tok3"}},
    }
    assert maker.tracked_market_token_pairs(state) == {
        ("m1", "tok1"),
        ("m2", "tok2"),
    }
    assert maker.markout_market_token_pairs(state) == {("m3", "tok3")}
    gaps = maker.replay_continuity_gaps(
        state,
        discovered_market_ids={"m1"},
        book_tokens={"tok1"},
    )
    assert gaps == {"missing_market_ids": ["m2"], "missing_book_tokens": ["tok2"]}
    assert maker.replay_continuity_gaps(
        state,
        discovered_market_ids={"m1", "m2"},
        book_tokens={"tok1", "tok2"},
    ) == {"missing_market_ids": [], "missing_book_tokens": []}


def test_markout_only_gap_does_not_block_execution_replay() -> None:
    state = {
        "orders": {"m1": {"market_id": "m1", "token_id": "tok1"}},
        "markout_watch": {"w2": {"market_id": "m2", "token_id": "tok2"}},
    }
    assert maker.replay_continuity_gaps(
        state,
        discovered_market_ids={"m1"},
        book_tokens={"tok1"},
    ) == {"missing_market_ids": [], "missing_book_tokens": []}
    assert maker.markout_continuity_gaps(
        state,
        discovered_market_ids={"m1"},
        book_tokens={"tok1"},
    ) == {"missing_market_ids": ["m2"], "missing_book_tokens": ["tok2"]}


def test_empty_or_untracked_state_does_not_block_research_discovery() -> None:
    assert maker.tracked_market_token_pairs({}) == set()
    assert maker.markout_market_token_pairs({}) == set()
    assert maker.replay_continuity_gaps(
        {}, discovered_market_ids=set(), book_tokens=set()
    ) == {"missing_market_ids": [], "missing_book_tokens": []}


def test_canonical_worker_fails_closed_before_replay_when_execution_continuity_is_missing() -> None:
    text = (SCRIPTS / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "REPLAY_CONTINUITY_CONTRACT" in text
    assert "execution_state_market_and_token_book_required_before_tape_replay" in text
    assert "MARKOUT_CONTINUITY_CONTRACT" in text
    assert "measurement_gap_never_blocks_execution_and_labels_remain_bounded_delay" in text
    assert "event.load_tape = patched_load_tape" in text
    assert "raise ReplayContinuityError" in text
    assert "FAIL_CLOSED_NO_REPLAY_NO_CANCEL" in text
    assert "MARKOUT_GAP_DOES_NOT_BLOCK_EXECUTION" in text
    assert text.index("event.load_tape = patched_load_tape") < text.index("rc = depth.main()")
    assert text.index("continuity_block is not None") < text.index("finalize_due_cancels")


def test_canonical_worker_replays_before_finalizing_pending_cancel() -> None:
    text = (SCRIPTS / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "v7_micro_maker_worker_depth_core" in text
    assert "v7_maker_cancel_latency" in text
    assert "CANCEL_REQUEST_TTL" in text
    assert "CancelAwareOrders" in text
    assert "--cancel-latency-ms" in text
    assert "--cancel-tape-grace-ms" in text
    assert text.index("rc = depth.main()") < text.index("finalize_due_cancels")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} maker cancel-latency tests")
