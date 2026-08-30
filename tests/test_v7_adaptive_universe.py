import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_adaptive_universe as universe

SHA = "a" * 40


def config() -> dict:
    return json.loads((ROOT / "config" / "v7_adaptive_universe.json").read_text())


def market(index: int, **overrides):
    row = {
        "id": str(index), "conditionId": f"c{index}", "clobTokenIds": [f"y{index}", f"n{index}"],
        "outcomes": ["Yes", "No"], "liquidityNum": 10000 - index,
        "outcomePrices": ["0.45", "0.55"], "bestBid": 0.44, "bestAsk": 0.46,
        "spread": 0.02, "lastTradePrice": 0.45,
        "description": "rules", "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "eventStartTime": "2026-08-29T16:30:00Z",
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True}, "feesEnabled": True,
        "volume24hr": index + 1, "active": True, "closed": False, "acceptingOrders": True,
        "events": [{"id": f"e{index // 2}"}],
    }
    row.update(overrides)
    return row


def test_discovery_pages_to_venue_exhaustion_beyond_one_thousand():
    cfg = config()
    rows = [market(index) for index in range(1234)]
    seen_cursors = []

    def fake(url, timeout):
        del timeout
        assert urlparse(url).path.endswith("/markets/keyset")
        query = parse_qs(urlparse(url).query)
        offset = int(query.get("after_cursor", ["0"])[0])
        limit = int(query["limit"][0])
        seen_cursors.append(offset)
        page = rows[offset:offset + limit]
        return {"markets": page, "next_cursor": str(offset + len(page)) if page else ""}

    discovered, stats = universe.discover_exhaustive(cfg, fetcher=fake)
    assert len(discovered) == 1234
    assert stats["discovery_exhaustive"] is True
    assert stats["pagination_loop_guard_hit"] is False
    assert seen_cursors == [*range(0, 1234, 100), 1234]


def test_discovery_does_not_treat_a_venue_page_cap_as_exhaustion():
    cfg = config()
    rows = [market(index) for index in range(250)]
    seen_cursors = []

    def venue_capped_at_one_hundred(url, timeout):
        del timeout
        query = parse_qs(urlparse(url).query)
        offset = int(query.get("after_cursor", ["0"])[0])
        requested = int(query["limit"][0])
        seen_cursors.append(offset)
        page = rows[offset:offset + min(requested, 100)]
        return {"markets": page, "next_cursor": str(offset + len(page)) if page else ""}

    discovered, stats = universe.discover_exhaustive(cfg, fetcher=venue_capped_at_one_hundred)
    assert len(discovered) == 250
    assert stats["discovery_exhaustive"] is True
    assert seen_cursors == [0, 100, 200, 250]


def test_discovery_fails_closed_on_repeated_keyset_cursor():
    cfg = config()

    def stuck(_url, _timeout):
        return {"markets": [market(1)], "next_cursor": "same"}

    discovered, stats = universe.discover_exhaustive(cfg, fetcher=stuck)
    assert len(discovered) == 1
    assert stats["discovery_exhaustive"] is False
    assert stats["pagination_loop_guard_hit"] is True


def test_normalization_preserves_public_price_and_spread_signals():
    normalized = universe.normalize_market(market(1))
    assert normalized is not None
    assert normalized["outcome_prices"] == [0.45, 0.55]
    assert normalized["best_bid"] == 0.44
    assert normalized["best_ask"] == 0.46
    assert normalized["midpoint"] == 0.45
    assert normalized["spread"] == 0.02
    assert normalized["last_trade_price"] == 0.45
    assert normalized["event_start_time"] == "2026-08-29T16:30:00Z"
    assert normalized["timed_sports"] is False
    assert normalized["sports_market_type"] == ""
    assert normalized["game_start_time"] == ""
    assert normalized["seconds_delay"] == 0
    assert normalized["fee_schedule"]["rate"] == 0.07


def test_normalization_preserves_nested_timed_sports_authority_facts():
    normalized = universe.normalize_market(market(
        2,
        events=[{
            "id": "e1", "sportsMarketType": "moneyline",
            "gameStartTime": "2026-08-30T00:15:00Z", "secondsDelay": 3,
        }],
    ))
    assert normalized is not None
    assert normalized["timed_sports"] is True
    assert normalized["sports_market_type"] == "moneyline"
    assert normalized["game_start_time"] == "2026-08-30T00:15:00Z"
    assert normalized["seconds_delay"] == 3


