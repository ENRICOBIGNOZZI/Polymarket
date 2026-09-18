from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts" / "v7_native_engine_supervisor.py"
spec = importlib.util.spec_from_file_location("native_supervisor", PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SHA = "a" * 40


def fixtures(now_ms: int):
    end = datetime.fromtimestamp((now_ms + 60_000) / 1000, tz=timezone.utc).isoformat()
    market = {
        "market_id": "m1", "asset": "BTC", "horizon": "M5",
        "active": True, "closed": False, "accepting_orders": True,
        "clob_token_ids": ["yes", "no"], "event_ids": ["e1"],
        "end_date": end,
    }
    universe = {
        "schema": "polymarket_v7_crypto_universe_snapshot_v1",
        "model_sha": SHA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "markets": [market],
    }
    external = {
        "schema": "polymarket_v7_external_fair_status_v1",
        "code_sha": SHA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "market": {
            "market_id": "m1", "active": True, "closed": False,
            "accepting_orders": True, "yes_token": "yes", "no_token": "no",
        },
        "contract": {"verified": True, "rules_hash_recognized": True, "rules_hash": "rules"},
        "settlement_reference": {"valid": True, "version": 7},
        "oracle": {"healthy": True},
        "external": {"healthy": True},
    }
    registry = {
        "schema": "polymarket_v7_fee_reward_registry_v1",
        "model_sha": SHA, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "markets": [{
            "market_id": "m1", "executable_under_registry": True,
            "fee": {
                "verified": True, "rate": 0.02, "exponent": 1.0,
                "source": "gamma:feeSchedule",
                "observed_at_ms": now_ms - 1000,
                "expires_at_ms": now_ms + 60_000,
            },
        }],
    }
    return universe, external, registry


def test_executable_market_requires_same_verified_identity() -> None:
    now = 2_000_000_000_000
    universe, external, registry = fixtures(now)
    value, reason = module.executable_spec(
        universe, external, registry, sha=SHA, now_ms=now)
    assert reason == "READY"
    assert value is not None
    assert value["market_id"] == "m1"
    assert value["yes_token"] == "yes"
    assert value["no_token"] == "no"
    assert value["fee_rate"] == 0.02


def test_executable_market_fails_closed_on_each_authority_gap() -> None:
    now = 2_000_000_000_000
    mutations = [
        ("universe", lambda u, e, r: u.update(model_sha="b" * 40)),
        ("contract", lambda u, e, r: e["contract"].update(verified=False)),
        ("reference", lambda u, e, r: e["settlement_reference"].update(valid=False)),
        ("venue", lambda u, e, r: e["external"].update(healthy=False)),
        ("token", lambda u, e, r: e["market"].update(yes_token="wrong")),
        ("fee", lambda u, e, r: r["markets"][0]["fee"].update(verified=False)),
        ("stale_fee", lambda u, e, r: r["markets"][0]["fee"].update(expires_at_ms=now - 1)),
    ]
    for _, mutate in mutations:
        universe, external, registry = fixtures(now)
        mutate(universe, external, registry)
        value, reason = module.executable_spec(
            universe, external, registry, sha=SHA, now_ms=now)
        assert value is None
        assert reason != "READY"


def test_market_too_close_to_end_is_not_started() -> None:
    now = 2_000_000_000_000
    universe, external, registry = fixtures(now)
    universe["markets"][0]["end_date"] = datetime.fromtimestamp(
        (now + 4_000) / 1000, tz=timezone.utc).isoformat()
    value, reason = module.executable_spec(
        universe, external, registry, sha=SHA, now_ms=now)
    assert value is None
    assert reason == "MARKET_TOO_CLOSE_TO_END"
