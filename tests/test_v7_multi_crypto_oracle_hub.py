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


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
