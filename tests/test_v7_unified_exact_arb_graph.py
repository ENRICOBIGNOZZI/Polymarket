from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_unified_exact_arb_graph import SAFETY, compile_graph, evaluate


def test_compiles_statewise_proven_basket_and_sizes_depth() -> None:
    model, semantic = "a" * 40, "b" * 64
    universe = {**SAFETY, "model_sha": model, "markets": [{
        "market_id":"m", "event_id":"e", "active":True, "closed":False,
        "asset":"BTC", "horizon":"M5", "contract_family":"binary",
        "settlement_semantic_hash":semantic, "window_start_unix":1, "close_timestamp_unix":2,
        "clob_token_ids":["yes", "no"], "outcomes":["YES", "NO"]}]}
    registry = {**SAFETY, "schema":"polymarket_v7_exact_arb_relation_registry_v1", "version":1,
        "relations":[{"id":"binary", "enabled":True, "states":["Y", "N"], "guaranteed_payout":1,
        "legs":[{"selector":{"market_id":"m"}, "outcome":"YES", "coefficient":1, "payout_vector":[1,0]},
                {"selector":{"market_id":"m"}, "outcome":"NO", "coefficient":1, "payout_vector":[0,1]}]}]}
    graph = compile_graph([registry], universe, model)
    assert graph["dependency_index"] == {"no":[0], "yes":[0]}
    books = {token:{"timestamp_ms":10,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                    "asks":[[".48","2"],[".49","8"]]} for token in ("yes", "no")}
    result = evaluate(graph["relations"][0], books, 10)
    assert result["accepted"] is True and result["quantity"] == "10"


def test_truncated_or_unproven_relation_is_not_actionable() -> None:
    # The evaluator specifically refuses a depth snapshot that cannot prove capacity.
    assert evaluate({"enabled":True,"legs":[{"token_id":"x","coefficient":"1"}],"guaranteed_payout":"1"},
                    {"x":{"lineage_continuous":True,"depth_truncated":True}}, 1)["reason"] == "truncated_depth"
