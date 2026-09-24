from copy import deepcopy
from fractions import Fraction
import itertools
import json
import random
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_evidence import data, MODEL, SAFETY
from v7_exact_arb_native_execution_bridge import native_candidate
from v7_exact_arb_resource_scheduler import select, native_request, ShadowResourceJournal, rounding_partition_upper_bound
from v7_exact_arb_resource_scheduler import ShadowCapitalJournal
from test_v7_exact_arb_native_arrival import inputs, next_frame, seal
from v7_exact_arb_native_arrival import NativeArrivalHistory
from v7_unified_exact_arb_graph_execution_shadow import simulate
from v7_unified_exact_arb_graph import sha, round_fee


def request(name, resources, score, **extra):
    return {**SAFETY,"schema":"polymarket_v7_exact_arb_resource_request_v1","model_sha":MODEL,
        "request_id":name,"portfolio_id":sha(name),"decision_available_ns":100,"valid_until_ns":1000,
        "resources":resources,"selection_score":str(score),"score_kind":"WORST_LIMIT_FULL_FILL_NET_PNL_NOT_EXPECTED_VALUE",**extra}


def snapshot(capacities,**extra):
    return {**SAFETY,"schema":"polymarket_v7_exact_arb_shadow_capacity_v1","model_sha":MODEL,
        "capacity_basis":"TOTAL_BEFORE_RESEARCH_HOLDS_NOT_EXECUTION_AUTHORITY",
        "available_ns":100,"expires_ns":1000,"capacities":capacities,**extra}


@pytest.fixture
def capital_case(inputs,tmp_path):
    raw,candidate,frame=inputs
    journal=ShadowCapitalJournal(tmp_path/"capital.sqlite",MODEL,"world")
    request=native_request(candidate);when=request["decision_available_ns"]
    source=snapshot({"PUSD":"10",**request["depth_capacities"]},available_ns=when,expires_ns=request["valid_until_ns"])
    assert journal.admit("batch",[request],source,when)["selected_ids"]==[request["request_id"]]
    def outcome(kind="full",ack=0):
        history=NativeArrivalHistory(raw,MODEL)
        try:
            history.ingest(json.dumps(frame))
            books=deepcopy(frame["books"])
            for index,book in enumerate(books):
                book.update(receive_monotonic_ns=1000100000,state_version=2)
                if kind=="zero" or (kind=="partial" and index==1): book["asks_e4_microshares"]=[[6000,5000000]]
            history.ingest(json.dumps(next_frame(frame,2,1000100000,books)))
            history.ingest(json.dumps(next_frame(frame,3,1020000000)))
            seal(history)
            result=simulate(candidate,history.for_candidate(candidate),"PARALLEL",1,0,ack_delay_ms=ack)
            result["shared_world_id"]="world"
            return result
        finally: history.close()
    yield journal,candidate,source,outcome
    journal.close()


def test_capital_zero_fill_releases_only_after_model_ack(capital_case):
    journal,candidate,source,outcome=capital_case
    before=journal.capital_receipt()
    assert before["pending_order_groups"]==before["unresolved_order_groups"]==1
    assert before["censored_order_groups"]==0
    result=outcome("zero",ack=2)
    assert result["state"]=="NO_LEGS_FILLED"
    done=int(Fraction(result["entry_results_timestamp_ms"])*1000000)
    with pytest.raises(ValueError,match="unacknowledged_fill"): journal.complete(candidate,result,done)
    receipt=journal.complete(candidate,result,done+1)
    assert receipt["modeled_orders_closed"] and receipt["modeled_flat_net_pnl"]=="0"
    assert journal.capital_receipt()["available_financial_resources"]["PUSD"]=="10"
    assert journal.capital_receipt()["unresolved_order_groups"]==0
    assert journal.complete(candidate,result,done+1)==receipt
    assert not receipt["resource_release_verified"]
    # Releasing its hold does not erase the request identity or permit replay.
    request=native_request(candidate)
    plan=journal.admit("repeat",[request],source,done+2)
    assert plan["rejected"][request["request_id"]]=="ALREADY_RESERVED"


