from __future__ import annotations
from pathlib import Path
import json
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_unified_exact_arb_graph import SAFETY, GraphError, compile_graph, evaluate


def test_indexed_market_resolution_preserves_ambiguity_without_full_scan():
    from v7_unified_exact_arb_graph import market_lookup, resolve_market
    markets=[{"market_id":str(i),"asset":"x"} for i in range(1000)]
    lookup=market_lookup({"markets":markets})
    class NoScan(list):
        def __iter__(self):raise AssertionError("indexed lookup scanned the full universe")
    assert resolve_market(NoScan(markets),{"market_id":"800","asset":"x"},lookup)==markets[800]
    assert resolve_market(NoScan(markets),{"market_id":"800","asset":"y"},lookup) is None
    lookup[(("market_id","800"),)].append(dict(markets[800]))
    assert resolve_market(NoScan(markets),{"market_id":"800"},lookup) is None
from v7_unified_exact_arb_graph_shadow import Shadow, native_binary_candidate
from v7_exact_relation_discovery import build as discover_relations


def _causal_snapshot(row):
    return {**row, "observer_session_id":"test", "connection_epoch":1,
            **{side+"_"+field: value for side in ("yes","no") for field,value in (
                ("valid",True),("lineage_continuous",True),("receive_wall_ms",row["receive_wall_ms"]),
                ("bid_truncated",False))}}


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
                    {"x":{"lineage_continuous":True,"depth_truncated":True,"timestamp_ms":1,
                          "fee_rate":"0","asks":[[".4","1"]]}}, 1)["reason"] == "truncated_depth"


def test_machine_attested_binary_partition_is_auto_compiled() -> None:
    model, semantic = "d" * 40, "e" * 64
    universe={**SAFETY,"model_sha":model,"markets":[{"market_id":"m","condition_id":"c","active":True,"closed":False,
        "binary_partition_verified":True,"asset":"BTC","horizon":"M5","contract_family":"binary","settlement_semantic_hash":semantic,
        "clob_token_ids":["y","n"],"outcomes":["YES","NO"]}]}
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","version":1,"relations":[]}
    graph=compile_graph([registry],universe,model)
    assert graph["relations"][0]["relation_family"]=="SAME_MARKET_BINARY_COMPLETE_SET"


def test_discovered_payoff_identical_duplicates_flow_into_the_canonical_graph() -> None:
    model, semantic="d"*40,"e"*64
    markets=[]
    for mid in ("a","b"):
        markets.append({"market_id":mid,"event_id":"e"+mid,"condition_id":"c"+mid,"active":True,"closed":False,
          "asset":"BTC","horizon":"M5","contract_family":"binary","settlement_semantic_hash":semantic,
          "window_start_unix":1,"close_timestamp_unix":2,"fee_schedule":{"rate":0},
          "settlement_identity_verified":True,"normalized_rules_hash":"f"*64,
          "clob_token_ids":[mid+"y",mid+"n"],"outcomes":["YES","NO"]})
    universe={**SAFETY,"model_sha":model,"markets":markets}
    discovered=discover_relations(universe,model)
    graph=compile_graph([discovered],universe,model)
    assert len(discovered["relations"])==2
    assert {relation["relation_family"] for relation in graph["relations"]}=={"AUTOMATIC_PAYOFF_IDENTICAL_DUPLICATE"}


def test_machine_attested_n_way_partition_is_a_single_hyperedge() -> None:
    model, semantic="d"*40,"e"*64
    universe={**SAFETY,"model_sha":model,"markets":[{"market_id":"p","condition_id":"cp","active":True,"closed":False,
      "asset":"BTC","horizon":"M5","contract_family":"partition","settlement_semantic_hash":semantic,
      "clob_token_ids":["pa","pb","pc"],"outcomes":["A","B","C"],"partition_verified":True,
      "partition_states":["A","B","C"],"partition_payout_vectors":{"A":[1,0,0],"B":[0,1,0],"C":[0,0,1]}}]}
    graph=compile_graph([_registry([])],universe,model)
    relation=graph["relations"][0]
    assert relation["relation_family"]=="N_WAY_COMPLETE_PARTITION" and len(relation["legs"])==3


