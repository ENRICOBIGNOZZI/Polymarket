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


def test_causal_fill_requires_both_receive_and_event_clocks():
    order = {"created_event_ms": 1000, "created_received_ms": 1100}
    assert maker.causal_after_order({"timestamp": "2", "received_ms": "2000"}, order)
    assert not maker.causal_after_order({"timestamp": "0", "received_ms": "2000"}, order)
    assert not maker.causal_after_order({"timestamp": "2", "received_ms": "1000"}, order)


def test_delayed_receipt_can_credit_pre_expiry_market_event():
    order = {"created_event_ms": 10_000, "created_received_ms": 10_000}
    row = {"timestamp": "10.050", "received_ms": "75000"}
    assert maker.causal_fill_eligible(row, order, processing_ms=80_000, ttl_seconds=60)


def test_post_expiry_market_event_never_fills_even_if_received_now():
    order = {"created_event_ms": 10_000, "created_received_ms": 10_000}
    row = {"timestamp": "70.001", "received_ms": "71000"}
    assert not maker.causal_fill_eligible(row, order, processing_ms=80_000, ttl_seconds=60)


def test_future_received_trade_never_fills_or_leaks_into_current_tick():
    order = {"created_event_ms": 10_000, "created_received_ms": 10_000}
    row = {"timestamp": "20", "received_ms": "81000"}
    assert not maker.causal_fill_eligible(row, order, processing_ms=80_000, ttl_seconds=60)


def test_arrival_latency_applies_to_both_order_clocks():
    order = {"created_event_ms": 10_100, "created_received_ms": 10_100}
    before_arrival = {"timestamp": "10.050", "received_ms": "10500"}
    after_arrival = {"timestamp": "10.150", "received_ms": "10600"}
    assert not maker.causal_fill_eligible(before_arrival, order, processing_ms=20_000, ttl_seconds=60)
    assert maker.causal_fill_eligible(after_arrival, order, processing_ms=20_000, ttl_seconds=60)


def test_maker_core_replays_before_residual_ttl_cancel_and_tracks_markouts():
    source = maker_core_source()
    replay = source.index("for row in tape:")
    residual_cancel = source.index("for market_id, order in list(orders.items()):", replay)
    ttl_cancel = source.index('"action": "CANCEL_TTL"', residual_cancel)
    assert replay < residual_cancel < ttl_cancel
    assert "causal_fill_eligible(row, order" in source[replay:residual_cancel]
    assert "maker_markouts.csv" in source
    assert "for horizon in (45, 60, 300):" in source
    assert '"entry_ts": fill_event_ts' in source
    assert '"created_event_ms": arrival_ms' in source
    assert '"created_received_ms": arrival_ms' in source


def test_full_depth_sell_vwap_walks_levels_and_fails_closed():
    levels = [(0.40, 5.0), (0.39, 10.0), (0.38, 20.0)]
    assert abs(maker.full_depth_sell_vwap(levels, 10.0) - 0.395) < 1e-12
    expected = (5.0 * 0.40 + 10.0 * 0.39 + 5.0 * 0.38) / 20.0
    assert abs(maker.full_depth_sell_vwap(levels, 20.0) - expected) < 1e-12
    assert maker.full_depth_sell_vwap(levels, 40.0) is None


def test_depth_aware_bid_is_quantity_specific_and_invalid_when_depth_insufficient():
    book = maker.core.base.Book({
        "asset_id": "token",
        "tick_size": 0.01,
        "min_order_size": 1,
        "bids": [
            {"price": "0.40", "size": "5"},
            {"price": "0.39", "size": "10"},
        ],
        "asks": [{"price": "0.41", "size": "10"}],
    })
    try:
        maker._DEPTH_EXIT_SHARES["token"] = 10.0
        assert abs(maker._depth_aware_bid(book) - 0.395) < 1e-12
        maker._DEPTH_EXIT_SHARES["token"] = 20.0
        assert math.isnan(maker._depth_aware_bid(book))
    finally:
        maker._DEPTH_EXIT_SHARES.clear()


def test_exit_requirement_covers_position_plus_residual_order_and_markout_watch_without_double_counting_fill():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = {
            "killed": False,
            "positions": {"m1": {"token_id": "x", "shares": 7.0}},
            "orders": {
                "m1": {"token_id": "x", "remaining_shares": 3.0},
                "m2": {"token_id": "z", "remaining_shares": 4.0},
            },
            "markout_watch": {
                "w1": {"token_id": "x", "shares": 5.0},
            },
        }
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        required = maker._exit_requirements(root)
        # Cover the live position plus its residual order, or the already-filled
        # markout watch if larger. A never-filled resting order has no liquidation
        # requirement while the system is not killed.
        assert required["x"] == 10.0
        assert "z" not in required


def test_kill_switch_closes_positions_before_resting_orders():
    source = maker_core_source()
    kill_pos = source.index("if killed:")
    close_pos = source.index('"reason": "drawdown_kill"', kill_pos)
    cancel_pos = source.index('"action": "CANCEL_KILL"', kill_pos)
    assert close_pos < cancel_pos


def test_hard_arb_guard_rejects_stale_book():
    live = {"token": {"received_ms": 95_000}}
    ok, reason, age, _ = hard.local_book_freshness(
        live,
        ["token"],
        now_ms=100_000,
        max_leg_age_ms=10_000,
        max_cross_leg_skew_ms=1_000,
    )
    assert ok and reason == "ok" and age == 5_000
    ok, reason, age, _ = hard.local_book_freshness(
        live,
        ["token"],
        now_ms=120_000,
        max_leg_age_ms=10_000,
        max_cross_leg_skew_ms=1_000,
    )
    assert not ok and reason == "max_leg_age" and age == 25_000


def test_taker_worker_uses_depth_and_round_trip_economics():
    source = (ROOT / "scripts" / "v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "depth_adjusted_economics" in source
    assert "full_visible_depth_entry_and_forecast_shifted_exit_vwap" in source
    assert "causal_flow_depth_complete_round_trip_ev" in source


def test_append_csv_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.csv"
        maker.core.base.append_csv(path, ["a", "b"], {"a": 1, "b": 2})
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert rows == [{"a": "1", "b": "2"}]


def test_state_json_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        payload = {"cash": 1000.0, "killed": False}
        maker.core.base.atomic_json(path, payload)
        assert json.loads(path.read_text(encoding="utf-8")) == payload


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 maker/taker/hard-arb tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