def test_full_fill_spends_cash_and_keeps_tokens_without_payout_credit(capital_case):
    journal,candidate,source,outcome=capital_case
    result=outcome();assert result["state"]=="ALL_LEGS_FILLED"
    journal.complete(candidate,result,int(Fraction(result["timestamp_ms"])*1000000)+1)
    receipt=journal.capital_receipt()
    assert receipt["modeled_balances"]["PUSD"]=="6"
    assert receipt["available_financial_resources"]["PUSD"]=="2399/400"
    assert all(receipt["modeled_balances"]["inventory:"+leg["token_id"]]=="5" for leg in candidate["relation"]["legs"])
    assert receipt["portfolio_net_pnl"] is None and receipt["modeled_flat_net_pnl"]=="0"


def test_partial_unwind_recomputes_cash_loss_and_retains_reserve(capital_case):
    journal,candidate,source,outcome=capital_case
    result=outcome("partial");assert result["state"]=="PARTIAL_UNWOUND"
    done=int(Fraction(result["timestamp_ms"])*1000000)+1
    receipt=journal.complete(candidate,result,done)
    assert receipt["modeled_flat_net_pnl"]=="-21/400"
    assert journal.capital_receipt()["modeled_balances"]["PUSD"]=="199/20"
    assert journal.capital_receipt()["available_financial_resources"]["PUSD"]=="3979/400"


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(shared_world_id="other"),lambda r:r.update(realized_counterfactual_pnl="1000"),
    lambda r:r["legs"][0].update(notional="0"),lambda r:r["unwind_legs"][0].update(filled_size="0"),
    lambda r:r.update(unhedged_exposure={"x":"1"}),lambda r:r.update(reserve_drag="0"),
    lambda r:r["unwind_legs"][0].update(submission_timestamp_ms=1000),
])
def test_malformed_result_cannot_release_or_credit_money(capital_case,mutation):
    journal,candidate,source,outcome=capital_case
    result=outcome("partial");before=journal.capital_receipt();mutation(result)
    with pytest.raises(ValueError): journal.complete(candidate,result,1010000000)
    assert journal.capital_receipt()==before


def test_censoring_and_source_expiry_never_release_capital(capital_case):
    journal,candidate,source,outcome=capital_case
    result=outcome();result.update(state="CENSORED",reason="unknown_ack")
    before=journal.capital_receipt()["available_financial_resources"]
    receipt=journal.complete(candidate,result,source["expires_ns"]+1000000)
    assert not receipt["modeled_orders_closed"]
    assert journal.capital_receipt()["available_financial_resources"]==before
    assert journal.capital_receipt()["censored_order_groups"]==1
    assert journal.capital_receipt()["pending_order_groups"]==0


def test_capital_restart_preserves_balances_results_and_world_boundary(capital_case):
    journal,candidate,source,outcome=capital_case
    result=outcome("partial");when=int(Fraction(result["timestamp_ms"])*1000000)+1
    event=journal.complete(candidate,result,when);before=journal.capital_receipt()
    path=journal.db.execute("PRAGMA database_list").fetchone()[2]
    other=ShadowCapitalJournal(path,MODEL,"world")
    try:
        assert other.capital_receipt()==before
        assert other.complete(candidate,result,when)==event
        with pytest.raises(ValueError,match="capital_conflicting_result"): other.complete(candidate,result,when+1)
    finally: other.close()
    with pytest.raises(ValueError,match="capital_world_identity"): ShadowCapitalJournal(path,MODEL,"different")


