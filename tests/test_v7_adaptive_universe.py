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
        "volume24hr": index, "active": True, "closed": False, "acceptingOrders": True,
        "events": [{"id": f"e{index // 2}"}],
    }
    row.update(overrides)
    return row


def test_discovery_pages_to_venue_exhaustion_beyond_one_thousand():
    cfg = config()
    rows = [market(index) for index in range(1234)]
    seen_offsets = []

    def fake(url, timeout):
        del timeout
        query = parse_qs(urlparse(url).query)
        offset = int(query["offset"][0])
        limit = int(query["limit"][0])
        seen_offsets.append(offset)
        return rows[offset:offset + limit]

    discovered, stats = universe.discover_exhaustive(cfg, fetcher=fake)
    assert len(discovered) == 1234
    assert stats["discovery_exhaustive"] is True
    assert stats["pagination_loop_guard_hit"] is False
    assert seen_offsets == [0, 500, 1000]


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
