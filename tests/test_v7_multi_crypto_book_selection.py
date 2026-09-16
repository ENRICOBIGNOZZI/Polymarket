#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_multi_crypto_book_selection import build_selection  # noqa: E402


def record(asset: str, horizon: str, market: str, *, end: str = "2026-09-16T23:00:00Z") -> dict:
    slug_horizon = "5m" if horizon == "M5" else "15m"
    return {
        "asset_hint": asset,
        "market_id": market,
        "event_id": "event-" + market,
        "slug": f"{asset.lower()}-updown-{slug_horizon}-1789596000",
        "contract_family": f"{asset}_USD_UPDOWN_{horizon}",
        "start_timestamp": "2026-09-16T22:45:00Z",
        "end_timestamp": end,
        "normalized_rules_hash": "a" * 64,
        "rule_snapshot_sha256": "b" * 64,
        "verified_template": True,
        "accepting_orders": True,
        "closed": False,
        "token_mapping": {"YES": market + "-yes", "NO": market + "-no"},
    }


def snapshot(rows: list[dict]) -> dict:
    return {
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "fetched_at_ms": 1_789_596_000_000,
        "records": rows,
    }


def test_six_asset_selection_is_zero_authority_and_explicitly_mapped() -> None:
    rows = [record(asset, "M5", f"m-{asset.lower()}")
            for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")]
    value = build_selection(snapshot(rows), now_unix=1_789_595_000)
    assert value["market_count"] == 6
    assert value["token_count"] == 12
    assert value["execution_authority"] is False
    assert value["real_order_submission"] is False
    assert value["selection_only"] is True
    assert {row["asset"] for row in value["markets"]} == {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
    assert all(row["yes_token"].endswith("-yes") and row["no_token"].endswith("-no") for row in value["markets"])


def test_closed_stale_and_invalid_mapping_are_rejected_not_repaired() -> None:
    good = record("ETH", "M5", "good")
    closed = record("SOL", "M5", "closed"); closed["closed"] = True
    stale = record("DOGE", "M5", "stale", end="2026-09-16T20:00:00Z")
    bad = record("BNB", "M5", "bad"); bad["token_mapping"] = {"YES": "same", "NO": "same"}
    value = build_selection(snapshot([good, closed, stale, bad]), now_unix=1_789_595_000)
    assert [row["market_id"] for row in value["markets"]] == ["good"]
    assert value["rejected_records"] == 3


def test_source_with_execution_authority_fails_closed() -> None:
    source = snapshot([record("ETH", "M5", "m")])
    source["execution_authority"] = True
    try:
        build_selection(source, now_unix=1_789_595_000)
    except ValueError as exc:
        assert "zero-authority" in str(exc)
    else:
        raise AssertionError("authoritative discovery snapshot accepted")


def test_capacity_is_bounded() -> None:
    rows = [record("ETH", "M5", f"m-{i}") for i in range(3)]
    try:
        build_selection(snapshot(rows), now_unix=1_789_595_000, maximum_markets=2)
    except ValueError as exc:
        assert "bounded observer capacity" in str(exc)
    else:
        raise AssertionError("selection exceeded bounded capacity")


def test_observer_selection_only_preserves_default_behavior_and_blocks_fair_injection() -> None:
    source = (ROOT / "src/v7_maker_fillability_observer.cpp").read_text()
    assert 'arg == "--selection-only"' in source
    assert '"--fair-only and --selection-only are mutually exclusive"' in source
    assert '"--selection-only requires explicit --selection"' in source
    assert 'arg == "--state-only"' in source
    assert '"--state-only requires --selection-only"' in source
    assert 'root["book_event_tape_enabled"] = !state_only_;' in source
    assert 'if (!state_only_) {' in source
    assert '"polymarket_v7_multi_crypto_book_selection_v1"' in source
    assert 'if (!options.selection_only)' in source
    assert 'reload = !options.selection_only' in source
    assert 'options.fair_only ? 25 : 1000' in source


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