def test_capital_transition_rolls_back_all_money_on_persistence_failure(capital_case):
    journal,candidate,source,outcome=capital_case
    before=journal.capital_receipt()
    journal.db.execute("CREATE TRIGGER fail_result BEFORE INSERT ON capital_results BEGIN SELECT RAISE(FAIL, 'injected'); END")
    result=outcome("partial")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match="injected"): journal.complete(candidate,result,1010000000)
    assert journal.capital_receipt()==before
    journal.db.execute("DROP TRIGGER fail_result")
    assert journal.complete(candidate,result,1010000000)["modeled_orders_closed"]


def test_empty_world_has_declared_resources_not_invented_zero_balance(tmp_path):
    journal=ShadowCapitalJournal(tmp_path/"empty.sqlite",MODEL,"world",{"PUSD":"1000"})
    try:
        receipt=journal.capital_receipt()
        assert receipt["available_financial_resources"]=={"PUSD":"1000"}
        assert receipt["portfolio_net_pnl"] is None
    finally: journal.close()


def test_capital_policy_cannot_be_downgraded_to_no_release_class(capital_case):
    journal,_,_,_=capital_case
    path=journal.db.execute("PRAGMA database_list").fetchone()[2]
    with pytest.raises(ValueError,match="journal_mode"): ShadowResourceJournal(path,MODEL)


@pytest.mark.parametrize("legacy",[False,True])
def test_no_release_baseline_cannot_silently_gain_release_policy(tmp_path,legacy):
    path=tmp_path/"baseline.sqlite"
    journal=ShadowResourceJournal(path,MODEL)
    journal.admit("batch",[request("A",{"PUSD":"1"},1)],snapshot({"PUSD":"2"}),100)
    if legacy:
        journal.db.execute("DELETE FROM resource_meta WHERE key='journal_mode'");journal.db.commit()
    journal.close()
    with pytest.raises(ValueError,match="journal_mode"): ShadowCapitalJournal(path,MODEL,"world")
    original=ShadowResourceJournal(path,MODEL)
    try:
        assert original.db.execute("SELECT COUNT(*) FROM resource_holds").fetchone()[0]==1
    finally: original.close()


def test_sell_spends_prefunded_inventory_without_synthetic_short(native,tmp_path):
    from v7_exact_arb_causal import CausalBooks
    full,episode,bundles=native
    candidate=native_candidate(full,episode,bundles,MODEL)
    candidate["result"].update(direction="SELL",net_locked_pnl="399/400")
    candidate["inventory_reserved"]=True
    for book in candidate["decision_books"].values():
        book.update(bids=[["3/5","5"]],asks=[["61/100","5"]])
    candidate["decision_books_sha256"]=sha(candidate["decision_books"])
    request=native_request(candidate);when=request["decision_available_ns"]
    inventory={"inventory:"+leg["token_id"]:"5" for leg in candidate["relation"]["legs"]}
    journal=ShadowCapitalJournal(tmp_path/"sell.sqlite",MODEL,"world",{"PUSD":"20",**inventory})
    try:
        capacities={"PUSD":"20",**inventory,**request["depth_capacities"]}
        source=snapshot(capacities,available_ns=when,expires_ns=request["valid_until_ns"])
        assert journal.admit("sell",[request],source,when)["selected_ids"]==[request["request_id"]]
        history=CausalBooks()
        history.ingest(Fraction(candidate["timestamp_ms"]),candidate["decision_books"])
        history.ingest(Fraction(candidate["timestamp_ms"])+10,{})
        result=simulate(candidate,history,"PARALLEL",1,0);result["shared_world_id"]="world"
        assert result["state"]=="ALL_LEGS_FILLED"
        journal.complete(candidate,result,int(Fraction(result["timestamp_ms"])*1000000)+1)
        receipt=journal.capital_receipt()
        assert receipt["modeled_balances"]["PUSD"]=="26"
        assert all(receipt["modeled_balances"][key]=="0" for key in inventory)
        assert receipt["portfolio_net_pnl"] is None  # inventory acquisition basis is not known
    finally: journal.close()


