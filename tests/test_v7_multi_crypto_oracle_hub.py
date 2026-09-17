#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_multi_crypto_oracle_hub import (  # noqa: E402
    ASSETS, apply_observation, bindings_from_registry, empty_state, snapshot,
    load_contract_selection, update_references,
)


def registry() -> dict:
    return json.loads((ROOT / "config/v7_crypto_settlement_markets.json").read_text())


def test_registry_binds_all_six_assets_to_same_m5_m15_oracle_semantics() -> None:
    bindings = bindings_from_registry(registry())
    assert tuple(bindings) == ASSETS
    for asset, row in bindings.items():
        assert row["symbol"] == f"{asset.lower()}/usd"
        assert row["window_seconds"] == 60
        assert row["comparison_operator"] == "GREATER_THAN_OR_EQUAL"
        assert row["stream_url"].startswith("https://data.chain.link/")
        assert len(row["settlement_semantic_hashes"]) == 1


def test_observation_is_asset_isolated_and_duplicate_safe() -> None:
    state = empty_state(bindings_from_registry(registry()))
    row = {"topic": "crypto_prices_twap_sixty", "symbol": "eth/usd", "price": 2500.0,
           "price_decimal": "2500.000000000000000000", "timestamp_ms": 1000,
           "window_seconds": 60}
    assert apply_observation(state, row, receive_wall_ns=1_000_000_000,
                             receive_monotonic_ns=500) is True
    assert state["ETH"]["valid"] is True and state["ETH"]["version"] == 1
    assert all(not state[asset]["valid"] for asset in ASSETS if asset != "ETH")
    assert apply_observation(state, row, receive_wall_ns=1_001_000_000,
                             receive_monotonic_ns=600) is True
    assert state["ETH"]["version"] == 1 and state["ETH"]["duplicates"] == 1


def test_out_of_order_and_future_clock_are_rejected() -> None:
    state = empty_state(bindings_from_registry(registry()))
    good = {"topic": "crypto_prices_twap_sixty", "symbol": "btc/usd", "price": 100.0,
            "price_decimal": "100", "timestamp_ms": 10_000, "window_seconds": 60}
    assert apply_observation(state, good, receive_wall_ns=10_000_000_000,
                             receive_monotonic_ns=1) is True
    old = dict(good, timestamp_ms=9_000, price=99.0, price_decimal="99")
    assert apply_observation(state, old, receive_wall_ns=10_001_000_000,
                             receive_monotonic_ns=2) is False
    future = dict(good, timestamp_ms=20_001, price=101.0, price_decimal="101")
    assert apply_observation(state, future, receive_wall_ns=10_001_000_000,
                             receive_monotonic_ns=3) is False
    assert state["BTC"]["out_of_order"] == 1
    assert state["BTC"]["future_clock_rejections"] == 1
    assert state["BTC"]["price_decimal"] == "100"


def test_missing_asset_context_fails_closed() -> None:
    value = registry()
    value["contexts"] = [row for row in value["contexts"] if row.get("asset") != "BNB"]
    try:
        bindings_from_registry(value)
    except ValueError as exc:
        assert "BNB" in str(exc)
    else:
        raise AssertionError("missing BNB oracle context accepted")



def test_reference_before_boundary_is_proxy_not_exact_reference() -> None:
    selection = {
        "schema": "polymarket_v7_multi_crypto_book_selection_v1",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "markets": [{
            "asset": "ETH", "horizon": "M5", "market_id": "m-eth",
            "start_timestamp": "2026-09-16T22:00:00Z",
            "end_timestamp": "2026-09-16T22:05:00Z",
            "normalized_rules_hash": "c" * 64,
        }],
    }
    contracts = load_contract_selection(selection)
    boundary = contracts[0]["start_timestamp_ms"]
    history = {asset: {} for asset in ASSETS}
    history["ETH"][boundary - 1000] = {"price": 2400.0, "price_decimal": "2400", "available_wall_ns": (boundary - 900) * 1_000_000}
    history["ETH"][boundary + 100] = {"price": 9999.0, "price_decimal": "9999"}
    references: dict[str, dict] = {}
    update_references(contracts, history, references, now_ms=boundary + 500,
                      maximum_gap_ms=2000)
    ref = references["m-eth"]
    assert ref["valid"] is False
    assert ref["is_proxy"] is True
    assert ref["status"] == "PROXY_NOT_EXACT_BOUNDARY"
    assert ref["price_decimal"] == "2400"
    assert ref["source_timestamp_ms"] == boundary - 1000
    assert ref["gap_ms"] == 1000


