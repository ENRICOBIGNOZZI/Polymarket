"""Causal, economic and recovery regressions, including seeded properties."""
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from v7_unified_exact_arb_graph import SAFETY, GraphError, evaluate, compile_graph, validate_graph, prove
from v7_exact_arb_causal import CausalBooks, ResourceLedger, ReconstructedDepth
from v7_unified_exact_arb_graph_execution_shadow import simulate
from v7_exact_arb_native_compile import compile_native


def relation(n=3):
    return {"relation_id":"test","enabled":True,"guaranteed_payout":"1","reserve_per_unit":".0005",
            "relation_family":"PARTITION","legs":[{"token_id":str(i),"coefficient":"1","fee_rate":"0",
            "fee_exponent":"1","minimum_order":"0"} for i in range(n)]}


def book(t,price=".2",size="10",lineage="a"):
    return {"timestamp_ms":t,"observation_ms":t,"lineage_id":lineage,"lineage_continuous":True,
            "depth_truncated":False,"asks":[[price,size]],"bids":[[".19","10"]]}


def opportunity(r=None):
    r=r or relation()
    return {**SAFETY,"model_sha":"a"*40,"timestamp_ms":100,"graph_generation":"b"*64,
            "opportunity_id":"id","relation":r,"result":{"direction":"BUY","quantity":"2","net_locked_pnl":".799"}}


def test_future_book_never_becomes_arrival_book():
    history=CausalBooks()
    history.ingest(100,{str(i):book(100) for i in range(3)})
    history.ingest(102,{str(i):book(102,".5") for i in range(3)})
    history.ingest(110,{str(i):book(110,".1") for i in range(3)})
    parallel=simulate(opportunity(),history,"PARALLEL",1,0)
    sequential=simulate(opportunity(),history,"SEQUENTIAL",1,1)
    assert [r["book_timestamp_ms"] for r in parallel["legs"]]==[100,100,100]
    assert [r["book_timestamp_ms"] for r in sequential["legs"]]==[100,102,102]
    assert Fraction(parallel["net_locked_pnl"])>0
    assert Fraction(sequential["net_locked_pnl"])<0
    assert parallel["realized_counterfactual_pnl"] is None


def test_partial_leg_unwind_and_unavailable_liquidity():
    history=CausalBooks()
    initial={str(i):book(100,size="1" if i==2 else "10") for i in range(3)}
    history.ingest(100,initial);history.ingest(110,{str(i):book(110) for i in range(3)})
    result=simulate(opportunity(),history,"BATCH",1,0)
    assert result["partial_fill"] and result["number_of_filled_legs"]==3
    assert result["state"]=="PARTIAL_UNWOUND" and Fraction(result["realized_counterfactual_pnl"])<0
    initial["0"]["bids"]=[]
    result=simulate(opportunity(),history,"BATCH",1,0)
    assert result["state"]=="EXPOSURE_REMAINS" and result["realized_counterfactual_pnl"] is None


def test_lineage_reset_censors_even_when_new_book_is_valid():
    history=CausalBooks();history.ingest(100,{str(i):book(100) for i in range(3)})
    history.ingest(101,{str(i):book(101,lineage="reset") for i in range(3)})
    history.ingest(110,{})
    assert simulate(opportunity(),history,"BATCH",1,0)["reason"]=="arrival_lineage_reset"


def test_transform_needs_explicit_cost_latency_and_capacity():
    history=CausalBooks();history.ingest(100,{str(i):book(100) for i in range(3)});history.ingest(110,{})
    op=opportunity();op["relation"]["transformation"]={"kind":"MERGE","capacity":"10"}
    assert simulate(op,history,"BATCH",1,0)["reason"]=="transformation_terms_missing"
    op["relation"]["transformation"].update(verification="EXPLICIT_VERIFIED",latency_ms=50,capital_lock_time_ms=60,
                                             fixed_cost=".01",variable_cost_per_unit="0",expires_at_ms=1000)
    out=simulate(op,history,"BATCH",1,0)
    assert out["transformation"]["completion_timestamp_ms"]==151
    assert out["realized_counterfactual_pnl"] is None