def test_exact_selection_beats_greedy_largest_single_pnl():
    batch=[request("A",{"PUSD":"10"},8),request("B",{"PUSD":"5"},5),request("C",{"PUSD":"5"},5)]
    row=select(batch,{"PUSD":"10"},100,MODEL)
    assert row["selected_ids"]==["B","C"]
    assert row["conditional_score"]==row["score_upper_bound"]=="10"
    assert row["optimal_value_proven"] is True and row["expected_pnl"] is None
    assert select(list(reversed(batch)),{"PUSD":"10"},100,MODEL)==row


def test_vector_constraints_include_inventory_transforms_and_pending_capital():
    resources={"PUSD":"10","inventory:yes":"2","merge:condition":"1","pending_capital:pool":"10"}
    batch=[request("A",{"PUSD":"3","inventory:yes":"2","merge:condition":"1"},5),
           request("B",{"PUSD":"3","inventory:yes":"1","merge:condition":"1"},4),
           request("C",{"PUSD":"3","pending_capital:pool":"11"},10)]
    row=select(batch,resources,100,MODEL)
    assert row["selected_ids"]==["A"]
    assert row["reserved_vector"]["inventory:yes"]=="2"
    assert row["rejected"]["C"]=="INSUFFICIENT_RESOURCE_CAPACITY"


def test_missing_resource_is_unknown_not_zero_and_expired_is_excluded():
    row=select([request("A",{"inventory:y":"1"},1),request("B",{"PUSD":"1"},2,decision_available_ns=99,valid_until_ns=100)],
               {"PUSD":"10"},100,MODEL)
    assert row["rejected"]=={"A":"UNKNOWN_RESOURCE_CAPACITY","B":"EXPIRED"}


def test_duplicate_economic_portfolio_not_two_trades():
    a=request("A",{"PUSD":"1"},1)
    b={**a,"request_id":"B"}
    row=select([b,a],{"PUSD":"10"},100,MODEL)
    assert row["selected_ids"]==["A"] and row["duplicate_paths_removed"]==1
    b["selection_score"]="2"
    with pytest.raises(ValueError,match="conflicting_duplicate_portfolio"): select([a,b],{"PUSD":"10"},100,MODEL)


def test_search_budget_preserves_feasibility_and_exposes_unproven_optimum():
    batch=[request(str(i),{"PUSD":"1"},i+1) for i in range(20)]
    row=select(batch,{"PUSD":"10"},100,MODEL,maximum_nodes=2)
    assert row["search_nodes"]<=2
    assert not row["optimal_value_proven"]
    assert Fraction(row["conditional_score"])<=155<=Fraction(row["score_upper_bound"])
    assert Fraction(row["reserved_vector"].get("PUSD","0"))<=10


def test_seeded_rational_scheduler_matches_exhaustive_subsets():
    rng=random.Random(2701)
    for _ in range(250):
        n=rng.randrange(1,9)
        caps={"PUSD":str(Fraction(rng.randrange(1,12),3)),"inventory:x":str(rng.randrange(1,6))}
        batch=[request(str(i),{"PUSD":str(Fraction(rng.randrange(1,8),3)),"inventory:x":str(rng.randrange(3))},
                       Fraction(rng.randrange(1,12),7)) for i in range(n)]
        best=Fraction(0)
        for selected in itertools.product((False,True),repeat=n):
            if all(sum((Fraction(row["resources"][k]) for row,on in zip(batch,selected) if on),Fraction(0))<=Fraction(cap)
                   for k,cap in caps.items()):
                best=max(best,sum((Fraction(row["selection_score"]) for row,on in zip(batch,selected) if on),Fraction(0)))
        result=select(batch,caps,100,MODEL)
        assert result["optimal_value_proven"] and Fraction(result["conditional_score"])==best


