import math

from research.walk_forward_v3.latency_frontier import latency_age_frontier


def row(index, *, signal_age_ms=10.0, exit_bid=.56):
    decision_ns = 1_790_000_000_000_000_000 + index * 3_000_000_000
    return {
        "decision_id": f"d-{index}",
        "market_id": f"m-{index}",
        "token_id": f"yes-{index}",
        "asset": "BTC",
        "horizon": "M5",
        "decision_ns": decision_ns,
        "information_end_ns": decision_ns,
        "signal_age_ns": int(signal_age_ms * 1e6),
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
        "paper_venue_delay_ns": 25_000_000,
        "paper_assumed_transport_delay_ns": 50_000_000,
        "features": {
            "external.binance_return_100ms_bp": 2.0,
            "signal_age_ns": float(signal_age_ms * 1e6),
            "tte_ns": 110_000_000_000.0,
        },
        "arrivals": {
            "50": {
                "time_ns": decision_ns + 50_000_000,
                "bid": .49,
                "ask": .50,
                "bid_quantity": 20.0,
                "quantity": 20.0,
                "epoch": 7,
            }
        },
        "targets": {
            "500": {
                "state": "OBSERVED",
                "arrival_bid": exit_bid,
                "arrival_ask": exit_bid + .01,
                "arrival_bid_quantity": 20.0,
                "arrival_quantity": 20.0,
                "observed_time_ns": decision_ns + 500_000_000,
            }
        },
        "label": None,
        "label_information_ns": None,
    }


def test_latency_age_frontier_is_oos_and_age_gate_only_removes_actions():
    rows = [
        row(index, signal_age_ms=10.0 if index % 2 == 0 else 90.0,
            exit_bid=.56 if index % 3 else .44)
        for index in range(36)
    ]
    result = latency_age_frontier(
        rows,
        desired_folds=2,
        latencies_ms=(50,),
        effective_age_gates_ms=(None, 100.0),
        capital_budget=1000.0,
        model_kwargs={
            "size_grid": (1.0, 5.0),
            "action_horizons_ms": (500,),
            "streaming_batch_size": 32,
            "selection_calibration_mode": "OFF",
        },
    )
    assert result["state"] == "READY"
    assert result["selection"] == "NONE_RESEARCH_FRONTIER_ONLY"
    assert result["folds"]

    for fold in result["folds"]:
        cells = {
            cell["maximum_effective_signal_age_ms"]: cell
            for cell in fold["cells"]
        }
        unrestricted = cells[None]
        gated = cells[100.0]
        assert (
            gated["economics"]["selected_trades"]
            <= unrestricted["economics"]["selected_trades"]
        )
        support = unrestricted["operational_support"]
        assert support["operational_floor_known_trades"] >= 0
        if support["operational_floor_known_trades"]:
            # research=50ms, configured floor=75ms
            assert math.isclose(
                support["mean_research_minus_operational_floor_ms"],
                -25.0,
                abs_tol=1e-12,
            )
            assert support["operationally_supported_trades"] == 0


def test_latency_frontier_rejects_nonpositive_age_gate():
    rows = [row(index) for index in range(12)]
    try:
        latency_age_frontier(
            rows,
            desired_folds=2,
            latencies_ms=(50,),
            effective_age_gates_ms=(0.0,),
            model_kwargs={
                "action_horizons_ms": (500,),
                "selection_calibration_mode": "OFF",
            },
        )
    except ValueError as exc:
        assert "positive effective-age gate required" in str(exc)
    else:
        raise AssertionError("expected invalid age gate to fail")
