from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from v7_exact_arb_exchange_universe import SAFETY, build_snapshot
from v7_exact_arb_hotset_selection import EVIDENCE, HOTSET_SCHEMA, SCHEMA, compile_selection
from v7_unified_exact_arb_graph import GraphError, compile_graph


MODEL = "c" * 40


def _raw_market(mid: str, *, neg: bool = False) -> dict:
    base = int(mid)
    return {
        "id": mid,
        "conditionId": "0x" + format(base, "x")[-1] * 64,
        "question": f"Q{mid}",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": f'["{1000+base}","{2000+base}"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "negRisk": neg,
        "orderPriceMinTickSize": .01,
        "orderMinSize": 5,
        "feesEnabled": False,
        "feeSchedule": {"rate": 0, "exponent": 1},
        "startDate": "2026-09-23T00:00:00Z",
        "endDate": "2026-09-24T00:00:00Z",
    }


def _event(markets: list[dict], *, neg: bool = False) -> dict:
    return {
        "id": "99",
        "active": True,
        "closed": False,
        "negRisk": neg,
        "enableNegRisk": neg,
        "negRiskAugmented": False,
        "markets": markets,
    }


def _registry() -> dict:
    return {
        **SAFETY,
        "schema": "polymarket_v7_exact_arb_relation_registry_v1",
        "version": 1,
        "relations": [],
    }





def _verified_n_way_registry() -> dict:
    states = ["A", "B", "C"]
    legs = []
    for index, mid in enumerate(("1", "2", "3")):
        vector = [0, 0, 0]
        vector[index] = 1
        legs.append({
            "selector": {"market_id": mid},
            "outcome": "YES",
            "coefficient": 1,
            "payout_vector": vector,
        })
    return {
        **SAFETY,
        "schema": "polymarket_v7_exact_arb_relation_registry_v1",
        "version": 1,
        "relations": [{
            "id": "explicit-n-way-test",
            "enabled": True,
            "relation_family": "EXPLICIT_VERIFIED_N_WAY",
            "states": states,
            "guaranteed_payout": 1,
            "legs": legs,
            "directions": ["BUY_BASKET"],
            "discovery": "EXPLICIT_VERIFIED_TEST",
        }],
    }


def _hotset(graph: dict, relation_ids: list[str]) -> dict:
    return {
        "schema": HOTSET_SCHEMA,
        **SAFETY,
        "execution_authority": False,
        "model_sha": MODEL,
        "graph_generation": graph["graph_generation"],
        "evidence_quality": EVIDENCE,
        "actionable": False,
        "selection_purpose": "CAUSAL_HOT_OBSERVATION_PRIORITY_ONLY",
        "relations": [{"relation_id": rid} for rid in relation_ids],
        "tokens": [],
    }


def test_binary_hotset_compiles_to_exact_causal_observer_selection() -> None:
    universe = build_snapshot([_event([_raw_market("1")])], MODEL, generated_at_ms=1)
    graph = compile_graph([_registry()], universe, MODEL)
    selection, status = compile_selection(
        graph, universe, _hotset(graph, [graph["relations"][0]["relation_id"]]), MODEL, 64
    )
    assert selection["schema"] == SCHEMA
    assert selection["selection_only"] is True
    assert selection["execution_authority"] is False
    assert selection["source_actionable"] is False
    assert selection["graph_generation"] == graph["graph_generation"]
    assert len(selection["markets"]) == 1
    row = selection["markets"][0]
    assert row["yes_token"] == "1001" and row["no_token"] == "2001"
    assert row["start_timestamp_ms"] > 0 and row["end_timestamp_ms"] > row["start_timestamp_ms"]
    assert status["state"] == "READY" and status["selected_tokens"] == 2


def test_live_selection_requires_fresh_sources_and_carries_bounded_lease() -> None:
    import pytest
    universe = build_snapshot([_event([_raw_market("1")])], MODEL, generated_at_ms=100)
    graph = compile_graph([_registry()], universe, MODEL)
    hotset = {**_hotset(graph, [graph["relations"][0]["relation_id"]]), "timestamp_ms": 101}
    selection, _ = compile_selection(graph, universe, hotset, MODEL, 64, as_of_ms=102)
    assert selection["source_valid"] is True
    assert selection["valid_until_ms"] == 120101
    with pytest.raises(ValueError, match="hotset_expired"):
        compile_selection(graph, universe, hotset, MODEL, 64, as_of_ms=120102)
    universe["source_valid"] = False
    with pytest.raises(ValueError, match="universe_source_invalid"):
        compile_selection(graph, universe, hotset, MODEL, 64, as_of_ms=102)


def test_verified_n_leg_relation_selects_every_binary_member_pair_for_causal_observation() -> None:
    universe = build_snapshot(
        [_event([_raw_market(str(i)) for i in (1, 2, 3)])],
        MODEL, generated_at_ms=1,
    )
    graph = compile_graph([_verified_n_way_registry()], universe, MODEL)
    relation = next(row for row in graph["relations"] if row["relation_family"] == "EXPLICIT_VERIFIED_N_WAY")
    selection, status = compile_selection(
        graph, universe, _hotset(graph, [relation["relation_id"]]), MODEL, 64
    )
    assert status["selected_relations"] == 1
    assert status["selected_markets"] == 3
    assert status["selected_tokens"] == 6
    assert {row["market_id"] for row in selection["markets"]} == {"1", "2", "3"}


def test_relation_is_dropped_if_complete_causal_membership_exceeds_bound() -> None:
    universe = build_snapshot(
        [_event([_raw_market(str(i)) for i in (1, 2, 3)])],
        MODEL, generated_at_ms=1,
    )
    graph = compile_graph([_verified_n_way_registry()], universe, MODEL)
    relation = next(row for row in graph["relations"] if row["relation_family"] == "EXPLICIT_VERIFIED_N_WAY")
    selection, status = compile_selection(
        graph, universe, _hotset(graph, [relation["relation_id"]]), MODEL, 2
    )
    assert selection["markets"] == []
    assert status["state"] == "EMPTY"
    assert status["rejection_reasons"]["hotset_market_capacity"] == 1


def test_hotset_generation_mismatch_fails_closed() -> None:
    universe = build_snapshot([_event([_raw_market("1")])], MODEL, generated_at_ms=1)
    graph = compile_graph([_registry()], universe, MODEL)
    hotset = _hotset(graph, [graph["relations"][0]["relation_id"]])
    hotset["graph_generation"] = "d" * 64
    try:
        compile_selection(graph, universe, hotset, MODEL, 64)
    except GraphError as exc:
        assert str(exc) == "hotset_generation"
    else:
        raise AssertionError("stale hotset generation accepted")
