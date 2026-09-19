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
    value = build_selection(snapshot(rows), model_sha="a" * 40, now_unix=1_789_595_000)
    assert value["market_count"] == 6
    assert value["token_count"] == 12
    assert value["execution_authority"] is False
    assert value["real_order_submission"] is False
    assert value["selection_only"] is True
    assert value["model_sha"] == "a" * 40
    assert {row["asset"] for row in value["markets"]} == {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"}
    assert all(row["yes_token"].endswith("-yes") and row["no_token"].endswith("-no") for row in value["markets"])
    assert all(row["start_timestamp_ms"] > 0 and row["end_timestamp_ms"] > row["start_timestamp_ms"] for row in value["markets"])


def test_closed_stale_and_invalid_mapping_are_rejected_not_repaired() -> None:
    good = record("ETH", "M5", "good")
    closed = record("SOL", "M5", "closed"); closed["closed"] = True
    stale = record("DOGE", "M5", "stale", end="2026-09-16T20:00:00Z")
    bad = record("BNB", "M5", "bad"); bad["token_mapping"] = {"YES": "same", "NO": "same"}
    value = build_selection(snapshot([good, closed, stale, bad]), model_sha="a" * 40, now_unix=1_789_595_000)
    assert [row["market_id"] for row in value["markets"]] == ["good"]
    assert value["rejected_records"] == 3


def test_source_with_execution_authority_fails_closed() -> None:
    source = snapshot([record("ETH", "M5", "m")])
    source["execution_authority"] = True
    try:
        build_selection(source, model_sha="a" * 40, now_unix=1_789_595_000)
    except ValueError as exc:
        assert "zero-authority" in str(exc)
    else:
        raise AssertionError("authoritative discovery snapshot accepted")


def test_capacity_is_bounded() -> None:
    rows = [record("ETH", "M5", f"m-{i}") for i in range(3)]
    try:
        build_selection(snapshot(rows), model_sha="a" * 40, now_unix=1_789_595_000, maximum_markets=2)
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
    assert 'arg == "--state-publish-ms"' in source
    assert '"--state-publish-ms must be in [10,1000]"' in source
    assert 'root["state_publish_ms"] = state_publish_ms_;' in source
    assert '"--state-only requires --selection-only"' in source
    assert 'root["book_event_tape_enabled"] = !state_only_;' in source
    assert 'if (!state_only_) {' in source
    assert '"polymarket_v7_multi_crypto_book_selection_v1"' in source
    assert 'text(find_value(object, "model_sha")) != expected_model_sha' in source
    assert 'if (!options.selection_only)' in source
    assert 'reload = !options.selection_only' in source
    assert 'options.fair_only ? 25 : 1000' in source
    assert 'recover_missing_lineage_' in source
    assert 'if (options.fair_only && observer.lineage_recovery_requested())' in source
    assert '(options.fair_only || options.selection_only)' not in source
    assert 'load_selected_pairs(options.selection, options.selection_only, options.model_sha) != selected_pairs' in source
    assert 'token_active(event.instrument_handle, receive.wall_ms)' in source
    assert 'result.price_change_without_lineage > 0' not in source
    assert 'token.start_wall_ms <= now_wall && now_wall < token.end_wall_ms' in source
    assert 'start_timestamp_ms' in (ROOT / 'scripts/v7_multi_crypto_book_selection.py').read_text()


def test_active_only_selection_excludes_future_preload_markets() -> None:
    active=record("ETH","M5","active",end="2026-09-16T23:00:00Z")
    active["start_timestamp"]="2026-09-16T22:45:00Z"
    future=record("SOL","M5","future",end="2026-09-16T23:05:00Z")
    future["start_timestamp"]="2026-09-16T23:00:00Z"
    value=build_selection(snapshot([active,future]),model_sha="a"*40,now_unix=1789599300,active_only=True)
    assert [row["market_id"] for row in value["markets"]]==["active"]
    assert value["active_only"] is True


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
