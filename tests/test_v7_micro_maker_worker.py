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
    residual_cancel = source.index("# Only residual, still-unfilled orders can now be cancelled")
    ttl_cancel = source.index('"action": "CANCEL_TTL"', residual_cancel)
    assert replay < residual_cancel < ttl_cancel
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


def test_exit_requirement_covers_position_plus_residual_order_and_markout_watch():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = {
            "killed": False,
            "positions": {
                "m1": {"token_id": "x", "shares": 7.0},
            },
            "orders": {
                "m1": {"token_id": "x", "remaining_shares": 3.0},
                "m2": {"token_id": "z", "remaining_shares": 4.0},
            },
            "markout_watch": {
                "w1": {"token_id": "x", "shares": 5.0},
                "w2": {"token_id": "y", "shares": 6.0},
            },
        }
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        required = maker._exit_requirements(root)
        assert required == {"x": 10.0, "y": 6.0}
        state["killed"] = True
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        required = maker._exit_requirements(root)
        assert required == {"x": 10.0, "y": 6.0, "z": 4.0}


def test_late_maker_markouts_fail_closed_in_canonical_adapter():
    fields = [
        "observation_ts", "fill_event_ts", "fill_received_ms", "market_id", "slug", "side",
        "token_id", "horizon_seconds", "observed_age_seconds", "shares", "entry_cost_per_share",
        "exit_bid", "exit_fee_per_share", "slippage_bps", "net_markout_per_share",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "maker_markouts.csv"
        rows = [
            {"market_id": "keep", "horizon_seconds": "45", "observed_age_seconds": "59"},
            {"market_id": "late", "horizon_seconds": "45", "observed_age_seconds": "61"},
            {"market_id": "invalid", "horizon_seconds": "60", "observed_age_seconds": "59"},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        stats = maker.filter_late_markouts(root, max_label_delay_seconds=15)
        assert stats == {"kept": 1, "rejected_late": 1, "rejected_invalid": 1}
        with path.open(newline="", encoding="utf-8") as handle:
            kept = list(csv.DictReader(handle))
        assert [row["market_id"] for row in kept] == ["keep"]
        with (root / "maker_markout_rejections.csv").open(newline="", encoding="utf-8") as handle:
            rejected = list(csv.DictReader(handle))
        assert {row["reject_reason"] for row in rejected} == {"late_markout_label", "invalid_markout_clock"}


def test_maker_adapter_declares_bounded_markout_and_depth_contracts():
    source = (ROOT / "scripts" / "v7_micro_maker_worker.py").read_text(encoding="utf-8")
    assert "MAX_MARKOUT_LABEL_DELAY_SECONDS = 15" in source
    assert "late_markout_label" in source
    assert "event_time_horizon_with_bounded_observation_delay" in source
    assert "shares_specific_full_visible_bid_depth_vwap_fail_closed" in source
    assert "full_depth_sell_vwap" in source


def test_maker_core_persists_trade_identity_and_respects_broker_token_ownership():
    source = maker_core_source()
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
    source = maker_core_source()
    assert "fair_exit_price=future_bid" in source
    assert "slippage_per_share=max(0.0, future_bid - executable_exit)" in source
    assert "fair_exit_price=executable_exit" not in source


def test_hard_arb_dual_clock_freshness_rejects_stale_or_skewed_state():
    live = {
        "a": {"received_ms": 10_000, "exchange_ts_ms": 9_990},
        "b": {"received_ms": 10_040, "exchange_ts_ms": 10_010},
    }
    ok, reason, age, skew = hard.local_book_freshness(
        live, ["a", "b"], now_ms=10_100, max_leg_age_ms=200, max_cross_leg_skew_ms=100
    )
    assert ok and reason == "ok" and age == 100 and skew == 40
    ok, reason, age, skew = hard.exchange_book_freshness(
        live, ["a", "b"], now_ms=10_100, max_snapshot_age_ms=200, max_snapshot_skew_ms=100
    )
    assert ok and reason == "ok" and age == 110 and skew == 20
    ok, reason, _, _ = hard.local_book_freshness(
        live, ["a", "b"], now_ms=10_500, max_leg_age_ms=200, max_cross_leg_skew_ms=100
    )
    assert not ok and reason == "max_leg_age"
    skewed = {
        "a": {"received_ms": 10_000, "exchange_ts_ms": 9_000},
        "b": {"received_ms": 10_040, "exchange_ts_ms": 10_010},
    }
    ok, reason, _, _ = hard.exchange_book_freshness(
        skewed, ["a", "b"], now_ms=10_100, max_snapshot_age_ms=2_000, max_snapshot_skew_ms=500
    )
    assert not ok and reason == "exchange_snapshot_skew"


def test_v7_runtime_uses_strict_hard_arb_guard_with_required_bounds():
    source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "python3 scripts/v7_hard_arb_guard.py" in source
    assert "--max-leg-age-ms 2000" in source
    assert "--max-cross-leg-skew-ms 1000" in source
    assert "--max-exchange-snapshot-age-ms 5000" in source
    assert "--max-exchange-snapshot-skew-ms 1000" in source
    assert "--leg-latency-ms 100" in source
    assert "python3 scripts/v6_hard_arb_paper.py --config \"$RUN_ROOT/hard_arb_config.json\"" not in source
    hard_source = (ROOT / "scripts" / "v7_hard_arb_guard.py").read_text(encoding="utf-8")
    assert '"atomic_snapshot_assumption": False' in hard_source
    assert '"multi_level_depth": True' in hard_source
    assert '"verified_fees_required": True' in hard_source
    assert '"sequential_leg_revalidation": True' in hard_source
    assert '"unwind_on_leg_failure": True' in hard_source


def test_hard_arb_timestamp_normalization_accepts_seconds_and_milliseconds():
    assert hard.normalize_timestamp_ms(1_787_700_000) == 1_787_700_000_000
    assert hard.normalize_timestamp_ms(1_787_700_000_123) == 1_787_700_000_123


def test_v7_hf_research_uses_authorized_bounded_paper_envelope():
    cfg = json.loads((ROOT / "config" / "paper_v7.json").read_text(encoding="utf-8"))
    assert cfg["paper_only"] is True
    assert cfg["market_limit"] == 1000
    assert abs(float(cfg["min_liquidity"]) - 2.0) < 1e-12
    assert abs(float(cfg["min_net_edge"]) - 0.00005) < 1e-12
    assert abs(float(cfg["fractional_kelly"]) - 0.25) < 1e-12
    assert cfg["fixed_dollar_trade_cap_enabled"] is True
    assert abs(float(cfg["max_trade_usd"]) - 125.0) < 1e-12
    assert abs(float(cfg["max_market_fraction"]) - 0.05) < 1e-12
    assert abs(float(cfg["max_event_fraction"]) - 0.15) < 1e-12
    assert abs(float(cfg["max_gross_fraction"]) - 0.70) < 1e-12
    assert abs(float(cfg["multi_strategy"]["global_max_gross_fraction"]) - 0.70) < 1e-12
    assert abs(float(cfg["max_drawdown"]) - 0.15) < 1e-12
    assert cfg["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] is True
    assert abs(float(cfg["v7"]["hard_arb_max_trade_usd"]) - 125.0) < 1e-12
    assert cfg["v7"]["authenticated_execution"] is False


def test_micro_taker_causal_flow_uses_receive_gate_and_event_age():
    fields = ["timestamp", "received_ms", "lag_ms", "condition_id", "asset_id", "outcome", "side", "price", "size", "transaction_hash", "slug", "event_slug"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trade_tape.csv"
        rows = [
            {"timestamp": 990, "received_ms": 995000, "asset_id": "yes", "side": "BUY", "price": 0.50, "size": 10},
            {"timestamp": 995, "received_ms": 1001000, "asset_id": "yes", "side": "SELL", "price": 0.49, "size": 50},
            {"timestamp": 900, "received_ms": 999000, "asset_id": "yes", "side": "SELL", "price": 0.49, "size": 100},
            {"timestamp": 999, "received_ms": 999500, "asset_id": "no", "side": "SELL", "price": 0.50, "size": 5},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        flow = taker.causal_flow_features(path, {"yes", "no"}, now=1000, lookback_seconds=60, half_life_seconds=15)
        assert flow["yes"]["prints"] == 1.0
        assert abs(flow["yes"]["signed_imbalance"] - 1.0) < 1e-12
        assert flow["no"]["prints"] == 1.0
        assert abs(flow["no"]["signed_imbalance"] + 1.0) < 1e-12


def test_micro_taker_full_depth_vwap_is_quantity_specific_and_fails_closed():
    asks = [(0.40, 5.0), (0.41, 10.0)]
    bids = [(0.39, 5.0), (0.38, 10.0)]
    assert abs(taker.full_depth_vwap(asks, 10.0, buy=True) - 0.405) < 1e-12
    assert abs(taker.full_depth_vwap(bids, 10.0, buy=False) - 0.385) < 1e-12
    assert taker.full_depth_vwap(asks, 20.0, buy=True) is None
    assert taker.full_depth_vwap(bids, 20.0, buy=False) is None


def test_micro_taker_model_never_mixes_legacy_six_feature_rows_with_flow_schema():
    legacy = [{"x": [1.0] * 6, "y": 0.01} for _ in range(100)]
    beta = taker.solve_ridge(legacy, 1e-2, taker.FLOW_FEATURE_DIM)
    assert beta == [0.0] * taker.FLOW_FEATURE_DIM


def test_micro_taker_declares_causal_flow_and_full_depth_execution_contracts():
    source = (ROOT / "scripts" / "v7_micro_taker_worker.py").read_text(encoding="utf-8")
    assert "causal_flow_depth_complete_round_trip_ev" in source
    assert "receive_causal_event_decayed_yes_no_taker_flow" in source
    assert "full_visible_depth_entry_and_forecast_shifted_exit_vwap" in source
    assert "shares_specific_full_visible_bid_depth_vwap_fail_closed" in source
    assert "received_ms > now_ms" in source
    assert "event_ts > now" in source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 HF execution tests")
