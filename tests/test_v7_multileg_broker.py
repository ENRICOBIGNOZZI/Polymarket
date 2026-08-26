#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_graph_queue_decoupled_sizing as sizing
import v7_multileg_broker as broker


def blank_broker() -> broker.Broker:
    value = broker.Broker.__new__(broker.Broker)
    value.cash = 1000.0
    value.bundles = {}
    value.legs = []
    value.clob = "https://clob.invalid"
    value.slippage_bps = 5.0
    value.adverse_horizon_seconds = 45
    value.killed = False
    value.event = lambda *args, **kwargs: None
    value.abort = lambda bundle_id, reason: None
    value.market = lambda market_id, force=False: {
        "id": market_id,
        "conditionId": "c-" + market_id,
        "feesEnabled": False,
        "active": True,
        "closed": False,
        "events": [{"id": "event"}],
        "clobTokenIds": ["t", "n"],
        "outcomes": ["Yes", "No"],
    }
    return value


def test_market_event_identity_never_falls_back_to_condition_id():
    assert broker.market_event_id({"conditionId": "condition-only"}) == ""
    assert broker.market_event_id({"eventId": "42", "conditionId": "x"}) == "42"
    assert broker.market_event_id({"events": [{"id": 77}], "eventId": "42"}) == "77"


def test_one_public_trade_capacity_is_conserved_across_own_orders():
    value = blank_broker()
    value.bundles = {
        "a": broker.Bundle("a", "GRAPH_RV", "event", "RESTING", 0, 0.01, 10, 9999999999, 9999999999),
        "b": broker.Bundle("b", "GRAPH_RV", "event", "RESTING", 0, 0.01, 10, 9999999999, 9999999999),
    }
    value.legs = [
        broker.Leg("a", "m1", "event", "YES", "t", 1.0, 10.0, 0.0, 0.40, 0.0, 1000, 1000),
        broker.Leg("b", "m2", "event", "YES", "t", 1.0, 10.0, 0.0, 0.40, 0.0, 1100, 1100),
    ]
    value.apply_trades([{"side": "SELL", "size": 15.0, "asset_id": "t", "price": 0.39, "received_ms": 2000, "event_ts_ms": 2000}])
    assert abs(sum(leg.filled_shares for leg in value.legs) - 15.0) < 1e-12
    assert abs(value.legs[0].filled_shares - 10.0) < 1e-12
    assert abs(value.legs[1].filled_shares - 5.0) < 1e-12


def test_backfilled_pre_order_event_cannot_fill_new_order():
    value = blank_broker()
    value.bundles = {"a": broker.Bundle("a", "GRAPH_RV", "event", "RESTING", 0, 0.01, 10, 9999999999, 9999999999)}
    value.legs = [broker.Leg("a", "m1", "event", "YES", "t", 1.0, 10.0, 0.0, 0.40, 0.0, 1000, 1000)]
    value.apply_trades([{"side": "SELL", "size": 100.0, "asset_id": "t", "price": 0.39, "received_ms": 2000, "event_ts_ms": 900}])
    assert value.legs[0].filled_shares == 0.0


def test_complete_bundle_enters_settling_instead_of_abort_on_async_close():
    value = blank_broker()
    value.run_dir = Path("/tmp/unused-v7-broker-test")
    value.bundles = {"a": broker.Bundle("a", "GRAPH_RV", "event", "COMPLETE", 0, 0.01, 10, 9999999999, 9999999999)}
    value.legs = [
        broker.Leg("a", "m1", "event", "YES", "t1", 1.0, 10.0, 10.0, 0.40, 0.0, 1000, 1000, entry_cash=4.0, order_state="FILLED"),
        broker.Leg("a", "m2", "event", "YES", "t2", 1.0, 10.0, 10.0, 0.50, 0.0, 1000, 1000, entry_cash=5.0, order_state="FILLED"),
    ]
    raws = {
        "m1": {"id": "m1", "conditionId": "c1", "feesEnabled": False, "closed": True, "outcomes": ["Yes", "No"], "outcomePrices": [1, 0], "events": [{"id": "event"}]},
        "m2": {"id": "m2", "conditionId": "c2", "feesEnabled": False, "closed": False, "events": [{"id": "event"}]},
    }
    value.market = lambda market_id, force=False: raws[market_id]
    value.write_ledger = lambda bundle_id: None
    value.manage({})
    assert value.bundles["a"].status == "SETTLING"
    assert value.bundles["a"].abort_reason == ""
    assert value.legs[0].exited is True
    assert value.legs[1].exited is False


def test_v7_runtime_routes_to_coordinated_python_broker_not_legacy_cpp():
    source = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "v7_multileg_broker_runner.py" in source
    assert "--capacity-lock" in source
    assert "polymarket_multileg_paper" not in source
    broker_source = (ROOT / "scripts" / "v7_multileg_broker.py").read_text(encoding="utf-8")
    runner_source = (ROOT / "scripts" / "v7_multileg_broker_runner.py").read_text(encoding="utf-8")
    assert 'bundle.status = "SETTLING"' in broker_source
    assert 'risk_event = market_event_id(raw)' in broker_source
    assert 'trade["received_ms"] > leg.arrival_ms' in broker_source
    assert 'trade["event_ts_ms"] > leg.arrival_event_ms' in broker_source
    assert "self.persist()" in runner_source
    assert "fcntl.LOCK_UN" in runner_source
    assert runner_source.index("self.persist()") < runner_source.index("fcntl.LOCK_UN")


def test_current_v7_graph_capacity_increases_mechanically_with_queue_ahead():
    low_queue = sizing.incumbent_queue_coupled_units(risk_units=1000.0, queue_ahead=20.0, weight=1.0)
    deep_queue = sizing.incumbent_queue_coupled_units(risk_units=1000.0, queue_ahead=1000.0, weight=1.0)
    assert low_queue == 5.0
    assert deep_queue == 250.0
    assert deep_queue > low_queue


def test_queue_decoupled_challenger_is_invariant_to_queue_when_unwind_depth_is_fixed():
    low_queue = sizing.compare_capacity(risk_units=1000.0, queue_ahead=20.0, unwind_depth_shares=80.0)
    deep_queue = sizing.compare_capacity(risk_units=1000.0, queue_ahead=1000.0, unwind_depth_shares=80.0)
    assert low_queue.challenger_units == 20.0
    assert deep_queue.challenger_units == 20.0
    assert low_queue.incumbent_units == 5.0
    assert deep_queue.incumbent_units == 250.0


def test_queue_decoupled_challenger_capacity_tracks_executable_unwind_depth():
    thin = sizing.queue_decoupled_units(risk_units=1000.0, unwind_depth_shares=20.0, weight=1.0)
    deep = sizing.queue_decoupled_units(risk_units=1000.0, unwind_depth_shares=200.0, weight=1.0)
    assert thin == 5.0
    assert deep == 50.0


def test_source_contract_exposes_queue_coupled_capacity_as_research_blocker():
    broker_source = (ROOT / "scripts" / "v7_multileg_broker.py").read_text(encoding="utf-8")
    assert 'units = min(units, 0.25 * max(1.0, books[token].queue_at(limits[index])) / max(weight, 1e-12))' in broker_source


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} v7 multileg broker tests")