def test_negrisk_metadata_verified_flags_cannot_manufacture_an_exhaustive_state_space() -> None:
    model, semantic="d"*40,"e"*64
    members=[]
    for mid in ("n1","n2","n3"):
        members.append({"market_id":mid,"event_id":"event","condition_id":"c"+mid,"active":True,"closed":False,
          "neg_risk":True,"neg_risk_complete_set_verified":True,"asset":"BTC","horizon":"M5",
          "neg_risk_complete_set_market_ids":["n1","n2","n3"],
          "contract_family":"negrisk","settlement_semantic_hash":semantic,"fee_schedule":{"rate":0},
          "clob_token_ids":[mid+"y",mid+"n"],"outcomes":["YES","NO"]})
    graph=compile_graph([_registry([])],{**SAFETY,"model_sha":model,"markets":members},model)
    assert not graph["relations"]
    assert any(c["reason"]=="metadata_flag_is_not_independent_terminal_exhaustiveness_proof"
               for c in graph["unverified_candidates"])
    members[-1]["neg_risk_complete_set_verified"]=False
    graph=compile_graph([_registry([])],{**SAFETY,"model_sha":model,"markets":members},model)
    assert not graph["relations"] and graph["unverified_candidates"]


def test_proven_inequality_is_retained_but_never_actionable() -> None:
    model, semantic="f"*40,"a"*64
    universe={**SAFETY,"model_sha":model,"markets":[{"market_id":"m","active":True,"closed":False,"asset":"BTC","horizon":"M5","contract_family":"binary","settlement_semantic_hash":semantic,"clob_token_ids":["y","n"],"outcomes":["YES","NO"]}]}
    registry={**SAFETY,"schema":"polymarket_v7_exact_arb_relation_registry_v1","version":1,"relations":[{"id":"upper","enabled":True,"relation_type":"PAYOFF_UPPER_BOUND","states":["a","b"],"guaranteed_payout":1,"legs":[{"selector":{"market_id":"m"},"outcome":"YES","payout_vector":[1,0]}]}]}
    relation=compile_graph([registry],universe,model)["relations"][0]
    assert relation["proof_type"]=="FINITE_STATE_EXACT_RATIONAL_INEQUALITY" and relation["enabled"] is False


def test_logical_implication_has_its_own_statewise_proof_and_fails_invalid_direction() -> None:
    relation={"id":"implies","enabled":True,"relation_type":"LOGICAL_IMPLICATION","states":["a","b"],
      "antecedent_payout_vector":[1,0],"consequent_payout_vector":[1,1],"legs":[
        {"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
        {"selector":{"market_id":"b"},"outcome":"YES","payout_vector":[1,1]}]}
    graph=compile_graph([_registry([relation])],_universe(),"a"*40)
    assert graph["relations"][0]["proof_type"]=="FINITE_STATE_EXACT_RATIONAL_IMPLICATION"
    relation["consequent_payout_vector"]=[0,1]
    assert not compile_graph([_registry([relation])],_universe(),"a"*40)["relations"]


def test_minimum_order_and_capital_are_fail_closed() -> None:
    relation={"enabled":True,"guaranteed_payout":"1","reserve_per_unit":"0","legs":[{"token_id":"a","coefficient":"1","minimum_order":"10"}]}
    books={"a":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0","asks":[[".5","5"]]}}
    assert evaluate(relation,books,1)["reason"]=="minimum_order"
    relation["legs"][0]["minimum_order"]="0";books["a"]["asks"]=[[".5","10"]]
    bounded=evaluate(relation,books,1,capital_limit="1")
    assert bounded["accepted"] and bounded["quantity"]=="2" and bounded["capital_required"]=="1"
    assert evaluate(relation,books,1,capital_limit="0")["reason"]=="capital_limit"


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
    assert evaluate(relation,books,1)["reason"]=="inventory_unavailable"
    out=evaluate(relation,books,1,inventory_limit="3")
    assert out["accepted"] is True and out["direction"]=="SELL" and out["quantity"]=="3"
    assert out["distance_to_raw_arbitrage"]=="-1/5"


def test_missing_bid_does_not_suppress_a_valid_buy_direction() -> None:
    relation={"enabled":True,"directions":["BUY_COMPLETE_SET","SELL_COMPLETE_SET"],"guaranteed_payout":"1",
              "legs":[{"token_id":"y","coefficient":"1"},{"token_id":"n","coefficient":"1"}]}
    books={token:{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                  "asks":[["2/5","1"]]} for token in ("y","n")}
    assert evaluate(relation,books,1)["direction"]=="BUY"


def test_nonpositive_buy_edge_is_never_emitted() -> None:
    relation={"enabled":True,"guaranteed_payout":"1","legs":[
        {"token_id":"y","coefficient":"1"},{"token_id":"n","coefficient":"1"}]}
    books={token:{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                  "asks":[["3/5","1"]]} for token in ("y","n")}
    assert evaluate(relation,books,1)["reason"]=="edge_after_costs_nonpositive"


def test_fee_rounding_is_exact_and_missing_bid_depth_fails_closed() -> None:
    relation={"enabled":True,"directions":["BUY_BASKET"],"guaranteed_payout":"1","legs":[
        {"token_id":"x","coefficient":"1","minimum_order":"0","fee_rounding_increment":"1/100",
         "fee_rounding_mode":"CEILING"}]}
    books={"x":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"1/100","fee_exponent":0,
                "asks":[["1/2","1"]]}}
    out=evaluate(relation,books,1)
    assert out["accepted"] is True and out["net_locked_pnl"]=="49/100"
    assert out["distance_to_after_fee_arbitrage"]=="-49/100"


def test_verified_market_fee_exponent_is_applied_per_price_level() -> None:
    relation={"enabled":True,"guaranteed_payout":"1","legs":[{"token_id":"x","coefficient":"1",
              "minimum_order":"0","fee_exponent":"1"}]}
    books={"x":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"1/10",
                "asks":[["1/2","1"]]}}
    assert evaluate(relation,books,1)["net_locked_pnl"]=="19/40"


