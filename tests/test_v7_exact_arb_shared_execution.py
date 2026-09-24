from collections import defaultdict
from copy import deepcopy
from fractions import Fraction

import pytest

from test_v7_exact_arb_adversarial import book, opportunity, relation
from test_v7_exact_arb_execution_orders import history
from v7_exact_arb_causal import CausalBooks
from v7_exact_arb_shared_execution import SharedDepthDebit, SharedExecutionQueue
from v7_unified_exact_arb_graph import GraphError, sha
from v7_unified_exact_arb_graph_execution_shadow import fill, simulate


ARM={"mode":"PARALLEL","delay_ms":"1","skew_ms":"0","unwind_delay_ms":"2","order_type":"FAK"}


def op(identifier,when=100):
    row=opportunity(relation(2));row.update(opportunity_id=identifier,timestamp_ms=when)
    return row


def run_jobs(jobs,books,arm=ARM):
    rows=[];trace=[]
    venue=SharedExecutionQueue(arm,"test-world",rows.append,before_event=trace.append)
    for row in jobs: venue.add(row,books)
    venue.advance(books.watermark)
    venue.finish()
    return rows,trace,venue.receipt()


def test_other_cycles_cannot_reuse_the_same_displayed_shares():
    books=history(2,arrival={str(i):book(101,size="2") for i in range(2)})
    assert simulate(op("a"),books,"PARALLEL",1,0)["state"]=="ALL_LEGS_FILLED"
    assert simulate(op("b"),books,"PARALLEL",1,0)["state"]=="ALL_LEGS_FILLED"
    rows,_,receipt=run_jobs([op("b"),op("a")],books)
    assert {row["opportunity_id"]:row["state"] for row in rows}=={"a":"ALL_LEGS_FILLED","b":"NO_LEGS_FILLED"}
    assert receipt["portfolio_net_pnl"] is None
    again,_,other=run_jobs([op("a"),op("b")],books)
    assert again==rows and other==receipt


def test_sequential_cycles_interleave_by_arrival_not_by_cycle_completion():
    books=history(2,arrival={str(i):book(101,size="3") for i in range(2)})
    arm={**ARM,"mode":"SEQUENTIAL","skew_ms":"2"}
    rows,trace,receipt=run_jobs([op("a"),op("b",102)],books,arm)
    assert trace[:4]==[101,103,104,106]
    a=next(r for r in rows if r["opportunity_id"]=="a")
    b=next(r for r in rows if r["opportunity_id"]=="b")
    assert [r["filled_size"] for r in a["legs"]]==["2","2"]
    assert [r["filled_size"] for r in b["legs"]]==["1","1"]
    assert b["state"]=="PARTIAL_UNWOUND"


def test_different_token_holds_are_event_ordered_in_shared_world():
    books=history(2,arrival={str(i):book(101,size="2") for i in range(2)})
    arm={**ARM,"venue_delay_ms_by_token":{"0":10,"1":0},"ack_delay_ms":2}
    rows,trace,receipt=run_jobs([op("a"),op("b",102)],books,arm)
    assert trace==sorted(trace)
    assert trace[:4]==[101,103,111,113]
    found={row["opportunity_id"]:row for row in rows}
    assert found["a"]["state"]=="ALL_LEGS_FILLED"
    assert found["b"]["state"]=="NO_LEGS_FILLED"
    assert [r["token_id"] for r in found["a"]["legs"]]==["1","0"]
    assert receipt["censored_world"] is None


def test_book_version_or_lineage_change_does_not_reset_debit():
    debit=SharedDepthDebit();leg=relation(2)["legs"][0]
    first={**book(100,size="2"),"state_version":1}
    assert fill(leg,first,Fraction(2),"BUY",100,100,debit,Fraction(1,5))["filled_size"]=="2"
    second={**book(101,size="2",lineage="new"),"state_version":99}
    assert fill(leg,second,Fraction(2),"BUY",101,100,debit,Fraction(1,5))["filled_size"]=="0"


