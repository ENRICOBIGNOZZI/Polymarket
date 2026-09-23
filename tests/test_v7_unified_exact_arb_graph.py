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


def test_machine_attested_binary_partition_is_auto_compiled() -> None:
    model, semantic = "d" * 40, "e" * 64
    universe={**SAFETY,"model_sha":model,"markets":[{"market_id":"m","condition_id":"c","active":True,"closed":False,
        "binary_partition_verified":True,"asset":"BTC","horizon":"M5","contract_family":"binary","settlement_semantic_hash":semantic,
        "clob_token_ids":["y","n"],"outcomes":["YES","NO"]}]}
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","version":1,"relations":[]}
    graph=compile_graph([registry],universe,model)
    assert graph["relations"][0]["relation_family"]=="SAME_MARKET_BINARY_COMPLETE_SET"


def test_proven_inequality_is_retained_but_never_actionable() -> None:
    model, semantic="f"*40,"a"*64
    universe={**SAFETY,"model_sha":model,"markets":[{"market_id":"m","active":True,"closed":False,"asset":"BTC","horizon":"M5","contract_family":"binary","settlement_semantic_hash":semantic,"clob_token_ids":["y","n"],"outcomes":["YES","NO"]}]}
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","version":1,"relations":[{"id":"upper","enabled":True,"relation_type":"PAYOFF_UPPER_BOUND","states":["a","b"],"guaranteed_payout":1,"legs":[{"selector":{"market_id":"m"},"outcome":"YES","payout_vector":[1,0]}]}]}
    relation=compile_graph([registry],universe,model)["relations"][0]
    assert relation["proof_type"]=="FINITE_STATE_EXACT_RATIONAL_INEQUALITY" and relation["enabled"] is False


def test_minimum_order_and_capital_are_fail_closed() -> None:
    relation={"enabled":True,"guaranteed_payout":"1","reserve_per_unit":"0","legs":[{"token_id":"a","coefficient":"1","minimum_order":"10"}]}
    books={"a":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0","asks":[[".5","5"]]}}
    assert evaluate(relation,books,1)["reason"]=="minimum_order_or_capital"
    relation["legs"][0]["minimum_order"]="0";books["a"]["asks"]=[[".5","10"]]
    assert evaluate(relation,books,1,capital_limit="1")["reason"]=="minimum_order_or_capital"
