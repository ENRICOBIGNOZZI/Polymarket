import hashlib
import json
from pathlib import Path

from scripts.v7_executable_markout_forward_shadow import (
    HORIZONS,
    decision,
    load_artifact,
    predict,
)


def artifact():
    names = ["bid_e4"]
    spec = {
        "state": "READY",
        "target": "future_executable_bid_minus_decision_ask_minus_entry_and_exit_taker_fees",
        "feature_names": names,
        "center": {"bid_e4": 5000.0},
        "scale": {"bid_e4": 1000.0},
        "beta": [0.01, 0.02, -0.03],
    }
    return {
        "schema": "historical_walk_forward_v2_full_window_repricing_models_v2",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "executable_markout_models": {str(h): dict(spec) for h in HORIZONS},
    }


def native(kind=2):
    return {
        "schema": "polymarket_v7_native_observation_v1",
        "kind": kind,
        "paper_only": True,
        "execution_authority": False,
        "signal_valid": True,
        "confirmed_non_opposing": True,
        "book_valid": True,
        "server_id": "s",
        "run_id": "r",
        "capture_id": "c",
        "market_id": "m",
        "token_id": "t",
        "asset": "BTC",
        "horizon": "M5",
        "signal_version": 7,
        "decision_monotonic_ns": 2_000_000_000,
        "decision_wall_ns": 1_800_000_000_000_000_000,
        "trigger_monotonic_ns": 1_999_000_000,
        "receive_monotonic_ns": 1_999_500_000,
        "bid_e4": 5500,
        "ask_e4": 5600,
        "signal_return_bp": 1.0,
        "signal_age_ns": 1_000_000,
        "tte_ns": 100_000_000_000,
        "ask_quantity": 5_000_000,
        "external_features": {"input_receive_ns": 1_999_000_000},
    }


def test_artifact_hash_and_prediction_contract(tmp_path: Path):
    path = tmp_path / "artifact.json"
    raw = json.dumps(artifact(), sort_keys=True, separators=(",", ":"))
    path.write_text(raw)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    _, models = load_artifact(path, sha)
    value = predict({"bid_e4": 5500.0}, models[500])
    assert abs(value - 0.02) < 1e-12


def test_shadow_scores_kind2_only_and_preserves_causal_feature_cut():
    row = native()
    item = decision(row)
    assert item is not None
    assert item["asset"] == "BTC"
    assert item["features"]["bid_e4"] == 5500.0

    label = native(kind=6)
    assert decision(label) is None

    future = native()
    future["external_features"]["input_receive_ns"] = future["decision_monotonic_ns"] + 1
    assert decision(future) is None


def test_shadow_rejects_authority_or_unconfirmed_rows():
    row = native()
    row["real_order_submission"] = True
    assert decision(row) is None
    row = native()
    row["confirmed_non_opposing"] = False
    assert decision(row) is None
