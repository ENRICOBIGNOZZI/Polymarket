#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_market_common as common


def test_explicit_gamma_fee_disabled_is_authoritative_zero():
    details = common.resolve_fee_details({"feesEnabled": False}, "https://clob.invalid", "c", "t")
    assert details.verified is True
    assert details.rate == 0.0
    assert details.source == "gamma:fees_disabled"


def test_gamma_fee_schedule_is_authoritative():
    details = common.resolve_fee_details({"feeSchedule": {"rate": 0.03, "exponent": 2.0, "takerOnly": True}}, "https://clob.invalid", "c", "t")
    assert details.verified is True
    assert abs(details.rate - 0.03) < 1e-12
    assert details.exponent == 2.0
    assert details.taker_only is True


def test_clob_descriptor_is_allowed_but_fee_rate_endpoint_is_not_pnl_fallback():
    calls = []
    original = common.request_json
    def fake(url, payload=None, timeout=20):
        calls.append(url)
        if "/clob-markets/" in url:
            return {"fd": {"r": 0.02, "e": 1.5, "to": True}}
        raise AssertionError("unexpected endpoint")
    common.request_json = fake
    try:
        details = common.resolve_fee_details({}, "https://clob.example", "cond", "token")
    finally:
        common.request_json = original
    assert details.verified is True
    assert details.source == "clob:fd"
    assert all("/fee-rate" not in url for url in calls)


def test_unknown_schedule_fails_closed_without_007():
    calls = []
    original = common.request_json
    def fake(url, payload=None, timeout=20):
        calls.append(url)
        return {}
    common.request_json = fake
    try:
        details = common.resolve_fee_details({}, "https://clob.example", "cond", "token")
    finally:
        common.request_json = original
    assert details.verified is False
    assert details.rate == 0.0
    assert details.source == "unverified_fee_schedule"
    assert all("/fee-rate" not in url for url in calls)
    source = (ROOT / "scripts" / "v7_market_common.py").read_text(encoding="utf-8")
    assert "legacy_unverified_fallback" not in source
    assert "FeeDetails(0.07" not in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} authoritative fee tests")
