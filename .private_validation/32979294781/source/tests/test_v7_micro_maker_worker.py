#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_micro_maker_worker as maker


def test_causal_fill_requires_both_receive_and_event_clocks():
    order = {"created_event_ms": 1000, "created_received_ms": 1100}
    assert maker.causal_after_order({"timestamp": "2", "received_ms": "2000"}, order)
    assert not maker.causal_after_order({"timestamp": "0", "received_ms": "2000"}, order)
    assert not maker.causal_after_order({"timestamp": "2", "received_ms": "1000"}, order)


def test_maker_source_persists_trade_identity_and_respects_broker_token_ownership():
    source = (ROOT / "scripts" / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "seen_trade_ids" in source
    assert "if identity in seen_trade_ids" in source
    assert "broker_owned_tokens" in source
    assert "CANCEL_TOKEN_OWNED_BY_MULTILEG" in source
    assert "fill_conditioned_net_pnl" in source
    assert "toxicity" in source


def test_runtime_runs_maker_under_shared_capacity_lock():
    source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "v7_capacity_lock.py" in source
    assert "v7_micro_maker_worker.py" in source
    assert "token_capacity.lock" in source
    assert "v6_micro_maker_v2.py" not in source


def test_maker_state_counts_exit_slippage_once():
    source = (ROOT / "scripts" / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "fair_exit_price=future_bid" in source
    assert "slippage_per_share=max(0.0, future_bid - executable_exit)" in source
    assert "fair_exit_price=executable_exit" not in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 micro maker worker tests")
