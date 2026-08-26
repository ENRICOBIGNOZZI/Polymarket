#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_hard_arb_guard as hard
import v7_micro_maker_worker as maker
import v7_micro_taker_worker as taker


def maker_core_source() -> str:
    return (ROOT / "scripts" / "v7_micro_maker_worker_eventtime_core.py").read_text(encoding="utf-8")


def test_causal_fill_uses_receive_and_event_clocks_and_ttl_event_time():
    order = {"created_event_ms": 10_000, "created_received_ms": 10_000}
    assert maker.causal_fill_eligible({"timestamp": "10.050", "received_ms": "75000"}, order, processing_ms=80_000, ttl_seconds=60)
    assert not maker.causal_fill_eligible({"timestamp": "70.001", "received_ms": "71000"}, order, processing_ms=80_000, ttl_seconds=60)
    assert not maker.causal_fill_eligible({"timestamp": "20", "received_ms": "81000"}, order, processing_ms=80_000, ttl_seconds=60)


def test_maker_core_replays_before_ttl_cancel_and_tracks_markouts():
    source = maker_core_source()
    replay = source.index("for row in tape:")
    ttl_cancel = source.index('"action": "CANCEL_TTL"', replay)
    assert replay < ttl_cancel
    assert "maker_markouts.csv" in source
    assert "for horizon in (45, 60, 300):" in source
    assert '"entry_ts": fill_event_ts' in source
    assert '"created_event_ms": arrival_ms' in source
    assert '"created_received_ms": arrival_ms' in source
    assert "v6_" not in source


def test_full_depth_sell_vwap_walks_levels_and_fails_closed():
    levels = [(0.40, 5.0), (0.39, 10.0), (0.38, 20.0)]
    assert abs(maker.full_depth_sell_vwap(levels, 10.0) - 0.395) < 1e-12
    expected = (5.0 * 0.40 + 10.0 * 0.39 + 5.0 * 0.38) / 20.0
    assert abs(maker.full_depth_sell_vwap(levels, 20.0) - expected) < 1e-12
    assert maker.full_depth_sell_vwap(levels, 40.0) is None


def test_depth_aware_bid_is_quantity_specific_and_fails_on_insufficient_depth():
    book = maker.core.base.Book({
        "asset_id": "token",
        "tick_size": 0.01,
        "min_order_size": 1,
        "bids": [{"price": "0.40", "size": "5"}, {"price": "0.39", "size": "10"}],
        "asks": [{"price": "0.41", "size": "10"}],
    })
    try:
        maker._DEPTH_EXIT_SHARES["token"] = 10.0
        assert abs(maker._depth_aware_bid(book) - 0.395) < 1e-12
        maker._DEPTH_EXIT_SHARES["token"] = 20.0
        assert math.isnan(maker._depth_aware_bid(book))
    finally:
        maker._DEPTH_EXIT_SHARES.clear()


def test_exit_requirement_covers_positions_residual_orders_and_markout_watch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = {
            "killed": False,
            "positions": {"m1": {"token_id": "x", "shares": 7.0}},
            "orders": {"m1": {"token_id": "x", "remaining_shares": 3.0}, "m2": {"token_id": "z", "remaining_shares": 4.0}},
            "markout_watch": {"w1": {"token_id": "x", "shares": 5.0}, "w2": {"token_id": "y", "shares": 6.0}},
        }
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        assert maker._exit_requirements(root) == {"x": 10.0, "y": 6.0}
        state["killed"] = True
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        assert maker._exit_requirements(root) == {"x": 10.0, "y": 6.0, "z": 4.0}