def test_resource_quantities_persist_across_timestamps_and_release():
    ledger=ResourceLedger();capacities={"PUSD":"10","inventory:x":"3","transform:m":"2"}
    assert ledger.reserve("a",{"PUSD":"4","inventory:x":"2"},capacities,1,100)
    assert not ledger.reserve("b",{"PUSD":"4","inventory:x":"2"},capacities,2,100)
    assert ledger.reserve("b",{"PUSD":"4","inventory:x":"1"},capacities,2,100)
    assert not ledger.reserve("c",{"PUSD":"3"},capacities,100,None)
    assert ledger.reserve("c",{"PUSD":"3"},capacities,101,None)
    ledger.release(100000)
    assert ledger.used()=={"PUSD":Fraction(3)}


def test_seeded_depth_capacity_and_economics_properties():
    rng=random.Random(715)
    for _ in range(250):
        n=rng.randint(2,16);r=relation(n);books={};capacity=[]
        for i,leg in enumerate(r["legs"]):
            coefficient=Fraction(rng.randint(1,8),rng.randint(1,8));leg["coefficient"]=str(coefficient)
            size=Fraction(rng.randint(1,100));capacity.append(size/coefficient)
            books[str(i)]=book(100,str(Fraction(1,4*n)/coefficient),str(size))
        result=evaluate(r,books,100,capital_limit="5")
        if result["accepted"]:
            assert 0<Fraction(result["quantity"])<=min(capacity)
            assert Fraction(result["net_locked_pnl"])>0
            assert Fraction(result["capital_required"])<=5
        future=deepcopy(books);future["0"]["timestamp_ms"]=101
        assert evaluate(r,future,100)["reason"]=="stale_book"


def test_missing_fee_exponent_and_mid_generation_fee_change_fail_closed():
    r=relation(2);r["legs"][0]["fee_rate"]=".07";del r["legs"][0]["fee_exponent"]
    books={str(i):book(100) for i in range(2)}
    assert evaluate(r,books,100)["reason"]=="fee_or_timestamp_missing"
    r["legs"][0]["fee_exponent"]="1";books["0"]["fee_rate"]=".08"
    assert evaluate(r,books,100)["reason"]=="fee_changed"


def test_seeded_exact_proofs_and_invalid_mutations():
    rng=random.Random(187)
    for _ in range(100):
        n=rng.randint(2,16);legs=[]
        for i in range(n):
            c=Fraction(rng.randint(1,100),rng.randint(1,100))
            legs.append({"coefficient":str(c),"payout_vector":[str(1/c) if j==i else "0" for j in range(n)]})
        r={"states":list(range(n)),"guaranteed_payout":"1","legs":legs}
        assert prove(r)["state_totals"]==["1"]*n
        r["legs"][0]["payout_vector"][0]="0"
        with pytest.raises(ValueError):prove(r)


def test_generation_digest_and_dependency_corruption():
    universe={**SAFETY,"model_sha":"a"*40,"markets":[]}
    g=compile_graph([],universe,"a"*40)
    validate_graph(g,"a"*40)
    with pytest.raises(GraphError):validate_graph(g,"b"*40)
    g["dependency_index"]={"unknown":[0]}
    with pytest.raises(GraphError):validate_graph(g,"a"*40)


