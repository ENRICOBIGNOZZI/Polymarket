#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_maker_cancel_latency as cancel


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
    # Event occurred before the effective cancel but REST receipt is much later.
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


def test_finalize_waits_for_latency_plus_grace() -> None:
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "state.json"
        order = base_order()
        cancel.request_cancel(order, processing_ms=10_000, latency_ms=100, grace_ms=30_000, reason="CANCEL_TTL")
        path.write_text(json.dumps({"orders": {"m1": order}}), encoding="utf-8")
        assert cancel.finalize_due_cancels(path, processing_ms=40_099) == []
        rows = cancel.finalize_due_cancels(path, processing_ms=40_100)
        assert len(rows) == 1
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["orders"] == {}


def test_canonical_worker_wraps_depth_core_with_cancel_state() -> None:
    text = (SCRIPTS / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "v7_micro_maker_worker_depth_core" in text
    assert "v7_maker_cancel_latency" in text
    assert "CANCEL_REQUEST_TTL" in text
    assert "CancelAwareOrders" in text
    assert "--cancel-latency-ms" in text
    assert "--cancel-tape-grace-ms" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} maker cancel-latency tests")
