from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_fill_conditioned_toxicity",
    ROOT / "scripts" / "v7_fill_conditioned_toxicity.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_fill_record_preserves_shadow_score_and_causal_features() -> None:
    row = {
        "event_type": "FILL",
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": "a" * 40,
        "paper_only": True,
        "authenticated_execution": False,
        "fill_id": "fill-1",
        "order_id": "order-1",
        "market_id": "market-1",
        "event_id": "event-1",
        "token_id": "token-1",
        "side": "BUY",
        "receive_ts_ms": 1000,
        "fill_price": 0.51,
        "filled_size": 2.0,
        "queue_ahead": 3.0,
        "predicted_fill_probability": 0.02,
        "metadata": {
            "component": "professional_maker",
            "execution_authority": "SIMULATED_PAPER_ONLY",
            "placement_features": {
                "microstructure_shadow_delta_250ms": 0.006,
                "imbalance": 0.7,
                "ofi": 0.3,
                "cancel_intensity": 0.1,
            },
        },
    }
    assert MODULE.valid_fill(row, "a" * 40)
    record = MODULE.fill_record(row)
    assert record["microstructure_shadow_delta_250ms"] == 0.006
    assert record["imbalance"] == 0.7
    assert record["queue_ahead"] == 3.0


def test_market_cluster_bootstrap_stays_positive_for_positive_clusters() -> None:
    rows = [
        {"market_id": "m1", "markout_250ms": 0.01},
        {"market_id": "m1", "markout_250ms": 0.02},
        {"market_id": "m2", "markout_250ms": 0.03},
        {"market_id": "m2", "markout_250ms": 0.04},
    ]
    result = MODULE.market_cluster_bootstrap(rows, "markout_250ms", draws=200, seed=1)
    assert result["market_count"] == 2
    assert result["ci95"][0] > 0.0


def test_market_split_is_disjoint_and_forward_ordered() -> None:
    rows = [
        {"market_id": f"m{i}", "receive_ts_ms": i * 1000, "markout_250ms": 0.01}
        for i in range(6)
    ]
    train, test = MODULE.train_test_market_split(rows, train_fraction=0.5)
    train_ids = {row["market_id"] for row in train}
    test_ids = {row["market_id"] for row in test}
    assert train_ids.isdisjoint(test_ids)
    assert train_ids == {"m0", "m1", "m2"}
    assert test_ids == {"m3", "m4", "m5"}


def test_ridge_toxicity_model_uses_market_separated_oos() -> None:
    rows = []
    for market_index in range(10):
        for fill_index in range(3):
            score = -0.01 + 0.0025 * (market_index * 3 + fill_index)
            row = {
                "market_id": f"m{market_index}",
                "receive_ts_ms": market_index * 10000 + fill_index,
                "markout_250ms": 0.001 + 1.8 * score,
                "microstructure_shadow_delta_250ms": score,
                "imbalance": score * 10.0,
                "ofi": score * 5.0,
                "cancel_intensity": 0.2 + fill_index * 0.01,
                "trade_intensity": 1.0 + market_index * 0.02,
                "aggressive_sell_prints_per_second": 2.0 + fill_index,
                "short_return_ticks": score * 3.0,
                "local_latency_ms": 1.0 + fill_index * 0.1,
                "queue_ahead": 2.0 + fill_index,
                "predicted_fill_probability": 0.01 + fill_index * 0.002,
            }
            rows.append(row)
    result = MODULE.fitted_toxicity_model(rows, "250ms", ridge=1.0)
    assert result["status"] == "OK"
    assert result["train_markets"] == 7
    assert result["test_markets"] == 3
    assert result["test_prediction_outcome_correlation"] is not None
    assert result["test_prediction_outcome_correlation"] > 0.8
    assert result["execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