def test_late_maker_markouts_fail_closed():
    fields = [
        "observation_ts", "fill_event_ts", "fill_received_ms", "market_id", "slug", "side",
        "token_id", "horizon_seconds", "observed_age_seconds", "shares", "entry_cost_per_share",
        "exit_bid", "exit_fee_per_share", "slippage_bps", "net_markout_per_share",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); path = root / "maker_markouts.csv"
        rows = [
            {"market_id": "keep", "horizon_seconds": "45", "observed_age_seconds": "59"},
            {"market_id": "late", "horizon_seconds": "45", "observed_age_seconds": "61"},
            {"market_id": "invalid", "horizon_seconds": "60", "observed_age_seconds": "59"},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        stats = maker.filter_late_markouts(root, max_label_delay_seconds=15)
        assert stats == {"kept": 1, "rejected_late": 1, "rejected_invalid": 1}


def _maker_state(*, limit: float, queue: float) -> maker.MakerState:
    return maker.MakerState(
        side="BUY",
        limit_price=limit,
        fair_exit_price=0.50004,
        queue_ahead=queue,
        own_size=100.0,
        compatible_flow=100.0,
        flow_horizon_seconds=60.0,
        ofi=0.0,
        imbalance=0.0,
        microprice=0.5,
        midpoint=0.5,
        displayed_depth=1_000_000.0,
        entry_fee_per_share=0.0,
        exit_fee_per_share=0.0,
        slippage_per_share=0.0,
        adverse_markout_per_share=0.0,
        partial_unwind_loss_per_share=0.0,
        expected_partial_fraction=0.0,
        capital_usd=0.0,
        capital_time_rate_per_second=0.0,
        expected_rest_seconds=60.0,
        latency_seconds=0.1,
    )


def test_inside_spread_falls_back_to_touch_when_residual_edge_fails_configured_floor():
    touch = _maker_state(limit=0.49, queue=1_000_000.0)
    improved = _maker_state(limit=0.50, queue=0.0)
    touch_decision = maker.maker_fill_conditioned_ev(touch)
    improved_decision = maker.maker_fill_conditioned_ev(improved)
    assert touch_decision.conditional_net_pnl_per_share > 0.00005
    assert improved_decision.conditional_net_pnl_per_share < 0.00005
    assert maker.quote_improvement_is_economic(touch, improved)
    assert not maker.selective_improvement_is_economic(
        touch,
        improved,
        min_edge=0.00005,
        min_fill_probability=0.001,
        base_check=maker.quote_improvement_is_economic,
    )


def test_inside_spread_guard_preserves_configured_fill_floor():
    touch = _maker_state(limit=0.49, queue=1_000_000.0)
    improved = _maker_state(limit=0.50, queue=0.0)
    improved_decision = maker.maker_fill_conditioned_ev(improved)
    assert improved_decision.fill_probability > 0.001
    assert not maker.selective_improvement_is_economic(
        touch,
        improved,
        min_edge=0.0,
        min_fill_probability=min(1.0, improved_decision.fill_probability + 0.01),
        base_check=maker.quote_improvement_is_economic,
    )


def test_maker_adapter_declares_depth_and_markout_contracts():
    source = (ROOT / "scripts" / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "MAX_MARKOUT_LABEL_DELAY_SECONDS = 15" in source
    assert "late_markout_label" in source
    assert "event_time_horizon_with_bounded_observation_delay" in source
    assert "shares_specific_full_visible_bid_depth_vwap_fail_closed" in source
    assert "full_depth_sell_vwap" in source
    assert "inside_spread_must_pass_configured_edge_fill_and_ev_floors_or_fall_back_to_touch" in source
    assert "v6_" not in source


def test_runtime_uses_v7_maker_capacity_lock_and_strict_hard_arb():
    source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    for marker in (
        "v7_capacity_lock.py", "v7_micro_maker_worker.py", "token_capacity.lock",
        "v7_hard_arb_guard.py", "--max-leg-age-ms 2000", "--max-cross-leg-skew-ms 1000",
        "--max-exchange-snapshot-age-ms 5000", "--max-exchange-snapshot-skew-ms 1000", "--leg-latency-ms 100",
    ):
        assert marker in source
    assert "v6_" not in source
    hard_source = (ROOT / "scripts" / "v7_hard_arb_guard.py").read_text(encoding="utf-8")
    for marker in ('"atomic_snapshot_assumption": False', '"multi_level_depth": True', '"verified_fees_required": True', '"sequential_leg_revalidation": True', '"unwind_on_leg_failure": True'):
        assert marker in hard_source


def test_micro_taker_flow_depth_and_complete_roundtrip_contract():
    source = (ROOT / "scripts" / "v7_micro_taker_worker.py").read_text(encoding="utf-8")
    for marker in (
        "causal_flow_depth_complete_round_trip_ev", "complete_round_trip_executable_ev",
        "received_ms > now_ms", "event_ts > now", "full_depth_vwap",
        "entry_fee_per_share", "exit_fee_per_share", "uncertainty_penalty_per_share",
        "adverse_markout_penalty_per_share", "capital_time_cost_per_share",
    ):
        assert marker in source
    assert "v6_" not in source


def test_hard_arb_timestamp_normalization_accepts_seconds_and_milliseconds():
    assert hard.normalize_timestamp_ms(1_787_700_000) == 1_787_700_000_000
    assert hard.normalize_timestamp_ms(1_787_700_000_123) == 1_787_700_000_123


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} V7 HF execution tests")
