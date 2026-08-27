#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_maker_uses_canonical_cpp_runtime_and_fill_conditioned_research_core():
    runtime_path = ROOT / "src/v7_market_maker_runtime.cpp"
    lane_path = ROOT / "src/v7_maker_lane.cpp"
    paper_path = ROOT / "src/v7_maker_paper.cpp"
    core_path = ROOT / "scripts/v7_market_maker_core.py"
    assert runtime_path.is_file()
    assert lane_path.is_file()
    assert paper_path.is_file()
    assert core_path.is_file()
    assert not (ROOT / "scripts/v7_micro_maker_worker.py").exists()
    assert not (ROOT / "scripts/v7_micro_maker_worker_eventtime_core.py").exists()

    runtime = runtime_path.read_text(encoding="utf-8")
    lane = lane_path.read_text(encoding="utf-8")
    paper = paper_path.read_text(encoding="utf-8")
    core = core_path.read_text(encoding="utf-8")
    assert "fill-conditioned trading economics" in core
    assert "toxicity_score" in core
    assert "exchange_ts_ms" in core
    assert "receive_ts_ms" in core
    assert "complete_sets" in core
    assert "expected_total_pnl" in core
    assert "subsidy_dependent" in core
    assert "MarketWebSocketFeed" in runtime
    assert "MarketWsShard" in runtime
    assert "MakerInstrumentLane" in runtime
    assert "MakerPaperMarketEngine" in runtime
    assert "v7_ledger_spool" in runtime
    assert "operational_fill_scenario" in runtime
    assert "pessimistic" in runtime
    assert "LineageInvalidated" in runtime
    assert "instrument_inventory_sign" in lane
    assert "allocate_public_print" in paper
    assert "CancelPending" in paper
    assert "FinalMerge" in paper


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
    test_maker_uses_canonical_cpp_runtime_and_fill_conditioned_research_core()
    test_taker_uses_complete_round_trip_contract()
    test_taker_freezes_new_risk_when_open_positions_are_unmarkable()
    print("ok 3 v7 micro worker tests")
