from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_crypto_market_discovery import discover  # noqa: E402
from v7_crypto_settlement import (  # noqa: E402
    CryptoSettlementError, aggregate_correlated_crypto_risk,
    assemble_causal_feature_groups, healthy_source_composite, load_registry,
    require_context, validate_model_artifact, validate_model_registry,
)


REGISTRY = ROOT / "config/v7_crypto_settlement_markets.json"
MODEL_REGISTRY = ROOT / "config/v7_crypto_settlement_model_registry.json"


def model(context) -> dict:
    return {
        "schema": "polymarket_v7_crypto_settlement_model_v1",
        "immutable": True,
        "asset": context.asset.value,
        "horizon": context.horizon.value,
        "model_sha": "a" * 40,
        "training_cut": "2026-08-01T00:00:00Z",
        "feature_schema_hash": "b" * 64,
        "settlement_semantic_hash": context.settlement_semantic_hash,
        "source_registry_hash": "c" * 64,
        "latency_profile_hash": "d" * 64,
        "training_period": "2026-01-01/2026-06-30",
        "validation_period": "2026-07-01/2026-07-31",
        "calibration_artifact": "isotonic-v1",
        "forward_shadow_start": "2026-08-02T00:00:00Z",
    }


def test_eight_verified_contexts_and_no_one_minute_instantiation() -> None:
    contexts = load_registry(REGISTRY)
    assert len(contexts) == 8
    assert {row.asset.value for row in contexts.values()} == {"BTC", "ETH", "SOL", "XRP"}
    assert {row.horizon.value for row in contexts.values()} == {"M5", "M15"}
    assert all(row.research_only for row in contexts.values() if row.asset.value != "BTC")
    try:
        require_context(contexts, "BTC", "M1")
    except CryptoSettlementError as exc:
        assert str(exc) == "unknown_or_unverified_crypto_context"
    else:
        raise AssertionError("unregistered BTC 1m context accepted")


def test_asset_horizon_and_settlement_hash_model_isolation() -> None:
    contexts = load_registry(REGISTRY)
    btc = require_context(contexts, "BTC", "M5")
    validate_model_artifact(model(btc), btc)
    for field, value in (("asset", "ETH"), ("horizon", "M15"),
                         ("settlement_semantic_hash", "0" * 64)):
        wrong = copy.deepcopy(model(btc))
        wrong[field] = value
        try:
            validate_model_artifact(wrong, btc)
        except CryptoSettlementError:
            pass
        else:
            raise AssertionError(f"wrong model binding accepted: {field}")
    wrong_sha = model(btc)
    wrong_sha["model_sha"] = "not-a-sha"
    try:
        validate_model_artifact(wrong_sha, btc)
    except CryptoSettlementError as exc:
        assert str(exc) == "model_sha"
    else:
        raise AssertionError("invalid model SHA accepted")


def test_model_registry_is_complete_indexed_and_zero_authority() -> None:
    contexts = load_registry(REGISTRY)
    value = json.loads(MODEL_REGISTRY.read_text(encoding="utf-8"))
    models = validate_model_registry(value, contexts)
    assert set(models) == set(contexts)
    assert all(artifact is None for artifact in models.values())
    assert all(row["new_risk_authorized"] is False for row in value["models"])


def test_discovery_never_executes_and_rejects_unknown_semantics() -> None:
    contexts = load_registry(REGISTRY)
    eth = require_context(contexts, "ETH", "M5")
    description = (
        "This market uses the time-weighted average price and resolves Up when it is "
        "greater than or equal to the start. " + eth.raw["settlement"]["stream_url"]
    )
    result = discover([
        {"slug": "eth-updown-5m-1788262200", "description": description},
        {"slug": "doge-updown-5m-1788262200", "description": description},
        {"slug": "sol-updown-5m-1788262200", "description": "spot price"},
    ], REGISTRY)
    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["authority"] == "SHADOW_ZERO_AUTHORITY"
    assert result["accepted"][0]["new_risk_authorized"] is False
    assert {row["reason"] for row in result["rejected"]} == {
        "UNRECOGNIZED_CRYPTO_CONTEXT", "SETTLEMENT_SEMANTICS_MISMATCH",
    }


def test_correlated_crypto_risk_has_no_fake_asset_diversification() -> None:
    report = aggregate_correlated_crypto_risk([
        {"asset": "BTC", "horizon": "M5", "signed_exposure_usd": 100},
        {"asset": "ETH", "horizon": "M5", "signed_exposure_usd": 80},
        {"asset": "SOL", "horizon": "M15", "signed_exposure_usd": 50},
        {"asset": "XRP", "horizon": "M15", "signed_exposure_usd": 20},
    ])
    assert report["gross_crypto_exposure_usd"] == 250
    assert report["net_directional_crypto_exposure_usd"] == 250
    assert report["correlated_crypto_cluster_exposure_usd"] == 250
    assert report["independent_asset_diversification_credit_usd"] == 0
    assert report["oracle_concentration_fraction"] == 1.0


