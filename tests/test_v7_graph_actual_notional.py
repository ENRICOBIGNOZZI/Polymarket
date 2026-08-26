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
        # The wrapper function's globals live in the adapter module.
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


def test_adapter_source_documents_actual_target_normalization():
    text = (ROOT / "scripts/v7_graph_roundtrip_guard.py").read_text(encoding="utf-8")
    assert "actual_target_notional" in text
    assert "quoted_max_notional" in text
    assert "target * (limit + fee)" in text


if __name__ == "__main__":
    test_attach_replaces_budget_ceiling_with_actual_target_cash()
    test_adapter_source_documents_actual_target_normalization()
    print("ok 2 V7 Graph actual-notional tests")
