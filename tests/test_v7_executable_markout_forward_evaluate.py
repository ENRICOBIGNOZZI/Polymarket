import json
from pathlib import Path

import pytest

from scripts.v7_executable_markout_forward_evaluate import (
    evaluate,
    label_point,
)
from scripts.v7_executable_markout_forward_shadow import decision


def origin_row(depth=6_000_000):
    decision_wall = 1_800_000_000_000_000_000
    return {
        "schema": "polymarket_v7_native_observation_v1",
        "kind": 2,
        "paper_only": True,
        "execution_authority": False,
        "signal_valid": True,
        "confirmed_non_opposing": True,
        "book_valid": True,
        "server_id": "s", "run_id": "r", "capture_id": "c",
        "market_id": "m", "token_id": "t", "asset": "BTC",
        "horizon": "M5", "signal_version": 7,
        "decision_monotonic_ns": 2_000_000_000,
        "decision_wall_ns": decision_wall,
        "trigger_monotonic_ns": 1_999_000_000,
        "receive_monotonic_ns": 1_999_500_000,
        "close_monotonic_ns": 112_000_000_000,
        "close_wall_ns": decision_wall + 110_000_000_000,
        "bid_e4": 4900, "ask_e4": 5000, "tick_e4": 100,
        "fee_rate": 0.0, "fee_exponent": 1.0,
        "minimum_order_microunits": 1_000_000,
        "ask_quantity": depth,
        "paper_venue_delay_ns": 250_000_000,
        "paper_assumed_transport_delay_ns": 250_000_000,
        "signal_return_bp": 2.0,
        "signal_age_ns": 1_000_000,
        "tte_ns": 110_000_000_000,
        "external_features": {"input_receive_ns": 1_999_000_000},
    }


def kind6(horizon, bid_e4, ask_e4, quantity=6_000_000):
    origin = origin_row()
    decision_mono = origin["decision_monotonic_ns"]
    observed = decision_mono + horizon * 1_000_000 + 1
    return {
        "schema": "polymarket_v7_native_observation_v1",
        "kind": 6,
        "paper_only": True,
        "execution_authority": False,
        "server_id": "s", "run_id": "r", "capture_id": "c",
        "market_id": "m", "token_id": "t",
        "repricing_origin_signal_version": 7,
        "repricing_horizon_ms": horizon,
        "decision_monotonic_ns": decision_mono,
        "decision_wall_ns": origin["decision_wall_ns"],
        "observed_monotonic_ns": observed,
        "close_monotonic_ns": origin["close_monotonic_ns"],
        "close_wall_ns": origin["close_wall_ns"],
        "connection_epoch": 7,
        "repricing_pair_valid": True,
        "book_valid": True,
        "bid_e4": bid_e4, "ask_e4": ask_e4, "tick_e4": 100,
        "ask_quantity": quantity,
    }


def prediction(scored_offset_ms=10):
    origin = origin_row()
    item = decision(origin)
    assert item is not None
    return {
        "schema": "polymarket_v7_executable_markout_forward_shadow_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "automatic_promotion": False,
        "forward_eligible": True,
        "server_id": "s", "run_id": "r", "capture_id": "c",
        "market_id": "m", "token_id": "t", "asset": "BTC",
        "contract_horizon": "M5", "signal_version": 7,
        "decision_id": item["decision_id"],
        "decision_monotonic_ns": item["decision_monotonic_ns"],
        "decision_wall_ns": item["decision_wall_ns"],
        "scored_wall_ns": item["decision_wall_ns"] + scored_offset_ms * 1_000_000,
        "inference_age_ns": scored_offset_ms * 1_000_000,
        "predictions": {"500": .02, "1000": .02, "2000": .02},
    }


def key():
    return ("s", "r", "c", "m", "t", 7, 2_000_000_000)


def test_forward_evaluator_includes_venue_and_transport_delay_before_arrival():
    pred = prediction(10)
    origin = origin_row()
    p500 = label_point(kind6(500, 4900, 5000))
    p750 = label_point(kind6(750, 5000, 5100))
    p1000 = label_point(kind6(1000, 5500, 5600))
    assert p500 and p750 and p1000
    labels = {p500["key"]: p500, p750["key"]: p750, p1000["key"]: p1000}
    result = evaluate({key(): pred}, {key(): origin}, labels)

    short = result["cells"]["BTC:500"]
    assert short["prediction_threshold"] == 1
    assert short["live_geometry"] == 1
    assert short["arrival_after_target"] == 1
    assert short["simulated_fills"] == 0

    cell = result["cells"]["BTC:1000"]
    assert cell["arrival_available"] == 1
    assert cell["simulated_fills"] == 1
    assert cell["marked_fills"] == 1
    assert cell["modeled_arrival_delay_ms_quantiles"]["0.5"] == 510.0
    assert cell["positive_markout_fills"] == 1


def test_forward_evaluator_blocks_sub_five_share_decision_depth():
    pred = prediction(10)
    origin = origin_row(depth=4_000_000)
    p750 = label_point(kind6(750, 5000, 5100))
    p1000 = label_point(kind6(1000, 5500, 5600))
    labels = {p750["key"]: p750, p1000["key"]: p1000}
    result = evaluate({key(): pred}, {key(): origin}, labels)
    cell = result["cells"]["BTC:1000"]
    assert cell["prediction_threshold"] == 1
    assert cell["live_geometry"] == 0
    assert cell["simulated_fills"] == 0


def test_forward_evaluator_rejects_prediction_after_label_availability():
    pred = prediction(600)
    origin = origin_row()
    p500 = label_point(kind6(500, 5500, 5600))
    labels = {p500["key"]: p500}
    with pytest.raises(ValueError, match="prediction was not strictly before label availability"):
        evaluate({key(): pred}, {key(): origin}, labels)



def test_forward_evaluator_fails_closed_without_paper_delay_terms():
    pred = prediction(10)
    origin = origin_row()
    origin.pop("paper_venue_delay_ns")
    origin.pop("paper_assumed_transport_delay_ns")
    p1000 = label_point(kind6(1000, 5500, 5600))
    result = evaluate({key(): pred}, {key(): origin}, {p1000["key"]: p1000})
    cell = result["cells"]["BTC:1000"]
    assert cell["arrival_delay_unavailable"] == 1
    assert cell["simulated_fills"] == 0
