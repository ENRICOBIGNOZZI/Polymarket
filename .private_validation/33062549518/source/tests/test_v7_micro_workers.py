#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_maker_uses_fill_conditioned_toxicity_objective_and_dual_clock():
    adapter = (ROOT / "scripts/v7_micro_maker_worker.py").read_text(encoding="utf-8")
    core = (ROOT / "scripts/v7_micro_maker_worker_eventtime_core.py").read_text(encoding="utf-8")
    assert "maker_fill_conditioned_ev" in core
    assert "toxicity_score" in core
    assert "created_received_ms" in core
    assert "created_event_ms" in core
    assert "received_ms" in core
    assert "broker_owned_tokens" in core
    assert "full_depth_sell_vwap" in adapter
    assert "shares_specific_full_visible_bid_depth_vwap_fail_closed" in adapter
    assert "MAX_MARKOUT_LABEL_DELAY_SECONDS = 15" in adapter


def test_taker_uses_complete_round_trip_contract():
    text = (ROOT / "scripts/v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "complete_round_trip_executable_ev" in text
    assert "expected_exit_price" in text
    assert "uncertainty_z" in text
    assert "adverse_markout_bps" in text
    assert "capital_cost_bps_per_hour" in text
    assert "full_visible_depth_entry_and_forecast_shifted_exit_vwap" in text


def test_taker_freezes_new_risk_when_open_positions_are_unmarkable():
    text = (ROOT / "scripts/v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "full_depth_executable_bid_net_fee_or_zero_fail_closed" in text
    assert '"reason": "missing_current_snapshot"' in text
    assert '"reason": "insufficient_exit_depth"' in text
    assert '"reason": "missing_authoritative_fee"' in text
    assert "new_risk_frozen = bool(unmarkable_positions)" in text
    assert "if not killed and not new_risk_frozen and model_labeled >= 40:" in text
    assert '"new_risk_frozen": new_risk_frozen' in text
    assert '"unmarkable_positions": unmarkable_positions' in text
    assert 'value += float(position["shares"]) * float(position["entry_price"])' not in text


if __name__ == "__main__":
    test_maker_uses_fill_conditioned_toxicity_objective_and_dual_clock()
    test_taker_uses_complete_round_trip_contract()
    test_taker_freezes_new_risk_when_open_positions_are_unmarkable()
    print("ok 3 v7 micro worker tests")