def test_generated_native_arrays_compile_and_execute(tmp_path):
    universe={**SAFETY,"model_sha":"a"*40,"markets":[{"market_id":"m","condition_id":"c",
        "active":True,"closed":False,"binary_partition_verified":True,"settlement_semantic_hash":"b"*64,
        "clob_token_ids":["y","n"],"outcomes":["UP","DOWN"],"fee_schedule":{"rate":0,"exponent":1}}]}
    g=compile_graph([],universe,"a"*40)
    header,excluded=compile_native(g,"a"*40)
    assert not excluded
    (tmp_path/"graph.hpp").write_text(header)
    source='''#include "graph.hpp"
#include <cassert>
int main() {
 using namespace pm::v7; using namespace pm::v7::exact_arb_graph;
 std::array<BookDeepSnapshot,2> books{};
 for (auto& b:books) {b.valid=1;b.lineage_continuous=1;b.ask_level_count=1;b.ask_levels[0]={4000,1000000};}
 assert(generated::relations.size()==2);
 auto d=evaluate_buy_basket(generated::relations[0],books);
 assert(d.reject==HotReject::Accepted && d.net_pnl_microunits==199500);
 int accepted=0,rejected=0;
 evaluate_token_update(0,generated::dependencies,generated::handles,generated::relations,books,{},
 [&](auto r) {if(r.reject==HotReject::Accepted)++accepted;else ++rejected;});
 assert(accepted==1 && rejected==1);
}'''
    (tmp_path/"main.cpp").write_text(source)
    root=Path(__file__).resolve().parents[1]
    subprocess.run(["c++","-std=c++20","-I"+str(root/"include"),str(tmp_path/"main.cpp"),"-o",str(tmp_path/"test")],check=True,capture_output=True)
    subprocess.run([str(tmp_path/"test")],check=True)


def test_anchor_delta_reconstruction_censors_gaps_and_future_anchors():
    from test_v7_unified_exact_arb_graph import _causal_snapshot
    row=_causal_snapshot({**SAFETY,"schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1",
        "model_sha":"a"*40,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","market_id":"m",
        "receive_wall_ms":100,"yes_token":"y","no_token":"n","yes_state_version":1,"no_state_version":1,
        "yes_ask_truncated":False,"no_ask_truncated":False,"yes_ask_levels":[{"price":".4","size":"10"}],
        "no_ask_levels":[{"price":".4","size":"10"}]})
    replay=ReconstructedDepth("a"*40);replay.anchor(row)
    delta={**SAFETY,"schema":"polymarket_v7_causal_book_observation_v1","model_sha":"a"*40,
        "observer_session_id":"test","connection_epoch":1,"observer_sequence":1,"receive_wall_ms":99,
        "token_id":"y","state_version":1,"valid":True,"lineage_continuous":True,"book_change":None}
    assert replay.delta(delta)[1]=={}
    delta.update(receive_wall_ms=101,observer_sequence=2,state_version=2,
                 book_change={"side":"SELL","price":".4","size":"3"})
    assert replay.delta(delta)[1]["y"]["asks"]==[["2/5","3"]]
    delta.update(receive_wall_ms=102,observer_sequence=4,state_version=3)
    _,invalidated=replay.delta(delta)
    assert invalidated["y"]["lineage_continuous"] is False and replay.gaps==1


def test_unverified_context_hash_cannot_establish_cross_market_equality():
    from v7_exact_relation_discovery import identity
    row={"market_id":"m","asset":"BTC","horizon":"M5","contract_family":"BTC_UP",
         "settlement_semantic_hash":"1"*64,"window_start_unix":1,"close_timestamp_unix":2}
    assert identity(row) is None


