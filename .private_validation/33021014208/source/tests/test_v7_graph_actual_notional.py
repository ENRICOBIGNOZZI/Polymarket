#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_roundtrip_guard as guard


def test_attach_replaces_budget_ceiling_with_actual_target_cash():
    original = guard._original_attach
    try:
        def fake_attach(session, _clob, _window):
            return ({
                **session,
                "max_notional": 200.0,
                "legs": [
                    {"market_id": "m1", "side": "YES", "target_shares": 20.0, "limit_price": 0.40},
                    {"market_id": "m2", "side": "NO", "target_shares": 10.0, "limit_price": 0.30},
                ],
                "execution_descriptor": {
                    "descriptor_version": 3,
                    "max_notional": 200.0,
                    "legs": [
                        {"key": "m1:YES", "entry_fee_per_share": 0.01},
                        {"key": "m2:NO", "entry_fee_per_share": 0.02},
                    ],
                },
            }, "ok")

        guard._original_attach = fake_attach
        guard.attach_roundtrip_descriptor.__globals__["_original_attach"] = fake_attach
        enriched, reason = guard.attach_roundtrip_descriptor({"signature": "s"}, "clob", 180)
        assert reason == "ok"
        assert enriched is not None
        expected = 20.0 * 0.41 + 10.0 * 0.32
        assert abs(enriched["actual_target_notional"] - expected) < 1e-12
        assert abs(enriched["max_notional"] - expected) < 1e-12
        descriptor = enriched["execution_descriptor"]
        assert descriptor["quoted_max_notional"] == 200.0
        assert abs(descriptor["max_notional"] - expected) < 1e-12
    finally:
        guard._original_attach = original
        guard.attach_roundtrip_descriptor.__globals__["_original_attach"] = original


def test_stale_liquidation_label_fails_before_book_accounting():
    original_now = guard.core.v2.base.now_ms
    original_mature = guard._original_mature
    called = {"value": False}
    try:
        guard.core.v2.base.now_ms = lambda: 1_000_000 + 180_000 + 46_000
        def fake_mature(*_args, **_kwargs):
            called["value"] = True
            return {"liquidation_book_received_ms": 1_000_000 + 180_000 + 46_000}
        guard._original_mature = fake_mature
        guard.mature_session.__globals__["_original_mature"] = fake_mature
        result = guard.mature_session(
            {"origin_received_ms": 1_000_000, "deadline_received_ms": 1_180_000},
            [], "gamma", "clob", 5.0, 0.25,
        )
        assert result is None
        assert called["value"] is False
    finally:
        guard.core.v2.base.now_ms = original_now
        guard._original_mature = original_mature
        guard.mature_session.__globals__["_original_mature"] = original_mature


def test_bounded_liquidation_label_records_effective_horizon():
    original_now = guard.core.v2.base.now_ms
    original_mature = guard._original_mature
    try:
        guard.core.v2.base.now_ms = lambda: 1_210_000
        def fake_mature(*_args, **_kwargs):
            return {"liquidation_book_received_ms": 1_212_000}
        guard._original_mature = fake_mature
        guard.mature_session.__globals__["_original_mature"] = fake_mature
        result = guard.mature_session(
            {"origin_received_ms": 1_000_000, "deadline_received_ms": 1_180_000},
            [], "gamma", "clob", 5.0, 0.25,
        )
        assert result is not None
        assert result["liquidation_label_delay_ms"] == 32_000
        assert abs(result["effective_liquidation_horizon_seconds"] - 212.0) < 1e-12
    finally:
        guard.core.v2.base.now_ms = original_now
        guard._original_mature = original_mature
        guard.mature_session.__globals__["_original_mature"] = original_mature


def test_adapter_source_documents_actual_target_and_label_delay():
    text = (ROOT / "scripts/v7_graph_roundtrip_guard.py").read_text(encoding="utf-8")
    assert "actual_target_notional" in text
    assert "quoted_max_notional" in text
    assert "target * (limit + fee)" in text
    assert "MAX_LIQUIDATION_LABEL_DELAY_MS = 45_000" in text
    assert "effective_liquidation_horizon_seconds" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 Graph notional/label-delay tests")
