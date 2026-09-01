from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_opportunity import envelope  # noqa: E402
from v7_crypto_settlement import (  # noqa: E402
    assemble_causal_feature_groups, load_registry, require_context,
)
from v7_opportunity import OpportunityEnvelope, coordinate  # noqa: E402


def test_btc_5m_and_15m_registry_economics_match_frozen_migration_baseline() -> None:
    baseline = json.loads((ROOT / "config/v7_crypto_btc_migration_baseline.json").read_text())
    contexts = load_registry(ROOT / "config/v7_crypto_settlement_markets.json")
    for horizon in ("M5", "M15"):
        context = require_context(contexts, "BTC", horizon)
        expected = baseline["contexts"][horizon]
        assert context.contract_family == expected["contract_family"]
        assert context.horizon_seconds == expected["horizon_seconds"]
        assert context.settlement_semantic_hash == expected["settlement_semantic_hash"]
        assert context.raw["maker_tte_window_seconds"] == expected["maker_tte_window_seconds"]
        assert context.raw["taker_tte_window_seconds"] == expected["taker_tte_window_seconds"]
        assert context.raw["external_symbols"] == baseline["external_symbols"]


def test_btc_feature_values_and_receive_timestamp_survive_context_migration() -> None:
    context = require_context(
        load_registry(ROOT / "config/v7_crypto_settlement_markets.json"), "BTC", "M5",
    )
    frame = lambda source, values, timestamp: {  # noqa: E731
        "asset": "BTC", "source_id": source,
        "receive_timestamp_ns": timestamp, "features": values,
    }
    legacy_features = {
        "local": frame("btc-local", {"return_1s": 0.001, "ofi": -0.2}, 90),
        "oracle": frame("btc-oracle", {"twap": 65000.0, "age_ms": 25.0}, 91),
        "polymarket": frame("btc-pm", {"yes_bid": 0.49, "yes_ask": 0.51}, 92),
    }
    cut = assemble_causal_feature_groups(
        context, decision_receive_timestamp_ns=100, **legacy_features,
    )
    assert cut["decision_receive_timestamp_ns"] == 100
    assert cut["groups"]["local"] == legacy_features["local"]
    assert cut["groups"]["oracle"] == legacy_features["oracle"]
    assert cut["groups"]["polymarket"] == legacy_features["polymarket"]


def test_btc_opportunity_and_risk_gating_keep_one_engine_ledger_attribution() -> None:
    context = require_context(
        load_registry(ROOT / "config/v7_crypto_settlement_markets.json"), "BTC", "M15",
    )
    value = envelope(horizon="M15", key="btc-m15")
    value["crypto_context"].update({
        "contract_family": context.contract_family,
        "settlement_semantic_hash": context.settlement_semantic_hash,
    })
    parsed = OpportunityEnvelope.parse(value)
    assert parsed.engine_id == "CRYPTO_SETTLEMENT_ENGINE"
    assert parsed.raw["market_id"] == "market-1"
    assert parsed.raw["model_sha"] == "a" * 40
    blocked = coordinate([value], now_ns=150, new_risk_authorized=False)
    assert blocked["action"] == "NOTHING"
    selected = coordinate([value], now_ns=150, new_risk_authorized=True)
    assert selected["action"] == "TAKE"
    assert selected["engine_id"] == "CRYPTO_SETTLEMENT_ENGINE"
    assert selected["crypto_context"]["horizon"] == "M15"


if __name__ == "__main__":
    test_btc_5m_and_15m_registry_economics_match_frozen_migration_baseline()
    test_btc_feature_values_and_receive_timestamp_survive_context_migration()
    test_btc_opportunity_and_risk_gating_keep_one_engine_ledger_attribution()
