from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_crypto_execution_alpha import (  # noqa: E402
    action_competition,
    information_rank,
    load_config,
    market_score,
    select_crypto_markets,
    validate_execution_alpha_packet,
)


CONFIG = load_config(ROOT / "config" / "v7_crypto_execution_alpha.json")


def packet(action: str, *, quality: float = 1.0) -> dict:
    features = {
        "queue_ahead": 5.0 / quality,
        "spread": 0.01 * quality,
        "book_imbalance": 0.3 * quality,
        "recent_aggressive_flow": 8.0 * quality,
        "quote_lifetime_ms": 250.0,
        "tte_seconds": 90.0 * quality,
        "binance_shock_bp": 0.4 * quality,
        "coinbase_shock_bp": 0.3 * quality,
        "bybit_shock_bp": 0.35 * quality,
        "cross_venue_disagreement_bp": 0.1 / quality,
        "oracle_distance_bp": 0.2 * quality,
        "volatility_bp": 2.0 * quality,
        "latency_ms": 20.0 / quality,
    }
    return {
        "schema": "polymarket_v7_execution_alpha_packet_v1",
        "model_id": "execution-alpha-test",
        "model_hash": "d" * 64,
        "feature_receive_timestamp_ns": 90,
        "features": features,
        "fill_probability": {"lower": min(0.95, 0.20 * quality), "point": min(0.97, 0.30 * quality), "upper": min(0.99, 0.40 * quality)},
        "markout_per_share": {"lower": -0.005 / quality, "point": -0.002 / quality, "upper": 0.001},
        "toxic_fill_probability": {"lower": 0.05, "point": min(0.8, 0.10 / quality + 0.05), "upper": min(0.9, 0.20 / quality + 0.05)},
        "action_ev": {
            "MAKE": {"point": 1.0, "conservative": 0.5},
            "TAKE": {"point": 1.2, "conservative": 0.7},
            "CANCEL": {"point": 0.1, "conservative": 0.05},
            "NOTHING": {"point": 0.0, "conservative": 0.0},
        },
        "selected_action": action,
        "evidence_status": "IMMATURE",
    }


def envelope(*, market: str, action: str = "MAKE", ev: float = 1.0, quality: float = 1.0, key: str | None = None) -> dict:
    return {
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "action": action,
        "market_id": market,
        "crypto_context": {"asset": "BTC", "horizon": "M5"},
        "deterministic_replay_key": key or f"{market}-{action}",
        "conservative_expected_wealth_change": ev,
        "fair_value": {"lower": 0.56, "point": 0.60, "upper": 0.64},
        "capacity": {"executable_size": 10.0 * quality},
        "latency": {"arrival_ns": int(20_000_000 / quality)},
        "cost_vector": {"adverse_markout": 0.002 / quality},
        "execution_plan": {"legs": [{"limit_price": 0.50}]},
        "uncertainty": {"lower_bound": -0.1, "upper_bound": 0.2},
        "execution_alpha": packet(action, quality=quality),
    }


def test_packet_enforces_receive_time_and_action_identity() -> None:
    value = packet("MAKE")
    assert validate_execution_alpha_packet(value, decision_ns=100, action="MAKE")["model_id"] == "execution-alpha-test"
    bad = json.loads(json.dumps(value))
    bad["feature_receive_timestamp_ns"] = 101
    try:
        validate_execution_alpha_packet(bad, decision_ns=100, action="MAKE")
    except ValueError as exc:
        assert str(exc) == "packet_feature_clock"
    else:
        raise AssertionError("future execution feature cut accepted")


def test_market_selection_concentrates_budget_on_top_fraction() -> None:
    rows = [
        envelope(market="m1", ev=0.2, quality=0.5),
        envelope(market="m2", ev=0.3, quality=0.8),
        envelope(market="m3", ev=0.4, quality=1.0),
        envelope(market="m4", ev=0.5, quality=2.0),
    ]
    result = select_crypto_markets(rows, CONFIG)
    assert result.diagnostics["market_count"] == 4
    assert result.diagnostics["retained_market_count"] == 1
    assert result.diagnostics["retained_market_keys"] == ["BTC|M5|m4"]
    assert result.retained_replay_keys == frozenset({"m4-MAKE"})


def test_market_score_exposes_component_decomposition() -> None:
    score = market_score(envelope(market="m1"), CONFIG)
    assert 0.0 <= score["score"] <= 1.0
    assert set(score["components"]) >= {
        "predictability", "mispricing", "depth", "latency", "fillability", "adverse_selection",
    }
    assert score["components"]["missing_execution_features"] == []


def test_make_and_take_compete_on_the_same_market() -> None:
    rows = [
        envelope(market="m1", action="MAKE", ev=0.6, key="make"),
        envelope(market="m1", action="TAKE", ev=1.2, key="take"),
    ]
    result = action_competition(rows)["BTC|M5|m1"]
    assert result["best_action"] == "TAKE"
    assert result["actions"]["MAKE"]["conservative_ev"] == 0.6
    assert result["actions"]["TAKE"]["conservative_ev"] == 1.2


def test_information_rank_rewards_uncertainty_without_promotion_credit() -> None:
    low = envelope(market="m1", action="TAKE", key="low")
    high = envelope(market="m2", action="TAKE", key="high")
    low["exploration"] = {"information_score": 1.0, "point_expected_wealth_change": 0.1}
    high["exploration"] = {"information_score": 1.0, "point_expected_wealth_change": 0.1}
    low["uncertainty"] = {"lower_bound": -0.01, "upper_bound": 0.01}
    high["uncertainty"] = {"lower_bound": -0.5, "upper_bound": 0.5}
    assert information_rank(high, CONFIG) > information_rank(low, CONFIG)


if __name__ == "__main__":
    test_packet_enforces_receive_time_and_action_identity()
    test_market_selection_concentrates_budget_on_top_fraction()
    test_market_score_exposes_component_decomposition()
    test_make_and_take_compete_on_the_same_market()
    test_information_rank_rewards_uncertainty_without_promotion_credit()