def test_capital_time_economics_are_explicit_when_attested() -> None:
    relation={"enabled":True,"guaranteed_payout":"1","capital_lock_time_ms":1000,
              "legs":[{"token_id":"x","coefficient":"1","minimum_order":"0"}]}
    books={"x":{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                "asks":[["1/2","2"]]}}
    out=evaluate(relation,books,1)
    assert out["capital_required"]=="1" and out["net_locked_pnl_per_capital_time"]=="1/1000"


def test_verified_transform_capacity_and_lock_are_enforced() -> None:
    relation={"id":"merge","enabled":True,"guaranteed_payout":"1","legs":[
      {"selector":{"market_id":"a"},"outcome":"YES","minimum_order":"1","payout_vector":[1,0]},
      {"selector":{"market_id":"a"},"outcome":"NO","minimum_order":"1","payout_vector":[0,1]}],"states":["y","n"],
      "transformation":{"kind":"MERGE","verification":"EXPLICIT_VERIFIED","capacity":"1","latency_ms":2,
                        "capital_lock_time_ms":100,"proof_hash":"f"*64,
                        "fixed_cost":"0","variable_cost_per_unit":"0","expires_at_ms":10000}}
    graph=compile_graph([_registry([relation])],_universe(),"a"*40)
    books={token:{"timestamp_ms":1,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                  "asks":[["2/5","2"]]} for token in ("ay","an")}
    out=evaluate(graph["relations"][0],books,1)
    assert out["accepted"] is True and out["quantity"]=="1" and out["capital_lock_time_ms"]==100
    relation["transformation"]["capacity"]="1/2"
    assert evaluate(compile_graph([_registry([relation])],_universe(),"a"*40)["relations"][0],books,1)["reason"]=="transformation_capacity"