def test_replay_restart_emits_once_and_corrupted_generation_blocks(tmp_path):
    from test_v7_unified_exact_arb_graph import _universe, _registry, _causal_snapshot
    from v7_unified_exact_arb_graph_shadow import Shadow
    raw={"id":"pair","enabled":True,"states":["y","n"],"guaranteed_payout":1,"legs":[
        {"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
        {"selector":{"market_id":"a"},"outcome":"NO","payout_vector":[0,1]}]}
    graph=compile_graph([_registry([raw])],_universe(),"a"*40)
    args=SimpleNamespace(graph=tmp_path/"graph",tape=tmp_path/"tape",status=tmp_path/"status",
                         opportunities=tmp_path/"opportunities",model_sha="a"*40,interval_ms=10,capital_limit="100")
    args.graph.write_text(json.dumps(graph))
    row=_causal_snapshot({**SAFETY,"schema":"polymarket_v7_pure_arb_deep_book_snapshot_v1","model_sha":"a"*40,
        "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","market_id":"a","receive_wall_ms":100,
        "yes_token":"ay","no_token":"an","yes_ask_truncated":False,"no_ask_truncated":False,
        "yes_ask_levels":[{"price":".4","size":"3"}],"no_ask_levels":[{"price":".4","size":"3"}]})
    first=Shadow(args);first.graph();first.update(row)
    second=Shadow(args);second.graph();second.update(row)
    assert len(args.opportunities.read_text().splitlines())==1
    assert first.funnel==second.funnel and first.resources.used()==second.resources.used()
    broken=deepcopy(graph);broken["model_sha"]="b"*40;args.graph.write_text(json.dumps(broken))
    second.graph();assert second.graph_state=="BLOCKED_INVALID_GRAPH"


def test_fractional_telemetry_and_truncated_books_still_report_distance():
    r=relation(2);books={str(i):book(100,".501") for i in range(2)}
    books["0"]["depth_truncated"]=True
    out=evaluate(r,books,100)
    assert not out["accepted"] and out["reason"]=="truncated_depth"
    assert Fraction(out["distance_to_raw_arbitrage"])==Fraction(1,500)
    del books["0"]["depth_truncated"]
    assert evaluate(r,books,100)["reason"]=="truncated_depth"


def test_delta_trade_does_not_refresh_depth_timestamp():
    replay=ReconstructedDepth("a"*40)
    replay.current["y"]={**book(100),"state_version":1}
    row={**SAFETY,"schema":"polymarket_v7_causal_book_observation_v1","model_sha":"a"*40,
         "observer_session_id":"test","connection_epoch":1,"observer_sequence":1,"receive_wall_ms":200,
         "token_id":"y","state_version":2,"valid":True,"lineage_continuous":True,"book_change":None,
         "receive_monotonic_ns":300000000,"book_receive_monotonic_ns":200000000}
    assert replay.delta(row)[1]["y"]["timestamp_ms"]==100
    row.update(observer_sequence=2,state_version=3,receive_wall_ms=210)
    row.pop("receive_monotonic_ns");row.pop("book_receive_monotonic_ns")
    assert replay.delta(row)[1]["y"]["timestamp_ms"]==100


def test_jsonl_cursor_drains_rotation_without_loss_or_repetition(tmp_path):
    from v7_exact_arb_causal import JsonlCursor
    current=tmp_path/"current.jsonl"
    current.write_text('{"n":1}\n{"n":2}\n')
    cursor=JsonlCursor(current,segmented=True)
    assert list(cursor.poll(limit=1))==[{"n":1}]
    current.rename(tmp_path/"session.segment-1000000.jsonl")
    current.write_text('{"n":3}\n{"n":')
    assert list(cursor.poll())==[{"n":2},{"n":3}]
    with current.open("a") as stream:stream.write('4}\n')
    assert list(cursor.poll())==[{"n":4}]
    assert list(cursor.poll())==[]


def test_native_dependency_capacity_is_atomic_per_relation():
    from test_v7_unified_exact_arb_graph import _universe, _registry
    from v7_unified_exact_arb_graph import generation_hash
    raw={"id":"pair","enabled":True,"states":["y","n"],"guaranteed_payout":1,"legs":[
        {"selector":{"market_id":"a"},"outcome":"YES","payout_vector":[1,0]},
        {"selector":{"market_id":"a"},"outcome":"NO","payout_vector":[0,1]}]}
    graph=compile_graph([_registry([raw])],_universe(),"a"*40)
    template=graph["relations"][0]
    graph["relations"]=[{**deepcopy(template),"relation_id":str(i),
                         "directions":["BUY_BASKET"] if i<63 else ["BUY_BASKET","SELL_INVENTORY_BASKET"]}
                        for i in range(64)]
    graph["dependency_index"]={leg["token_id"]:list(range(64)) for leg in template["legs"]}
    graph["graph_generation"]=generation_hash(graph)
    header,excluded=compile_native(graph,"a"*40)
    assert "std::array<CompiledRelation, 63>" in header
    assert excluded==[{"relation_id":"63","reason":"native_dependency_capacity"}]


def test_python_native_nleg_depth_fee_and_inventory_parity(tmp_path):
    source=r'''#include "pm/v7_exact_arb_graph_hotpath.hpp"
#include <iostream>
int main() {
 using namespace pm::v7; using namespace pm::v7::exact_arb_graph;
 int n,buy;
 while(std::cin>>n>>buy) {
  CompiledRelation r{};r.enabled=1;r.leg_count=n;
  r.guaranteed_payout_microunits=1000000;r.reserve_per_unit_microunits=500;
  std::array<BookDeepSnapshot,16> books{};std::array<std::int64_t,16> inventory{};
  for(int i=0;i<n;++i) {
   auto& leg=r.legs[i];leg.book_handle=i;leg.fee_verified=1;
   std::cin>>leg.coefficient.numerator>>leg.coefficient.denominator>>leg.fee_rate>>leg.fee_exponent;
   leg.minimum_order_microunits=1000000;
   auto& b=books[i];b.valid=1;b.lineage_continuous=1;b.ask_level_count=3;b.bid_level_count=3;
   for(int j=0;j<3;++j) {
    std::cin>>b.ask_levels[j].price_e4>>b.ask_levels[j].quantity_microunits;
    b.bid_levels[j]=b.ask_levels[j];
   }
   inventory[i]=1000000000;
  }
  HotResources resources{};resources.inventory=inventory;
  auto d=evaluate_basket(r,books,buy,{},resources);
  std::cout<<int(d.reject)<<' '<<d.quantity_microunits<<' '<<d.raw_pnl_microunits
   <<' '<<d.fees_microunits<<' '<<d.net_pnl_microunits<<' '<<d.capital_required_microunits;
  for(int i=0;i<n;++i)std::cout<<' '<<d.levels_consumed[i];
  std::cout<<'\n';
 }
}'''
    root=Path(__file__).resolve().parents[1]
    (tmp_path/"parity.cpp").write_text(source)
    subprocess.run(["c++","-std=c++20","-O2","-I"+str(root/"include"),str(tmp_path/"parity.cpp"),
                    "-o",str(tmp_path/"parity")],check=True,capture_output=True)
    rng=random.Random(921);cases=[];wire=[]
    for n in (2,3,4,8,16):
        for buy in (True,False):
            for _ in range(4):
                r=relation(n);r["directions"]=["BUY_BASKET" if buy else "SELL_INVENTORY_BASKET"]
                books={};line=[str(n),str(int(buy))]
                for i,leg in enumerate(r["legs"]):
                    c=Fraction(rng.randint(2,4),2);rate=Fraction(rng.randint(0,7),100)
                    exponent=1 if _==0 else _%3
                    leg.update(coefficient=str(c),fee_rate=str(rate),fee_exponent=str(exponent),minimum_order="1",
                               fee_rounding_mode="VENUE_5DP",fee_rounding_increment=".00001")
                    line.extend((str(c.numerator),str(c.denominator),str(float(rate)),str(exponent)))
                    base=int(Fraction(7000 if buy else 13000,n)/c)
                    depth=[]
                    for j in range(3):
                        price=base+(j*23 if buy else -j*23);size=rng.randint(1,9)
                        depth.append([str(Fraction(price,10000)),str(size)])
                        line.extend((str(price),str(size*1000000)))
                    books[str(i)]={**book(100),"asks":depth,"bids":depth}
                expected=evaluate(r,books,100,capital_limit="1000000",inventory_limit="100")
                cases.append(expected);wire.append(" ".join(line))
    output=subprocess.run([str(tmp_path/"parity")],input="\n".join(wire)+"\n",text=True,
                          capture_output=True,check=True).stdout.splitlines()
    assert len(output)==len(cases)
    for case,(expected,line) in enumerate(zip(cases,output)):
        actual=list(map(int,line.split()))
        assert (actual[0]==0)==expected["accepted"]
        if not expected["accepted"]:continue
        assert actual[1]==Fraction(expected["quantity"])*1000000
        for index,field in enumerate(("gross_pnl","fee_drag","net_locked_pnl","capital_required"),2):
            # Native outputs microcurrency; Python retains exact fractions.
            assert abs(actual[index]-Fraction(expected[field])*1000000)<=1,(case,field,actual[index],expected[field],wire[case])
        assert actual[6:]==expected["levels_consumed_per_leg"]