def test_observed_absence_only_resets_complete_valid_side():
    debit=SharedDepthDebit();key=("x","asks",("a",1),".2")
    debit[key]=2
    for flags in ({"valid":False},{"continuous":False},{"ask_truncated":True}):
        options={"valid":True,"continuous":True,"ask_truncated":False,"bid_truncated":False,**flags}
        debit.observe("x",[],[],**options)
        assert debit[key]==2
    debit.observe("x",[[".18","5"]],[[".19","5"]],valid=True,continuous=True,ask_truncated=False,bid_truncated=False)
    assert debit[key]==2
    debit.observe("x",[],[],valid=True,continuous=True,ask_truncated=False,bid_truncated=False)
    assert debit[key]==0 and debit.observed_zero_resets==1


def test_newly_observed_size_does_not_restore_previously_consumed_size():
    debit=SharedDepthDebit();leg=relation(2)["legs"][0]
    fill(leg,book(100,size="2"),Fraction(2),"BUY",100,100,debit,Fraction(1,5))
    newer=book(101,size="3")
    debit.observe("0",newer["asks"],newer["bids"],valid=True,continuous=True,ask_truncated=False,bid_truncated=False)
    assert fill(leg,newer,Fraction(2),"BUY",101,100,debit,Fraction(1,5))["filled_size"]=="1"


def test_future_missing_leg_does_not_hide_an_earlier_fill():
    books=CausalBooks();books.ingest(100,{str(i):book(100) for i in range(2)})
    books.ingest(102,{"1":book(102,lineage="reset")});books.ingest(110,{})
    result=simulate(op("a"),books,"SEQUENTIAL",1,1)
    assert result["state"]=="CENSORED"
    assert result["known_entry_fills"]=={"0":"2"}
    assert result["realized_counterfactual_pnl"] is None


def test_incomplete_tape_preserves_known_entry_fills():
    books=CausalBooks();books.ingest(100,{str(i):book(100) for i in range(2)});books.ingest(102,{})
    result=simulate(op("a"),books,"SEQUENTIAL",1,2)
    assert result["reason"]=="observation_watermark"
    assert result["known_entry_fills"]=={"0":"2"}


def test_complete_fill_does_not_wait_for_a_nonexistent_unwind():
    books=CausalBooks();books.ingest(100,{str(i):book(100) for i in range(2)});books.ingest(102,{})
    result=simulate(op("a"),books,"PARALLEL",1,0)
    assert result["state"]=="ALL_LEGS_FILLED"
    assert result["timestamp_ms"]==result["entry_completion_timestamp_ms"]==101
    assert result["scheduled_unwind_horizon_ms"]==103


def test_censored_order_taints_later_shared_fills_not_earlier_ones():
    books=history(2)
    bad=op("bad",102);bad["relation"]["reserve_per_unit"]="0"
    rows,_,receipt=run_jobs([bad,op("a"),op("later",104)],books)
    found={row["opportunity_id"]:row for row in rows}
    assert found["a"]["state"]=="ALL_LEGS_FILLED"
    assert found["later"]["state"]=="CENSORED"
    assert found["later"]["reason"]=="shared_venue_uncertain_after_censored_order"
    assert receipt["censored_world"]["timestamp_ms"]=="102"


def test_strict_watermark_and_final_censoring():
    rows=[];venue=SharedExecutionQueue(ARM,"world",rows.append)
    venue.add(op("a"),history(2))
    venue.advance(101)
    assert not rows
    venue.finish()
    assert rows[0]["state"]=="CENSORED" and rows[0]["reason"]=="observation_watermark"
    assert venue.receipt()["pending_jobs"]==0


def test_pending_capacity_and_duplicate_ids_fail_closed():
    venue=SharedExecutionQueue(ARM,"world",lambda row:None,maximum_pending=1)
    venue.add(op("a"),history(2))
    with pytest.raises(GraphError,match="duplicate_shared"): venue.add(op("a"),history(2))
    with pytest.raises(GraphError,match="shared_pending_capacity"): venue.add(op("b"),history(2))