def test_invalid_inequality_and_unverified_components_remain_non_actionable() -> None:
    invalid={"id":"bad","enabled":True,"relation_type":"PAYOFF_LOWER_BOUND","states":["a","b"],"guaranteed_payout":1,
             "legs":[{"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]}]}
    graph=compile_graph([_registry([invalid])],_universe(),"a"*40,
        [{"schema":"polymarket_v7_combo_rfq_shadow_v1","model_sha":"a"*40,"paper_only":True,
          "authenticated_execution":False,"real_order_submission":False}])
    assert not graph["relations"]
    assert any(row["relation_family"]=="LEGACY_COMPONENT" for row in graph["unverified_candidates"])


def test_combo_catalog_and_rfq_gateway_are_retained_as_unverified_component_sources() -> None:
    inputs=[{"schema":"polymarket_v7_combo_market_source_v1","model_sha":"a"*40,"paper_only":True,
             "authenticated_execution":False,"real_order_submission":False},
            {"schema":"polymarket_v7_combo_rfq_gateway_status_v1","model_sha":"a"*40,"paper_only":True,
             "authenticated_execution":False,"real_order_submission":False}]
    graph=compile_graph([_registry([])],_universe(),"a"*40,inputs)
    assert len(graph["component_provenance"])==2
    assert {row["source_schema"] for row in graph["unverified_candidates"] if row["relation_family"]=="LEGACY_COMPONENT"}=={x["schema"] for x in inputs}


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
                         opportunities=tmp_path/"opportunities", model_sha="a"*40, interval_ms=10, capital_limit="100")
    shadow=Shadow(args);shadow.generation=graph["graph_generation"];shadow.relations=graph["relations"];shadow.index=graph["dependency_index"]
    row={"schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1","model_sha":"a"*40,**SAFETY,
         "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","receive_wall_ms":10,"market_id":"a",
         "yes_token":"ay","no_token":"an","yes_ask_levels":[{"price":".4","size":"3"}],
         "no_ask_levels":[{"price":".4","size":"3"}],"yes_bid_levels":[{"price":".6","size":"3"}],
         "no_bid_levels":[{"price":".6","size":"3"}],"yes_ask_truncated":False,"no_ask_truncated":False}
    shadow.update(_causal_snapshot(row))
    assert shadow.funnel["raw_path_count"]==1
    assert shadow.funnel["unique_economic_opportunity_count"]==1
    assert shadow.funnel["deduplicated_path_count"]==0
    assert shadow.funnel_by_family["EXPLICIT_EXACT"]["books_ready"]==1
    emitted=json.loads(args.opportunities.read_text().splitlines()[0])
    assert set(emitted["decision_books"]) == {"ay","an"}
    from v7_unified_exact_arb_graph import sha
    assert emitted["decision_books_sha256"] == sha(emitted["decision_books"])
    candidate=native_binary_candidate(graph["relations"][0], {"direction":"BUY","quantity":"3"}, 10)
    assert candidate is not None and candidate["kind"]=="BUY_COMPLETE_SET" and candidate["market_id"]=="a"


def test_incremental_graph_replay_is_deterministic(tmp_path: Path) -> None:
    relation={"id":"pair","enabled":True,"directions":["BUY_BASKET"],"states":["y","n"],"guaranteed_payout":1,
      "legs":[{"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
              {"selector":{"market_id":"a"},"outcome":"NO","payout_vector":[0,1]}]}
    graph=compile_graph([_registry([relation])],_universe(),"a"*40)
    def replay(name: str) -> tuple[str, dict]:
        root=tmp_path/name
        root.mkdir()
        args=SimpleNamespace(graph=root/"graph",tape=root/"tape",status=root/"status",opportunities=root/"opportunities",
                             model_sha="a"*40,interval_ms=10,capital_limit="100")
        shadow=Shadow(args);shadow.generation=graph["graph_generation"];shadow.relations=graph["relations"];shadow.index=graph["dependency_index"]
        for now in (10,100):
            row={"schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1","model_sha":"a"*40,**SAFETY,
                 "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","receive_wall_ms":now,"market_id":"a",
                 "yes_token":"ay","no_token":"an","yes_ask_levels":[{"price":".4","size":"3"}],
                 "no_ask_levels":[{"price":".4","size":"3"}],"yes_bid_levels":[{"price":".6","size":"3"}],
                 "no_bid_levels":[{"price":".6","size":"3"}],"yes_ask_truncated":False,"no_ask_truncated":False}
            shadow.update(_causal_snapshot(row))
        return args.opportunities.read_text(),dict(shadow.funnel)
    assert replay("left")==replay("right")


def test_shadow_reserves_shared_capital_once_per_snapshot(tmp_path: Path) -> None:
    relations=[{"id":"same","enabled":True,"states":["y","n"],"guaranteed_payout":1,"legs":[
                 {"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
                 {"selector":{"market_id":"a"},"outcome":"NO","payout_vector":[0,1]}]},
               {"id":"cross","enabled":True,"states":["y","n"],"guaranteed_payout":1,"legs":[
                 {"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
                 {"selector":{"market_id":"b"},"outcome":"NO","payout_vector":[0,1]}]}]
    graph=compile_graph([_registry(relations)],_universe(),"a"*40)
    args=SimpleNamespace(graph=tmp_path/"graph",tape=tmp_path/"tape",status=tmp_path/"status",opportunities=tmp_path/"opportunities",
                         model_sha="a"*40,interval_ms=10,capital_limit="3")
    shadow=Shadow(args);shadow.generation=graph["graph_generation"];shadow.relations=graph["relations"];shadow.index=graph["dependency_index"]
    shadow.books["bn"]={"timestamp_ms":10,"lineage_continuous":True,"depth_truncated":False,"fee_rate":"0",
                         "asks":[[".4","3"]],"bids":[[".6","3"]]}
    row={"schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1","model_sha":"a"*40,**SAFETY,
         "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","receive_wall_ms":10,"market_id":"a",
         "yes_token":"ay","no_token":"an","yes_ask_levels":[{"price":".4","size":"3"}],
         "no_ask_levels":[{"price":".4","size":"3"}],"yes_bid_levels":[{"price":".6","size":"3"}],
         "no_bid_levels":[{"price":".6","size":"3"}],"yes_ask_truncated":False,"no_ask_truncated":False}
    shadow.update(_causal_snapshot(row))
    assert shadow.funnel["candidate_emitted"]==1 and shadow.funnel["capital_conflicts"]>=1
    assert len(args.opportunities.read_text().splitlines())==1
