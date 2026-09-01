#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_maker_uses_zero_authority_cpp_components_and_fill_conditioned_research_core():
    runtime_path = ROOT / "src/v7_market_maker_runtime.cpp"
    lane_path = ROOT / "src/v7_maker_lane.cpp"
    paper_path = ROOT / "src/v7_maker_paper.cpp"
    core_path = ROOT / "scripts/v7_market_maker_core.py"
    assert not runtime_path.exists()
    assert lane_path.is_file()
    assert paper_path.is_file()
    assert core_path.is_file()
    assert not (ROOT / "scripts/v7_market_maker_worker.py").exists()
    assert not (ROOT / "scripts/v7_micro_maker_worker.py").exists()
    assert not (ROOT / "scripts/v7_micro_maker_worker_eventtime_core.py").exists()

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
    assert "instrument_inventory_sign" in lane
    assert "allocate_public_print" in paper
    assert "CancelPending" in paper
    assert "PaperMakerEventKind::InventoryMerge" in paper


def test_micro_taker_is_zero_authority_round_trip_research():
    text = (ROOT / "scripts/v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "complete_round_trip_executable_ev" in text
    assert "expected_exit_price" in text
    assert "uncertainty_z" in text
    assert "adverse_markout_bps" in text
    assert "capital_cost_bps_per_hour" in text
    assert "full_visible_depth_entry_and_forecast_shifted_exit_vwap" in text
    assert "v7_shared_market_state" in text
    assert "spool_event" not in text
    assert "LedgerEvent" not in text
    assert "v7_execution_ledger" not in text
    assert "--research-only" not in text
    assert "--max-probe-usd" in text
    assert '"schema": "polymarket_v7_micro_taker_status_v1"' in text
    assert '"model_sha": args.model_sha' in text
    assert '"real_order_submission": False' in text
    loop = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "--max-probe-usd" in loop
    assert '"execution_authority": "RESEARCH_ONLY_ZERO_AUTHORITY"' in text
    for authority in (
        "capital_authority", "oms_authority", "inventory_authority",
        "ledger_writer_authority", "order_authority", "promotion_authority",
    ):
        assert f'"{authority}": False' in text
    assert '"atomic_book_pairs": book_pair_count' in text
    assert '"missing_book_pairs": missing_book_pair_count' in text
    assert '"feature_ready_markets": len(current)' in text


def test_micro_taker_cannot_restore_inventory_or_publish_orders():
    text = (ROOT / "scripts/v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "full_depth_executable_bid_net_fee_or_zero_fail_closed" in text
    assert "model_valid\n        and flow_valid" in text
    assert "and flow_valid" in text
    assert '"DEGENERATE_ZERO_TARGET_VARIANCE"' in text
    assert '"duplicate_snapshots_rejected_last_tick"' in text
    assert "zero_authority_research_refuses_prior_paper_positions" in text
    assert '"inventory_state_created": False' in text
    assert '(args.run_dir.parent / "control" / "CUTOVER_DRAIN").exists()' in text
    assert '"drain_complete": True' in text
    assert "append_fill" not in text


if __name__ == "__main__":
    test_maker_uses_canonical_cpp_runtime_and_fill_conditioned_research_core()
    test_micro_taker_is_zero_authority_round_trip_research()
    test_micro_taker_cannot_restore_inventory_or_publish_orders()
    print("ok 3 v7 micro worker tests")