def test_native_funding_is_pinned_and_does_not_use_future_fills(native):
    full,episode,bundles=native
    candidate=native_candidate(full,episode,bundles,MODEL)
    row=native_request(candidate)
    assert row["resources"]["PUSD"]=="1601/400"
    assert row["selection_score"]=="399/400"
    assert row["release_time_ns"] is None and row["resource_admission_verified"] is False
    assert len(row["depth_capacities"])==2
    changed=deepcopy(candidate)
    for book in changed["decision_books"].values(): book["state_version"]+=1
    changed["decision_books_sha256"]=sha(changed["decision_books"])
    assert native_request(changed)["depth_capacities"]==row["depth_capacities"]


def test_fragmentation_bound_is_tight_and_holds_for_random_partitions():
    h=Fraction(1,100000)
    assert round_fee(3*h/2,h,"VENUE_5DP")==rounding_partition_upper_bound(str(3*h/2),str(h),"VENUE_5DP")
    rng=random.Random(832)
    for _ in range(1000):
        parts=[Fraction(rng.randrange(2000),10000000) for _ in range(rng.randrange(1,30))]
        actual=sum(round_fee(x,h,"VENUE_5DP") for x in parts)
        bound=rounding_partition_upper_bound(str(sum(parts)),str(h),"VENUE_5DP")
        assert actual<=bound
    with pytest.raises(ValueError,match="fee_rounding_unknown"): rounding_partition_upper_bound("1",str(h),"UNKNOWN")


def test_persistent_holds_do_not_release_on_source_expiry_or_restart(tmp_path):
    path=tmp_path/"resources.sqlite"
    first=request("A",{"PUSD":"7"},2)
    journal=ShadowResourceJournal(path,MODEL)
    receipt=journal.admit("batch",[first],snapshot({"PUSD":"10"}),100)
    assert receipt["selected_ids"]==["A"]
    assert journal.admit("batch",[first],snapshot({"PUSD":"10"}),100)==receipt
    journal.close()
    journal=ShadowResourceJournal(path,MODEL)
    try:
        with pytest.raises(ValueError,match="snapshot_lease"):
            journal.admit("expired",[request("B",{"PUSD":"5"},4)],snapshot({"PUSD":"10"}),1000)
        later=request("B",{"PUSD":"5"},4,decision_available_ns=2000,valid_until_ns=3000)
        result=journal.admit("new",[later],snapshot({"PUSD":"10"},available_ns=2000,expires_ns=3000),2000)
        assert not result["selected_ids"] and result["holds_after"]["PUSD"]=="7"
        with pytest.raises(ValueError,match="clock_reversal"):
            journal.admit("old",[],snapshot({"PUSD":"10"}),101)
    finally: journal.close()


def test_two_writers_cannot_oversubscribe_same_shadow_balance(tmp_path):
    path=tmp_path/"journal.sqlite"
    a=ShadowResourceJournal(path,MODEL);b=ShadowResourceJournal(path,MODEL)
    try:
        assert a.admit("a",[request("A",{"PUSD":"7"},1)],snapshot({"PUSD":"10"}),100)["selected_ids"]==["A"]
        assert b.admit("b",[request("B",{"PUSD":"7"},1)],snapshot({"PUSD":"10"}),100)["selected_ids"]==[]
        with pytest.raises(ValueError,match="conflicting_reserved_request"):
            b.admit("different",[request("A",{"PUSD":"1"},1)],snapshot({"PUSD":"10"}),100)
    finally: a.close();b.close()


def test_simultaneous_sqlite_writers_serialize_reservations(tmp_path):
    path=tmp_path/"concurrent.sqlite";ready=threading.Barrier(2)
    def write(name):
        journal=ShadowResourceJournal(path,MODEL)
        try:
            ready.wait(timeout=5)
            return journal.admit(name,[request(name,{"PUSD":"7"},1)],snapshot({"PUSD":"10"}),100)
        finally: journal.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(write,("A","B")))
    assert sum(len(row["selected_ids"]) for row in rows)==1
    assert all(Fraction(row["holds_after"]["PUSD"])==7 for row in rows)


