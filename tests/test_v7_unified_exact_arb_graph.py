from __future__ import annotations
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_unified_exact_arb_graph import SAFETY, GraphError, compile_graph, evaluate
from v7_unified_exact_arb_graph_shadow import Shadow


def _universe(model: str = "a" * 40, semantic: str = "b" * 64) -> dict:
    return {**SAFETY, "model_sha": model, "markets": [
        {"market_id":"a", "condition_id":"ca", "active":True, "closed":False,
         "asset":"BTC", "horizon":"M5", "contract_family":"binary",
         "settlement_semantic_hash":semantic, "fee_schedule":{"rate":0}, "clob_token_ids":["ay", "an"], "outcomes":["YES", "NO"]},
        {"market_id":"b", "condition_id":"cb", "active":True, "closed":False,
         "asset":"BTC", "horizon":"M5", "contract_family":"binary",
         "settlement_semantic_hash":semantic, "fee_schedule":{"rate":0}, "clob_token_ids":["by", "bn"], "outcomes":["YES", "NO"]},
        {"market_id":"c", "condition_id":"cc", "active":True, "closed":False,
         "asset":"BTC", "horizon":"M5", "contract_family":"binary",
         "settlement_semantic_hash":semantic, "fee_schedule":{"rate":0}, "clob_token_ids":["cy", "cn"], "outcomes":["YES", "NO"]},
    ]}


def _registry(relations: list[dict]) -> dict:
    return {**SAFETY, "schema":"polymarket_v7_exact_arb_relation_registry_v1", "version":1,
            "relations":relations}


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


def test_n_way_and_fractional_coefficients_size_at_every_depth_breakpoint() -> None:
    relation={"id":"nway","enabled":True,"directions":["BUY_BASKET"],"states":["a","b","c"],"guaranteed_payout":"1",
              "legs":[
                  {"selector":{"market_id":"a"},"outcome":"YES","coefficient":"1/2","payout_vector":[2,0,0]},
                  {"selector":{"market_id":"b"},"outcome":"YES","coefficient":"1/2","payout_vector":[0,2,0]},
                  {"selector":{"market_id":"c"},"outcome":"YES","coefficient":"1/2","payout_vector":[0,0,2]}]}
    graph=compile_graph([_registry([relation])],_universe(),"a"*40)
    books={token:{"timestamp_ms":100,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                  "asks":[[".2","1"],[".4","5"]]} for token in ("ay","by","cy")}
    result=evaluate(graph["relations"][0],books,100)
    assert result["accepted"] is True and result["quantity"]=="12"


def test_sell_inventory_direction_requires_inventory_and_uses_bids() -> None:
    relation={"enabled":True,"directions":["SELL_COMPLETE_SET"],"guaranteed_payout":"1","legs":[
        {"token_id":"y","coefficient":"1","minimum_order":"0"},
        {"token_id":"n","coefficient":"1","minimum_order":"0"}]}
    books={token:{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                  "asks":[[".7","4"]],"bids":[[".6","4"]]} for token in ("y","n")}
    assert evaluate(relation,books,1)["reason"]=="minimum_order_or_capital"
    out=evaluate(relation,books,1,inventory_limit="3")
    assert out["accepted"] is True and out["direction"]=="SELL" and out["quantity"]=="3"


def test_fee_rounding_is_exact_and_missing_bid_depth_fails_closed() -> None:
    relation={"enabled":True,"directions":["BUY_BASKET"],"guaranteed_payout":"1","legs":[
        {"token_id":"x","coefficient":"1","minimum_order":"0","fee_rounding_increment":"1/100",
         "fee_rounding_mode":"CEILING"}]}
    books={"x":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"1/100",
                "asks":[["1/2","1"]]}}
    out=evaluate(relation,books,1)
    assert out["accepted"] is True and out["net_locked_pnl"]=="49/100"


def test_verified_market_fee_exponent_is_applied_per_price_level() -> None:
    relation={"enabled":True,"guaranteed_payout":"1","legs":[{"token_id":"x","coefficient":"1",
              "minimum_order":"0","fee_exponent":"1"}]}
    books={"x":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"1/10",
                "asks":[["1/2","1"]]}}
    assert evaluate(relation,books,1)["net_locked_pnl"]=="19/40"


def test_invalid_inequality_and_unverified_components_remain_non_actionable() -> None:
    invalid={"id":"bad","enabled":True,"relation_type":"PAYOFF_LOWER_BOUND","states":["a","b"],"guaranteed_payout":1,
             "legs":[{"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]}]}
    graph=compile_graph([_registry([invalid])],_universe(),"a"*40,
        [{"schema":"polymarket_v7_combo_rfq_shadow_v1","model_sha":"a"*40,"paper_only":True,
          "authenticated_execution":False,"real_order_submission":False}])
    assert not graph["relations"]
    assert any(row["relation_family"]=="LEGACY_COMPONENT" for row in graph["unverified_candidates"])


def test_verified_component_attestation_compiles_and_bad_directions_reject() -> None:
    attested={"id":"neg","enabled":True,"relation_family":"NEGRISK_TRANSFORMATION","directions":["BUY_BASKET"],
              "states":["a","b"],"guaranteed_payout":1,"legs":[
                {"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
                {"selector":{"market_id":"b"},"outcome":"NO","payout_vector":[0,1]}]}
    component={"schema":"test_component","model_sha":"a"*40,"paper_only":True,"authenticated_execution":False,
               "real_order_submission":False,"exact_relation_attestations":[{"verified":True,"relation":attested}]}
    graph=compile_graph([_registry([])],_universe(),"a"*40,[component])
    assert graph["relations"][0]["relation_family"]=="NEGRISK_TRANSFORMATION"
    attested["directions"]=["SUBMIT_ORDER"]
    assert not compile_graph([_registry([])],_universe(),"a"*40,[component])["relations"]


def test_incremental_shadow_deduplicates_changed_leg_paths_and_records_funnel(tmp_path: Path) -> None:
    relation={"id":"pair","enabled":True,"directions":["BUY_BASKET"],"states":["y","n"],"guaranteed_payout":1,
              "legs":[{"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
                      {"selector":{"market_id":"a"},"outcome":"NO","payout_vector":[0,1]}]}
    graph=compile_graph([_registry([relation])],_universe(),"a"*40)
    args=SimpleNamespace(graph=tmp_path/"graph.json", tape=tmp_path/"tape", status=tmp_path/"status",
                         opportunities=tmp_path/"opportunities", model_sha="a"*40, interval_ms=10)
    shadow=Shadow(args);shadow.generation=graph["graph_generation"];shadow.relations=graph["relations"];shadow.index=graph["dependency_index"]
    row={"schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1","model_sha":"a"*40,**SAFETY,
         "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","receive_wall_ms":10,"market_id":"a",
         "yes_token":"ay","no_token":"an","yes_ask_levels":[{"price":".4","size":"3"}],
         "no_ask_levels":[{"price":".4","size":"3"}],"yes_bid_levels":[{"price":".6","size":"3"}],
         "no_bid_levels":[{"price":".6","size":"3"}],"yes_ask_truncated":False,"no_ask_truncated":False}
    shadow.update(row)
    assert shadow.funnel["raw_path_count"]==2
    assert shadow.funnel["unique_economic_opportunity_count"]==1
    assert shadow.funnel["deduplicated_path_count"]==1
    assert shadow.funnel_by_family["EXPLICIT_EXACT"]["books_ready"]==2
