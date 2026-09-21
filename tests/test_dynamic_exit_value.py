import math

from research.walk_forward_v3.direct_action import (
    DEFAULT_ACTION_HORIZONS_MS,
    DEFAULT_TRAIN_LATENCIES_MS,
    DirectActionValueModel,
)
from research.walk_forward_v3.dynamic_exit import (
    DynamicExitValueModel,
    hold_incremental_value,
)


def row(index, *, future_bid=.55):
    decision_ns = 1_790_000_000_000_000_000 + index * 1_000_000_000
    return {
        "decision_id": f"d-{index}",
        "market_id": f"m-{index}",
        "token_id": f"yes-{index}",
        "yes_token_id": f"yes-{index}",
        "no_token_id": f"no-{index}",
        "asset": "BTC",
        "horizon": "M5",
        "decision_ns": decision_ns,
        "signal_age_ns": 1_000_000,
        "tte_ns": 110_000_000_000,
        "direction": 1,
        "signal_valid": True,
        "confirmed": True,
        "book_valid": True,
        "pretrigger": True,
        "bid": .49,
        "ask": .50,
        "bid_quantity": 20.0,
        "quantity": 20.0,
        "minimum": 1.0,
        "tick": .01,
        "fee_rate": 0.0,
        "fee_exponent": 1.0,
        "features": {
            "external.binance_return_100ms_bp": 2.0,
            "external.coinbase_return_100ms_bp": 1.5,
            "external.bybit_return_100ms_bp": 1.0,
            "external.return_250ms": .0004,
            "external.return_1s": .0010,
            "external.return_5s": .0020,
            "external.native_vol_fast": .002,
            "external.native_vol_slow": .001,
            "external.dispersion_bps": .5,
            "external.fresh_venues": 3.0,
            "signal_age_ns": 1_000_000.0,
            "tte_ns": 110_000_000_000.0,
        },
        "arrivals": {
            "50": {
                "time_ns": decision_ns + 50_000_000,
                "bid": .49, "ask": .50,
                "bid_quantity": 20.0, "quantity": 20.0, "epoch": 1,
            }
        },
        "targets": {
            "500": {
                "state": "OBSERVED",
                "arrival_bid": future_bid,
                "arrival_ask": future_bid + .01,
                "arrival_bid_quantity": 20.0,
                "arrival_quantity": 20.0,
                "observed_time_ns": decision_ns + 500_000_000,
            }
        },
    }


def test_direct_action_defaults_cover_5ms_10ms_and_10s():
    assert DEFAULT_TRAIN_LATENCIES_MS == (5, 10, 25, 50, 100, 250)
    assert 750 in DEFAULT_ACTION_HORIZONS_MS
    assert 3000 in DEFAULT_ACTION_HORIZONS_MS
    assert 5000 in DEFAULT_ACTION_HORIZONS_MS
    assert 7500 in DEFAULT_ACTION_HORIZONS_MS
    assert 10000 in DEFAULT_ACTION_HORIZONS_MS


def test_continuation_reversal_features_are_explicit():
    r = row(1)
    model = DirectActionValueModel()
    model._configure_levels([r])
    action = model._action_record(
        r, size=5.0, horizon_ms=10000, latency_ms=5, side="YES")
    f = action["features"]
    assert f["action.continuation_100ms"] > 0
    assert f["action.reversal_100ms"] < 0
    assert f["action.continuation_250ms"] > 0
    assert f["action.reversal_250ms"] < 0
    assert f["state.cross_venue_agreement"] == 1.0
    assert f["state.native_vol_ratio"] > 1.0
    assert action["latency_ms"] == 5
    assert action["exit_horizon_ms"] == 10000


def test_unobserved_timing_cell_is_not_selectable():
    rows = [row(i + 10, future_bid=.56 if i % 2 == 0 else .44)
            for i in range(40)]
    model = DirectActionValueModel(
        size_grid=(1.0, 5.0),
        action_horizons_ms=(500, 1000),
        train_latencies_ms=(50,),
        max_sizes_per_state=2,
    ).fit(rows)
    assert model.action_cell_target_counts.get("50::500::YES", 0) > 0
    assert model.action_cell_target_counts.get("50::1000::YES", 0) == 0
    scored, state = model.score_actions(row(999, future_bid=.56), latency_ms=50)
    assert state in ("READY", "NO_FEASIBLE_ACTION")
    assert all(item["exit_horizon_ms"] == 500 for item in scored)


def test_dynamic_exit_incremental_value_and_hold_decision():
    r = row(100, future_bid=.55)
    value, state = hold_incremental_value(
        r, side="YES", horizon_ms=500, size=5.0)
    assert state == "OBSERVED"
    assert math.isclose(value, 5.0 * (.55 - .49), abs_tol=1e-12)

    rows = [row(i + 200, future_bid=.55) for i in range(30)]
    model = DynamicExitValueModel(horizons_ms=(500,)).fit(rows)
    decision = model.decide(
        row(9999, future_bid=.55), side="YES", position_size=5.0)
    assert decision["action"] == "HOLD"
    assert decision["additional_horizon_ms"] == 500
    assert decision["lower_incremental_value"] > 0
    assert decision["re_evaluate_on_next_causal_book_update"] is True