def test_failed_batch_rolls_back_every_hold_and_decision(tmp_path,monkeypatch):
    from v7_exact_arb_causal import ResourceLedger
    real=ResourceLedger.reserve
    def failing(self,identity,*args):
        if identity=="B": raise RuntimeError("injected_failure")
        return real(self,identity,*args)
    journal=ShadowResourceJournal(tmp_path/"atomic.sqlite",MODEL)
    batch=[request("A",{"PUSD":"1"},1),request("B",{"PUSD":"1"},1)]
    try:
        monkeypatch.setattr(ResourceLedger,"reserve",failing)
        with pytest.raises(RuntimeError,match="injected_failure"):
            journal.admit("batch",batch,snapshot({"PUSD":"10"}),100)
        assert journal.db.execute("SELECT count(*) FROM resource_holds").fetchone()[0]==0
        assert journal.db.execute("SELECT count(*) FROM resource_decisions").fetchone()[0]==0
        monkeypatch.setattr(ResourceLedger,"reserve",real)
        assert journal.admit("batch",batch,snapshot({"PUSD":"10"}),100)["selected_ids"]==["A","B"]
    finally: journal.close()


def test_capacity_source_shrink_does_not_free_existing_holds(tmp_path):
    journal=ShadowResourceJournal(tmp_path/"shrink.sqlite",MODEL)
    try:
        journal.admit("a",[request("A",{"PUSD":"7"},1)],snapshot({"PUSD":"10"}),100)
        with pytest.raises(ValueError,match="encumbered_financial_capacity"):
            journal.admit("b",[request("B",{"inventory:x":"1"},1)],snapshot({"PUSD":"6","inventory:x":"2"}),101)
        assert journal.db.execute("SELECT count(*) FROM resource_holds").fetchone()[0]==1
    finally: journal.close()


def test_full_depth_resource_vector_is_not_top_of_book_approximation(native):
    full,episode,bundles=native
    candidate=native_candidate(full,episode,bundles,MODEL)
    for leg in candidate["relation"]["legs"]: leg["tick_size"]="1/10000"
    for book in candidate["decision_books"].values():
        book["tick_size"]="1/10000"
        book["asks"]=[[str(Fraction(4000+i,10000)),"1/100"] for i in range(500)]
    candidate["decision_books_sha256"]=sha(candidate["decision_books"])
    row=native_request(candidate)
    assert len(row["depth_capacities"])==1000
    assert set(row["order_limits"].values())=={"4499/10000"}
    capacities={**row["depth_capacities"],"PUSD":"10"}
    receipt=select([row],capacities,row["decision_available_ns"],MODEL)
    assert receipt["selected_ids"]==[row["request_id"]]


def test_nonzero_fee_reserves_include_fragmentation_and_unwind_bounds(native):
    full,episode,bundles=native
    candidate=native_candidate(full,episode,bundles,MODEL)
    for leg in candidate["relation"]["legs"]: leg["fee_rate"]="1/50"
    for book in candidate["decision_books"].values(): book["fee_rate"]="1/50"
    candidate["decision_books_sha256"]=sha(candidate["decision_books"])
    row=native_request(candidate)
    assert Fraction(row["funding"]["entry_fee_upper_bound"])==Fraction(8,125)
    assert Fraction(row["funding"]["unwind_fee_upper_bound"])==Fraction(1,15)
    assert Fraction(row["resources"]["PUSD"])==4+Fraction(8,125)+Fraction(1,15)+Fraction(1,400)


@pytest.mark.parametrize("mutate",[
    lambda r:r.update(decision_available_ns=101),lambda r:r.update(paper_only=False),
    lambda r:r.update(selection_score="nan"),lambda r:r.update(resources={"PUSD":"-1"}),
    lambda r:r.update(resources={"PUSD":1.0}),lambda r:r.update(selection_score=str(2**300))])
def test_bad_inputs_never_enter_optimizer(mutate):
    row=request("A",{"PUSD":"1"},1);mutate(row)
    with pytest.raises(ValueError): select([row],{"PUSD":"10"},100,MODEL)