def test_reference_gap_and_late_start_fail_closed() -> None:
    selection = {
        "schema": "polymarket_v7_multi_crypto_book_selection_v1",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "markets": [{
            "asset": "SOL", "horizon": "M5", "market_id": "m-sol",
            "start_timestamp": "2026-09-16T22:00:00Z",
            "end_timestamp": "2026-09-16T22:05:00Z",
            "normalized_rules_hash": "d" * 64,
        }],
    }
    contracts = load_contract_selection(selection); boundary = contracts[0]["start_timestamp_ms"]
    history = {asset: {} for asset in ASSETS}
    history["SOL"][boundary - 2501] = {"price": 100.0, "price_decimal": "100"}
    references: dict[str, dict] = {}
    update_references(contracts, history, references, now_ms=boundary + 1,
                      maximum_gap_ms=2000)
    assert references["m-sol"]["valid"] is False
    assert references["m-sol"]["status"] == "MISSING_REFERENCE"
    assert references["m-sol"]["gap_ms"] == 2501
    # Observation learned after the boundary can never repair the historical reference.
    history["SOL"][boundary + 1] = {"price": 101.0, "price_decimal": "101"}
    update_references(contracts, history, references, now_ms=boundary + 2,
                      maximum_gap_ms=2000)
    assert references["m-sol"]["valid"] is False


def test_future_contract_waits_for_boundary() -> None:
    selection = {
        "schema": "polymarket_v7_multi_crypto_book_selection_v1",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": False,
        "markets": [{
            "asset": "BNB", "horizon": "M15", "market_id": "m-bnb",
            "start_timestamp": "2026-09-16T22:15:00Z",
            "end_timestamp": "2026-09-16T22:30:00Z",
            "normalized_rules_hash": "e" * 64,
        }],
    }
    contracts = load_contract_selection(selection); boundary = contracts[0]["start_timestamp_ms"]
    history = {asset: {} for asset in ASSETS}; references: dict[str, dict] = {}
    update_references(contracts, history, references, now_ms=boundary - 1,
                      maximum_gap_ms=2000)
    assert references["m-bnb"]["status"] == "AWAITING_BOUNDARY"
    assert references["m-bnb"]["valid"] is False

def test_status_has_no_execution_authority() -> None:
    state = empty_state(bindings_from_registry(registry()))
    transport = {asset: {"connection_epoch": 1, "reconnects": 0, "gaps": 0,
                         "connected": True, "last_error": "", "observations_accepted": 0}
                 for asset in ASSETS}
    value = snapshot(state, model_sha="a" * 40, transport_by_asset=transport,
                     maximum_receive_age_ms=3000, running=True)
    assert value["paper_only"] is True
    assert value["authenticated_execution"] is False
    assert value["real_order_submission"] is False
    assert value["execution_authority"] is False
    assert value["one_way_latency_identified"] is False



def test_exact_boundary_reference_requires_causal_receive_provenance() -> None:
    contract = {"asset": "ETH", "horizon": "M5", "market_id": "market",
                "start_timestamp_ms": 10_000, "end_timestamp_ms": 310_000,
                "normalized_rules_hash": "a" * 64}
    history = {asset: {} for asset in ASSETS}
    observation = {"price": 100.0, "price_decimal": "100", "available_wall_ns": 10_001_000_000}
    history["ETH"][10_000] = observation
    refs = {}
    update_references([contract], history, refs, now_ms=10_002, maximum_gap_ms=2000)
    ref = refs["market"]
    assert ref["valid"] is True and ref["is_proxy"] is False
    assert ref["available_wall_ns"] == 10_002_000_000
    assert ref["observation_received_wall_ns"] == 10_001_000_000
    for unavailable in (None, 20_000_000_000):
        observation["available_wall_ns"] = unavailable
        refs = {}
        update_references([contract], history, refs, now_ms=10_002, maximum_gap_ms=2000)
        assert refs["market"]["valid"] is False
        assert refs["market"]["status"] == "MISSING_OR_FUTURE_RECEIVE_PROVENANCE"

if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