def test_tiers_are_resource_derived_and_cover_every_eligible_market():
    cfg = config()
    cfg["resource_budget"]["hot"].update({
        "websocket_asset_capacity": 8, "assets_per_market": 2,
        "memory_budget_bytes": 100, "estimated_bytes_per_market": 20,
        "cpu_budget_micros_per_second": 100, "estimated_update_rate_hz_per_market": 2,
        "estimated_cpu_micros_per_update": 10,
    })
    cfg["resource_budget"]["warm"].update({
        "scan_time_budget_millis": 9, "estimated_scan_millis_per_market": 2,
        "memory_budget_bytes": 100, "estimated_bytes_per_market": 10,
    })
    normalized = [universe.normalize_market(market(index)) for index in range(12)]
    snapshot = universe.build_snapshot(normalized, {"discovery_exhaustive": True}, cfg, model_sha=SHA, timestamp_ms=1)
    assert snapshot["tier_counts"] == {"HOT": 4, "WARM": 4, "COLD": 4}
    tier_ids = sum(snapshot["tiers"].values(), [])
    assert len(tier_ids) == len(set(tier_ids)) == snapshot["eligible_markets"] == 12
    assert snapshot["resource_capacities"]["hot_limiting_dimensions"] == ["websocket_assets"]
    assert snapshot["resource_capacities"]["warm_limiting_dimensions"] == ["scan_time"]


def test_skip_reasons_are_explicit_and_safety_is_fail_closed():
    cfg = config()
    raw = [
        market(1), market(2, acceptingOrders=False), market(3, liquidityNum=0),
        market(4, conditionId=""), market(5, clobTokenIds=[]),
    ]
    normalized = [universe.normalize_market(row) for row in raw]
    snapshot = universe.build_snapshot(normalized, {"discovery_exhaustive": True}, cfg, model_sha=SHA, timestamp_ms=1)
    assert snapshot["eligible_markets"] == 1
    assert snapshot["skipped_by_reason"] == {
        "BELOW_MINIMUM_LIQUIDITY": 1, "MISSING_CLOB_TOKENS": 1,
        "MISSING_CONDITION_ID": 1, "NOT_ACCEPTING_ORDERS": 1,
    }
    assert snapshot["paper_only"] is True
    assert snapshot["authenticated_execution"] is False
    assert snapshot["real_order_submission"] is False
    assert snapshot["execution_authority"] is False


def test_prior_tier_hysteresis_is_deterministic():
    cfg = config()
    cfg["resource_budget"]["hot"]["websocket_asset_capacity"] = 2
    cfg["resource_budget"]["hot"]["memory_budget_bytes"] = 2 * 1024 * 1024
    cfg["resource_budget"]["hot"]["cpu_budget_micros_per_second"] = 600
    normalized = [universe.normalize_market(market(1, liquidityNum=100, volume24hr=100)), universe.normalize_market(market(2, liquidityNum=100, volume24hr=100))]
    previous = {"markets": [{"market_id": "2", "tier": "HOT"}]}
    first = universe.build_snapshot(normalized, {"discovery_exhaustive": True}, cfg, model_sha=SHA, timestamp_ms=1, previous=previous)
    second = universe.build_snapshot(normalized, {"discovery_exhaustive": True}, cfg, model_sha=SHA, timestamp_ms=2, previous=previous)
    assert first["tiers"]["HOT"] == ["2"]
    assert first["tiers"] == second["tiers"]


def test_persist_is_atomic_and_change_tape_is_append_only(tmp_path):
    cfg = config()
    normalized = [universe.normalize_market(market(1))]
    snapshot = universe.build_snapshot(normalized, {"discovery_exhaustive": True}, cfg, model_sha=SHA, timestamp_ms=1)
    universe.persist(tmp_path, snapshot, {})
    universe.persist(tmp_path, snapshot, snapshot)
    assert json.loads((tmp_path / "current.json").read_text())["membership_sha256"] == snapshot["membership_sha256"]
    assert json.loads((tmp_path / "status.json").read_text())["state"] == "OPERATIONAL"
    assert len((tmp_path / "changes.jsonl").read_text().splitlines()) == 1


def test_configuration_contract_is_valid():
    cfg = config()
    universe.validate_config(cfg)
    assert cfg["paper_only"] is True
    assert cfg["authenticated_execution"] is False
    assert cfg["real_order_submission"] is False


if __name__ == "__main__":
    import tempfile

    test_discovery_pages_to_venue_exhaustion_beyond_one_thousand()
    test_discovery_does_not_treat_a_venue_page_cap_as_exhaustion()
    test_discovery_fails_closed_on_repeated_keyset_cursor()
    test_normalization_preserves_public_price_and_spread_signals()
    test_normalization_preserves_nested_timed_sports_authority_facts()
    test_tiers_are_resource_derived_and_cover_every_eligible_market()
    test_skip_reasons_are_explicit_and_safety_is_fail_closed()
    test_prior_tier_hysteresis_is_deterministic()
    with tempfile.TemporaryDirectory() as directory:
        test_persist_is_atomic_and_change_tape_is_append_only(Path(directory))
    test_configuration_contract_is_valid()