def frame(asset: str, timestamp: int, source: str = "source") -> dict:
    return {
        "asset": asset, "source_id": source,
        "receive_timestamp_ns": timestamp, "features": {"return_1s": 0.01},
    }


def test_cross_asset_features_are_receive_time_causal_and_asset_isolated() -> None:
    context = require_context(load_registry(REGISTRY), "ETH", "M5")
    cut = assemble_causal_feature_groups(
        context, decision_receive_timestamp_ns=100,
        local=frame("ETH", 90, "eth-local"),
        oracle=frame("ETH", 91, "eth-oracle"),
        polymarket=frame("ETH", 92, "eth-pm"),
        btc_cross=frame("BTC", 93, "btc-cross"),
        crypto_wide=frame("CRYPTO_WIDE", 94, "crypto-wide"),
    )
    assert cut["groups"]["local"]["asset"] == "ETH"
    assert cut["groups"]["btc_cross"]["asset"] == "BTC"
    for broken in (
        {"local": frame("BTC", 90, "wrong-local")},
        {"btc_cross": frame("ETH", 93, "wrong-cross")},
        {"oracle": frame("ETH", 101, "future-oracle")},
    ):
        values = {
            "local": frame("ETH", 90, "eth-local"),
            "oracle": frame("ETH", 91, "eth-oracle"),
            "polymarket": frame("ETH", 92, "eth-pm"),
            "btc_cross": frame("BTC", 93, "btc-cross"),
        }
        values.update(broken)
        try:
            assemble_causal_feature_groups(
                context, decision_receive_timestamp_ns=100, **values,
            )
        except CryptoSettlementError:
            pass
        else:
            raise AssertionError(f"non-causal or cross-populated feature accepted: {broken}")


def test_source_composite_drops_stale_and_unhealthy_then_renormalizes() -> None:
    context = require_context(load_registry(REGISTRY), "SOL", "M15")
    result = healthy_source_composite(context, [
        {"asset": "SOL", "source_id": "binance", "receive_timestamp_ns": 95,
         "price": 100.0, "weight": 0.6, "healthy": True},
        {"asset": "SOL", "source_id": "coinbase", "receive_timestamp_ns": 94,
         "price": 102.0, "weight": 0.4, "healthy": True},
        {"asset": "SOL", "source_id": "stale", "receive_timestamp_ns": 1,
         "price": 999.0, "weight": 10.0, "healthy": True},
        {"asset": "SOL", "source_id": "unhealthy", "receive_timestamp_ns": 99,
         "price": 999.0, "weight": 10.0, "healthy": False},
    ], decision_receive_timestamp_ns=100, maximum_age_ns=10)
    assert abs(result["price"] - 100.8) < 1e-12
    assert result["weights"] == {"binance": 0.6, "coinbase": 0.4}
    assert result["dropped_sources"] == {"stale": "STALE", "unhealthy": "UNHEALTHY"}
    wrong = [{"asset": "BTC", "source_id": "btc", "receive_timestamp_ns": 99,
              "price": 100.0, "weight": 1.0, "healthy": True}]
    try:
        healthy_source_composite(
            context, wrong, decision_receive_timestamp_ns=100, maximum_age_ns=10,
        )
    except CryptoSettlementError as exc:
        assert str(exc) == "composite_asset_mismatch"
    else:
        raise AssertionError("BTC source populated SOL composite")


def test_source_and_symbol_mappings_are_isolated_by_asset() -> None:
    contexts = load_registry(REGISTRY)
    source_registry = json.loads(
        (ROOT / "config/v7_external_source_registry.json").read_text(encoding="utf-8")
    )
    sources = source_registry["sources"]
    venue_fields = {
        "binance_spot": "BINANCE_SPOT", "coinbase_spot": "COINBASE_SPOT",
        "bybit_spot": "BYBIT_SPOT", "binance_perp": "BINANCE_USDM",
        "bybit_perp": "BYBIT_LINEAR",
    }
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        context = require_context(contexts, asset, "M5")
        mappings = context.raw["external_symbols"]
        for field, venue in venue_fields.items():
            assert any(
                row["asset"] == asset and row["venue"] == venue
                and row["instrument_id"] == mappings[field]
                for row in sources
            ), (asset, field, mappings[field])
    assert require_context(contexts, "ETH", "M5").raw["external_symbols"]["binance_spot"] != \
        require_context(contexts, "BTC", "M5").raw["external_symbols"]["binance_spot"]


if __name__ == "__main__":
    test_eight_verified_contexts_and_no_one_minute_instantiation()
    test_asset_horizon_and_settlement_hash_model_isolation()
    test_model_registry_is_complete_indexed_and_zero_authority()
    test_discovery_never_executes_and_rejects_unknown_semantics()
    test_correlated_crypto_risk_has_no_fake_asset_diversification()
    test_cross_asset_features_are_receive_time_causal_and_asset_isolated()
    test_source_composite_drops_stale_and_unhealthy_then_renormalizes()
    test_source_and_symbol_mappings_are_isolated_by_asset()
