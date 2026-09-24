from __future__ import annotations

from pathlib import Path
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import v7_exact_arb_warm_scan as warm
from v7_unified_exact_arb_graph import SAFETY, generation_hash


MODEL = "b" * 40


def _leg(token: str, market: str) -> dict:
    return {
        "token_id": token,
        "market_id": market,
        "coefficient": "1",
        "minimum_order": "0",
        "fee_rate": "0",
        "fee_exponent": "1",
        "fee_rounding_mode": "VENUE_5DP",
        "fee_rounding_increment": "0.00001",
        "tick_size": "0.01",
    }


def _relation(rid: str, family: str, tokens: list[str], *, directions=None) -> dict:
    return {
        "relation_id": rid,
        "relation_family": family,
        "relation_type": "CONSTANT_PAYOUT_EQUALITY",
        "enabled": True,
        "guaranteed_payout": "1",
        "reserve_per_unit": "0.01",
        "directions": directions or ["BUY_BASKET"],
        "legs": [_leg(token, f"m{i}") for i, token in enumerate(tokens)],
    }


def _book(ask: float, bid: float, size: float = 10.0) -> dict:
    return {"asks": [(ask, size)], "bids": [(bid, size)]}


def test_warm_screen_finds_apparent_n_leg_edge_but_never_marks_actionable() -> None:
    relation = _relation("n3", "NEGRISK_COMPLETE_SET", ["a", "b", "c"])
    books = {token: _book(.30, .29) for token in ("a", "b", "c")}
    result = warm.screen_relation(relation, books, warm.frac("100"))
    row = result["directions"]["BUY"]
    assert result["evidence_quality"] == warm.EVIDENCE
    assert result["actionable"] is False
    assert row["actionable"] is False
    assert row["requires_causal_confirmation"] is True
    assert warm.frac(row["distance_to_raw_arbitrage"]) < 0
    assert warm.frac(row["distance_to_after_reserve_arbitrage"]) < 0
    assert warm.frac(row["quantity_apparent"]) == 10


def test_warm_sell_screen_is_explicitly_inventory_required() -> None:
    relation = _relation(
        "sell3", "NEGRISK_COMPLETE_SET", ["a", "b", "c"],
        directions=["SELL_INVENTORY_BASKET"],
    )
    books = {token: _book(.41, .40) for token in ("a", "b", "c")}
    result = warm.screen_relation(relation, books, warm.frac("100"))
    row = result["directions"]["SELL"]
    assert row["inventory_required"] is True
    assert row["actionable"] is False
    assert warm.frac(row["distance_to_raw_arbitrage"]) < 0


def test_relation_selection_prioritizes_nonbinary_families() -> None:
    relations = [
        _relation("b1", "SAME_MARKET_BINARY_COMPLETE_SET", ["a", "b"]),
        _relation("n1", "NEGRISK_COMPLETE_SET", ["c", "d", "e"]),
        _relation("b2", "SAME_MARKET_BINARY_COMPLETE_SET", ["f", "g"]),
    ]
    selected, tokens = warm.select_relations(relations, cursor=0, max_tokens=3)
    assert selected == [1]
    assert tokens == ["c", "d", "e"]


def test_warm_scan_status_never_exposes_actionable_candidates(tmp_path: Path, monkeypatch) -> None:
    relation = _relation("n3", "NEGRISK_COMPLETE_SET", ["a", "b", "c"])
    graph = {
        "schema": "polymarket_v7_unified_exact_arb_graph_v1",
        "version": 1,
        **SAFETY,
        "model_sha": MODEL,
        "source_universe_timestamp_ms": warm.time.time_ns() // 1_000_000,
        "source_universe_valid": True,
        "source_universe_membership_sha256": "c" * 64,
        "nodes": [],
        "relations": [relation],
        "dependency_index": {"a": [0], "b": [0], "c": [0]},
        "proof_registry": {},
        "rejected_relations": [],
        "unverified_candidates": [],
        "component_provenance": [],
        "metadata": {"compiled_at_ms": 1},
        "transformation_registry": {},
        "settlement_dependency_index": {},
        "resource_dependency_index": {},
    }
    graph["graph_generation"] = generation_hash(graph)
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph))

    def fake_fetch(base, tokens, timeout, **kwargs):
        return {
            token: {
                "asset_id": token,
                "asks": [{"price": ".30", "size": "10"}],
                "bids": [{"price": ".29", "size": "10"}],
            }
            for token in tokens
        }

    monkeypatch.setattr(warm, "fetch_books", fake_fetch)
    args = SimpleNamespace(
        graph=path,
        model_sha=MODEL,
        max_tokens_per_cycle=50,
        clob_url="https://clob.example",
        timeout_seconds=1.0,
        chunk_size=50,
        maximum_relation_units="100",
        hotset_relations=10,
    )
    status, hotset, _ = warm.scan_once(args, 0)
    assert status["after_reserve_positive_screen_only"] == 1
    assert status["actionable_candidates"] == 0
    assert status["evidence_quality"] == warm.EVIDENCE
    assert hotset["actionable"] is False
    assert hotset["relations"][0]["requires_causal_confirmation"] is True
