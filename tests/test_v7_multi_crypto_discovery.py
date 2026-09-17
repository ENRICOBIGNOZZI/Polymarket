#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_contract_registry_v2 import crypto_market  # noqa: E402
from v7_multi_crypto_discovery import (  # noqa: E402
    build_snapshot,
    candidate_asset,
    discover_raw,
    discover_window_hints,
    explicit_token_mapping,
    load_json,
)


def test_keyset_pagination_is_exhaustive_and_deduplicated() -> None:
    first = crypto_market("ETH", 5)
    second = crypto_market("SOL", 15)
    calls: list[str] = []

    def fetcher(url: str, timeout: int):
        calls.append(url)
        if "after_cursor=" not in url:
            return {"markets": [first], "next_cursor": "cursor-1"}
        return {"markets": [first, second], "next_cursor": ""}

    rows, meta = discover_raw(
        "https://gamma.example", page_size=100, max_pages=5, timeout=1, fetcher=fetcher
    )
    assert meta["discovery_exhaustive"] is True
    assert meta["pages"] == 2
    assert meta["duplicate_rows"] == 1
    assert len(rows) == 2
    assert "after_cursor=cursor-1" in calls[1]


def test_windowed_discovery_uses_slug_as_hint_and_does_not_invent_missing_markets() -> None:
    config = load_json(ROOT / "config/v7_multi_crypto_assets.json")
    eth = crypto_market("ETH", 5)
    eth["slug"] = "eth-updown-5m-1789593600"
    calls: list[str] = []

    def fetcher(url: str, timeout: int):
        calls.append(url)
        return [eth] if "slug=eth-updown-5m-1789593600" in url else []

    rows, meta = discover_window_hints(
        config, "https://gamma.example", now_s=1789593745, windows=2, timeout=1,
        fetcher=fetcher,
    )
    assert len(calls) == 24  # 6 assets x 2 horizons x current/next
    assert len(rows) == 1
    assert rows[0]["id"] == eth["id"]
    assert meta["hits"] == 1
    assert meta["unique_markets"] == 1
    assert meta["discovery_exhaustive"] is False


def test_snapshot_uses_rules_as_proof_and_slug_only_as_hint() -> None:
    config = load_json(ROOT / "config/v7_multi_crypto_assets.json")
    eth = crypto_market("ETH", 5)
    unrelated = {"id": "sports", "question": "Team A vs Team B", "slug": "sports"}
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = build_snapshot(
            [eth, unrelated], config=config, approved_rule_hashes=set(),
            fetched_at_ms=1000,
            discovery={"discovery_exhaustive": True},
            rules_dir=Path(tmp),
        )
        assert snapshot["candidate_markets"] == 1
        row = snapshot["records"][0]
        assert row["asset_hint"] == "ETH"
        assert row["state"] == "RULES_VERIFIED"
        assert row["verified_template"] is True
        assert row["rules_hash_recognized"] is False
        assert row["informed_trading_authorized"] is False
        assert row["token_mapping"] == {"NO": "eth-no", "YES": "eth-yes"}
        assert (Path(tmp) / f"{row['market_id']}.json").exists()


def test_approved_hash_is_explicit_and_does_not_grant_execution_authority() -> None:
    config = load_json(ROOT / "config/v7_multi_crypto_assets.json")
    sol = crypto_market("SOL", 5)
    first = build_snapshot(
        [sol], config=config, approved_rule_hashes=set(), fetched_at_ms=1,
        discovery={"discovery_exhaustive": True},
    )
    rule_hash = first["records"][0]["normalized_rules_hash"]
    approved = build_snapshot(
        [sol], config=config, approved_rule_hashes={rule_hash}, fetched_at_ms=2,
        discovery={"discovery_exhaustive": True},
    )
    row = approved["records"][0]
    assert row["state"] == "RULES_VERIFIED_APPROVED_HASH"
    assert row["informed_trading_authorized"] is True
    assert approved["execution_authority"] is False
    assert approved["real_order_submission"] is False


def test_asset_source_mismatch_is_invalid_rules() -> None:
    config = load_json(ROOT / "config/v7_multi_crypto_assets.json")
    bad = crypto_market("ETH", 5, source_asset="SOL")
    snapshot = build_snapshot(
        [bad], config=config, approved_rule_hashes=set(), fetched_at_ms=1,
        discovery={"discovery_exhaustive": True},
    )
    row = snapshot["records"][0]
    assert row["state"] == "INVALID_RULES"
    assert row["verified_template"] is False
    assert "title:asset_mismatch" in row["verification_reasons"]


def test_explicit_outcome_mapping_does_not_assume_array_order() -> None:
    raw = crypto_market("XRP", 5)
    assert explicit_token_mapping(raw) == {"NO": "xrp-no", "YES": "xrp-yes"}
    assert candidate_asset(raw, {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}) == "XRP"


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
