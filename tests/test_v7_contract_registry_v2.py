#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_contract_registry import market as btc_market  # noqa: E402
from v7_contract_registry import (  # noqa: E402
    PARSER_VERSION,
    PARSER_VERSION_V2,
    contract_from_market,
    contract_from_market_v2,
)

V1_HASH = "9ab964d9446b4dd86c987915b0918bacc8aee066b19878dadc49936b218f3645"
V1_CONTRACT_VERSION = 3818579448567966459


def crypto_market(asset: str, minutes: int, *, source_asset: str | None = None) -> dict:
    names = {
        "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
        "XRP": "XRP", "DOGE": "Dogecoin", "BNB": "BNB",
    }
    source_asset = source_asset or asset
    raw = btc_market(
        f"https://data.chain.link/streams/{source_asset.lower()}-usd-twap-60s-streams"
    )
    raw["id"] = f"{asset.lower()}-{minutes}m-market"
    raw["eventId"] = f"{asset.lower()}-{minutes}m-event"
    raw["conditionId"] = f"{asset.lower()}-{minutes}m-condition"
    raw["slug"] = f"{asset.lower()}-updown-{minutes}m-test"
    raw["question"] = f"{names[asset]} Up or Down {minutes}m"
    raw["rules"] = raw["rules"].replace("Bitcoin", names[asset])
    raw["eventStartTime"] = "2026-09-16T20:00:00Z"
    raw["endDate"] = f"2026-09-16T20:{minutes:02d}:00Z"
    raw["tokens"] = [
        {"outcome": "Down", "token_id": f"{asset.lower()}-no"},
        {"outcome": "Up", "token_id": f"{asset.lower()}-yes"},
    ]
    return raw


def test_v1_identity_is_unchanged() -> None:
    spec = contract_from_market(btc_market())
    assert spec.parser_version == PARSER_VERSION == "btc-updown-chainlink-twap-v1"
    assert spec.normalized_rules_hash == V1_HASH
    assert spec.contract_version == V1_CONTRACT_VERSION
    assert spec.contract_family == "BTC_USD_UPDOWN_5M"


def test_v2_eth_m5_verified_but_not_authorized_without_rule_approval() -> None:
    spec = contract_from_market_v2(crypto_market("ETH", 5))
    assert spec.parser_version == PARSER_VERSION_V2
    assert spec.verified_template is True
    assert spec.asset == "ETH"
    assert spec.quote_currency == "USD"
    assert spec.contract_family == "ETH_USD_UPDOWN_M5"
    assert spec.oracle_window_seconds == 60
    assert spec.rules_hash_recognized is False
    assert spec.informed_trading_authorized is False


def test_v2_sol_m15_requires_independent_rule_approval() -> None:
    first = contract_from_market_v2(crypto_market("SOL", 15))
    approved = contract_from_market_v2(
        crypto_market("SOL", 15), approved_rule_hashes={first.normalized_rules_hash}
    )
    assert first.contract_family == "SOL_USD_UPDOWN_M15"
    assert approved.rules_hash_recognized is True
    assert approved.informed_trading_authorized is True


def test_v2_token_order_is_not_assumed() -> None:
    spec = contract_from_market_v2(crypto_market("XRP", 5))
    assert spec.verified_template is True
    # Explicit outcomes are mapped even though Down is first in the input array.
    from v7_contract_registry import hash_handle
    assert spec.yes_instrument_handle == hash_handle("xrp-yes")
    assert spec.no_instrument_handle == hash_handle("xrp-no")


def test_v2_asset_title_source_mismatch_fails_closed() -> None:
    raw = crypto_market("ETH", 5, source_asset="SOL")
    spec = contract_from_market_v2(raw)
    assert spec.verified_template is False
    assert spec.contract_family == "UNVERIFIED"
    assert "title:asset_mismatch" in spec.verification_reasons


def test_v2_unsupported_horizon_fails_closed() -> None:
    raw = crypto_market("DOGE", 10)
    spec = contract_from_market_v2(raw)
    assert spec.verified_template is False
    assert "horizon:unsupported" in spec.verification_reasons


def test_v2_parser_identity_is_distinct_from_frozen_v1() -> None:
    raw = btc_market()
    raw["eventStartTime"] = "2026-08-28T18:00:00Z"
    v1 = contract_from_market(raw)
    v2 = contract_from_market_v2(raw)
    assert v1.normalized_rules_hash != v2.normalized_rules_hash
    assert v1.contract_version != v2.contract_version
    assert v1.parser_version != v2.parser_version


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
