"""Pinned N-leg orders, causal unwind and no synthetic venue fill guarantees."""
from collections import Counter, defaultdict
from fractions import Fraction

import pytest

from test_v7_exact_arb_adversarial import book, opportunity, relation
from v7_exact_arb_causal import CausalBooks
from v7_unified_exact_arb_graph import GraphError, sha, fstr
from v7_unified_exact_arb_graph_execution_shadow import fill, simulate, record_summary
from v7_exact_arb_execution_timing import timing_plan, normalize_delay_profile


def history(n=3, arrival=None, later=None):
    result = CausalBooks()
    result.ingest(100, {str(i):book(100) for i in range(n)})
    result.ingest(101, arrival or {str(i):book(101) for i in range(n)})
    if later is not None:
        result.ingest(102, later)
    result.ingest(200, {})
    return result


def test_hold_is_added_to_wire_latency_and_uses_matching_time_book():
    books=CausalBooks(); books.ingest(100,{str(i):book(100) for i in range(2)})
    books.ingest(101,{str(i):book(101) for i in range(2)})
    books.ingest(350,{str(i):book(350,price=".3") for i in range(2)})
    books.ingest(400,{})
    result=simulate(opportunity(relation(2)),books,"PARALLEL",1,0,
                    venue_delay_ms_by_token={"0":250,"1":250})
    assert result["state"]=="NO_LEGS_FILLED"
    for row in result["legs"]:
        assert row["submission_timestamp_ms"]==100
        assert row["arrival_timestamp_ms"]==row["wire_arrival_timestamp_ms"]==101
        assert row["match_timestamp_ms"]==351
        assert row["book_timestamp_ms"]==350 and row["book_age_ms"]=="1"
    assert result["mandatory_venue_delay_verified"] is False


def test_different_holds_match_out_of_order_without_mixing_unwind_tokens():
    books=CausalBooks(); books.ingest(100,{str(i):book(100) for i in range(3)})
    arrived={"0":book(101),"1":book(101,size="1"),"2":book(101)}
    books.ingest(101,arrived); books.ingest(150,{})
    result=simulate(opportunity(),books,"PARALLEL",1,0,
                    venue_delay_ms_by_token={"0":10,"1":0,"2":5},ack_delay_ms=3)
    assert result["state"]=="PARTIAL_UNWOUND"
    assert [(r["token_id"],r["match_timestamp_ms"]) for r in result["legs"]]==[("1",101),("2",106),("0",111)]
    assert [(r["token_id"],r["match_timestamp_ms"]) for r in result["unwind_legs"]]==[("1",116),("2",121),("0",126)]
    assert [r["filled_size"] for r in result["unwind_legs"]]==["1","2","2"]
    assert all(r["submission_timestamp_ms"]==114 for r in result["unwind_legs"])
    assert result["timestamp_ms"]==129


def test_sequential_waits_for_hold_and_modeled_response_before_next_submission():
    plan=timing_plan(["a","b","c"],100,"SEQUENTIAL",1,2,4,{"a":10,"b":0,"c":5},3)
    assert [r["submission_ms"] for r in plan["legs"]]==[100,116,122]
    assert [r["wire_arrival_ms"] for r in plan["legs"]]==[101,117,123]
    assert [r["match_ms"] for r in plan["legs"]]==[111,117,128]
    assert plan["entry_result_ms"]==131 and plan["finish_ms"]==148


def test_unknown_token_delay_is_censored_not_assumed_zero():
    result=simulate(opportunity(),history(),"PARALLEL",1,0,venue_delay_ms_by_token={"0":250})
    assert result["state"]=="CENSORED" and result["reason"]=="execution_mandatory_delay_unknown"
    assert result["legs"]==[] and result["net_locked_pnl"] is None


def test_unconfigured_zero_hold_is_labeled_upper_bound_and_has_distinct_identity():
    op=opportunity(); books=history()
    upper=simulate(op,books,"PARALLEL",1,0)
    explicit=simulate(op,books,"PARALLEL",1,0,venue_delay_ms_by_token={str(i):0 for i in range(3)})
    assert upper["venue_delay_policy"]=="ZERO_HOLD_UPPER_BOUND_UNVERIFIED"
    assert explicit["venue_delay_policy"]=="EXPLICIT_PER_TOKEN_SCENARIO"
    assert upper["cycle_id"]!=explicit["cycle_id"]
    modes,arms=defaultdict(Counter),{}
    record_summary(upper,modes,arms); record_summary(explicit,modes,arms)
    assert len(arms)==2


@pytest.mark.parametrize("profile",[{},[],{"0":None},{"0":True},{"0":-1},{"0":60001},
                                   {"0":"1/10000000"},{1:0},{"":0},{str(i):0 for i in range(129)}])
def test_invalid_hold_profiles_fail_closed(profile):
    with pytest.raises(GraphError): normalize_delay_profile(profile)


def test_missing_late_hold_book_retains_earlier_known_fill():
    result=simulate(opportunity(relation(2)),history(2),"PARALLEL",1,0,
                    venue_delay_ms_by_token={"0":250,"1":0})
    assert result["state"]=="CENSORED" and result["reason"]=="observation_watermark"
    assert result["known_entry_fills"]=={"1":"2"}


def test_unwind_limit_is_frozen_before_token_hold_not_reselected_at_match():
    books=CausalBooks(); books.ingest(100,{str(i):book(100) for i in range(2)})
    books.ingest(101,{"0":book(101),"1":book(101,price=".3")})
    changed=book(120); changed["bids"]=[[".1","10"]]
    books.ingest(120,{"0":changed}); books.ingest(150,{})
    result=simulate(opportunity(relation(2)),books,"BATCH",1,0,
                    venue_delay_ms_by_token={"0":10,"1":0})
    assert result["state"]=="EXPOSURE_REMAINS"
    row=result["unwind_legs"][0]
    assert row["submission_timestamp_ms"]==111 and row["wire_arrival_timestamp_ms"]==113
    assert row["match_timestamp_ms"]==123
    assert Fraction(row["limit_price"])==Fraction(".19") and row["filled_size"]=="0"


def test_result_delay_does_not_invent_observation_beyond_tape():
    books=CausalBooks(); books.ingest(100,{str(i):book(100) for i in range(2)}); books.ingest(102,{})
    result=simulate(opportunity(relation(2)),books,"PARALLEL",1,0,ack_delay_ms=2)
    assert result["state"]=="CENSORED" and result["reason"]=="observation_watermark"
    assert result["known_entry_fills"]=={"0":"2","1":"2"}


def test_seeded_n_leg_timing_matches_chronological_queries_and_never_future_books():
    import random
    rng=random.Random(20260924)
    for _ in range(100):
        count=rng.randint(2,16); mode=rng.choice(["PARALLEL","BATCH","SEQUENTIAL"])
        if count==16 and mode=="BATCH": mode="PARALLEL"
        tokens=[str(i) for i in range(count)]
        profile={token:rng.randint(0,5) for token in tokens}
        delay,skew,ack=rng.randint(0,2),rng.randint(0,2),rng.randint(0,2)
        plan=timing_plan(tokens,100,mode,delay,skew,2,profile,ack)
        books=CausalBooks()
        for t in range(100,int(plan["finish_ms"])+2):
            books.ingest(t,{token:book(t) for token in tokens})
        result=simulate(opportunity(relation(count)),books,mode,delay,skew,
                        venue_delay_ms_by_token=profile,ack_delay_ms=ack,maximum_skew_ms=1000)
        assert result["state"]=="ALL_LEGS_FILLED"
        matches=[row["match_timestamp_ms"] for row in result["legs"]]
        assert matches==sorted(matches)
        for row in result["legs"]:
            expected=plan["legs"][row["leg_index"]]
            assert row["token_id"]==expected["token_id"]
            assert row["submission_timestamp_ms"]<=row["wire_arrival_timestamp_ms"]<=row["match_timestamp_ms"]
            assert row["book_observation_ms"]<=row["match_timestamp_ms"]
            assert row["match_timestamp_ms"]==expected["match_ms"]
        assert result["economic_admission"]=="NON_EXECUTABLE_UNVERIFIED_VENUE_TERMS"


def test_each_fok_is_independent_and_batch_is_not_atomic():
    books = history(arrival={str(i):book(101,size="1" if i == 2 else "10") for i in range(3)})
    fok = simulate(opportunity(),books,"BATCH",1,0,order_type="FOK")
    fak = simulate(opportunity(),books,"BATCH",1,0,order_type="FAK")
    assert [r["filled_size"] for r in fok["legs"]] == ["2", "2", "0"]
    assert [r["filled_size"] for r in fak["legs"]] == ["2", "2", "1"]
    assert fok["state"] == fak["state"] == "PARTIAL_UNWOUND"
    assert fok["atomic_batch"] is False
    assert fok["cycle_id"] != fak["cycle_id"]
    assert not fok["venue_execution_verified"]


def test_fok_no_fill_does_not_consume_liquidity():
    depleted = defaultdict(Fraction)
    leg = relation()["legs"][0]
    short = book(100,size="1")
    no = fill(leg,short,Fraction(2),"BUY",100,100,depleted,Fraction(".2"),"FOK")
    assert no["filled_size"] == "0"
    yes = fill(leg,short,Fraction(1),"BUY",100,100,depleted,Fraction(".2"),"FOK")
    assert yes["filled_size"] == "1"


def test_repeated_observation_does_not_replenish_same_version():
    depleted = defaultdict(Fraction)
    leg = relation()["legs"][0]
    state = {**book(100,size="2"), "state_version": 4}
    fill(leg,state,Fraction(2),"BUY",100,100,depleted,Fraction(".2"))
    repeated = {**state,"observation_ms":101}
    result = fill(leg,repeated,Fraction(2),"BUY",101,100,depleted,Fraction(".2"))
    assert result["filled_size"] == "0"


def test_limit_pins_worst_consumed_decision_level_not_worst_future_level():
    books = CausalBooks()
    initial = {str(i):book(100) for i in range(3)}
    initial["0"]["asks"] = [[".2","1"],[".21","1"],[".5","10"]]
    books.ingest(100, initial)
    arrived = {str(i):book(101) for i in range(3)}
    arrived["0"]["asks"] = [[".19","1"],[".22","9"]]
    arrived["0"]["bids"] = [[".18","10"]]
    books.ingest(101, arrived); books.ingest(200,{})
    op = opportunity(); op.update(decision_books=initial,decision_books_sha256=sha(initial))
    result = simulate(op,books,"PARALLEL",1,0)
    assert Fraction(result["legs"][0]["limit_price"]) == Fraction(".21")
    assert result["legs"][0]["filled_size"] == "1"
    assert result["legs"][0]["fills"] == [["19/100", "1"]]


def test_all_missed_orders_do_not_invent_reserve_expense_or_unwind():
    books = history(arrival={str(i):book(101,price=".3") for i in range(3)})
    result = simulate(opportunity(),books,"PARALLEL",1,0)
    assert result["state"] == "NO_LEGS_FILLED"
    assert result["realized_counterfactual_pnl"] == "0"
    assert result["unwind_required"] is False


def test_unwind_limit_cannot_follow_favorable_or_unfavorable_future_price():
    arrived = {str(i):book(101,size="1" if i == 2 else "10") for i in range(3)}
    later = {str(i):book(102) for i in range(3)}
    later["0"]["bids"] = [[".1","10"]]
    result = simulate(opportunity(),history(arrival=arrived,later=later),"BATCH",1,0)
    assert result["state"] == "EXPOSURE_REMAINS"
    assert result["unhedged_exposure"] == {"0":"2"}
    unwind = result["unwind_legs"][0]
    assert Fraction(unwind["limit_price"]) == Fraction(".19")
    assert unwind["submission_timestamp_ms"] == 101 and unwind["arrival_timestamp_ms"] == 103
    assert unwind["filled_size"] == "0"
    assert result["realized_counterfactual_pnl"] is None


def test_subquantum_partial_fill_residual_is_not_faked_as_liquidatable():
    arrived = {"0":book(101,size="1.005"),"1":book(101,price=".3")}
    r = relation(2)
    r["states"] = ["a","b"]
    for i,leg in enumerate(r["legs"]):
        leg.update(minimum_order="1",payout_vector=[int(i==0),int(i==1)])
    result = simulate(opportunity(r),history(2,arrived),"BATCH",1,0)
    assert result["state"] == "EXPOSURE_REMAINS"
    assert Fraction(result["unhedged_exposure"]["0"]) == Fraction(".005")
    assert list(map(Fraction,result["residual_state_payoffs"]["payoffs"])) == [Fraction(".005"),0]
    assert result["realized_counterfactual_pnl"] is None


def test_minimum_order_blocks_dust_unwind():
    arrived = {"0":book(101,size=".9"),"1":book(101,price=".3")}
    r = relation(2)
    for leg in r["legs"]: leg["minimum_order"] = "1"
    result = simulate(opportunity(r),history(2,arrived),"BATCH",1,0)
    assert result["unwind_legs"][0]["state"] == "NON_EXECUTABLE_DUST"
    assert Fraction(result["unhedged_exposure"]["0"]) == Fraction(".9")


def test_batch_capacity_does_not_disallow_parallel_sixteen_leg_research():
    batch = simulate(opportunity(relation(16)),history(16),"BATCH",1,0)
    parallel = simulate(opportunity(relation(16)),history(16),"PARALLEL",1,0)
    assert batch["state"] == "CENSORED" and batch["reason"] == "batch_order_capacity"
    assert len(parallel["legs"]) == 16


@pytest.mark.parametrize("change,reason", [
    ({"tick_size":None}, "execution_tick_unknown"),
    ({"coefficient":"1/3"}, "execution_share_precision"),
    ({"minimum_order":None}, "execution_minimum"),
    ({"fee_rate":".07"}, "arrival_fee_rounding_unknown"),
])
def test_unknown_or_incompatible_order_terms_censor(change,reason):
    op = opportunity()
    op["relation"]["legs"][0].update(change)
    if change.get("coefficient") == "1/3":
        op["relation"]["legs"][0]["payout_vector"][0] = 3
    result = simulate(op,history(),"BATCH",1,0)
    assert result["state"] == "CENSORED"
    assert reason in result["reason"]
    assert result["net_locked_pnl"] is None


def test_venue_fee_rounding_is_explicit_and_preserved():
    op = opportunity()
    for leg in op["relation"]["legs"]:
        leg.update(fee_rate=".07",fee_rounding_increment=".00001",fee_rounding_mode="VENUE_5DP")
    result = simulate(op,history(),"BATCH",1,0)
    assert result["state"] == "ALL_LEGS_FILLED"
    assert all(Fraction(leg["fee"]) == Fraction(".0224") for leg in result["legs"])
    assert result["realized_counterfactual_pnl"] is None


def test_sell_limits_and_inventory_boundary():
    op = opportunity(relation(2)); op["result"]["direction"] = "SELL"
    with pytest.raises(GraphError,match="execution_inventory"):
        simulate(op,history(2),"BATCH",1,0)
    op["inventory_reserved"] = True
    arrived = {str(i):book(101) for i in range(2)}
    arrived["1"]["bids"] = [[".18","10"]]
    result = simulate(op,history(2,arrived),"BATCH",1,0)
    assert [leg["filled_size"] for leg in result["legs"]] == ["2","0"]
    assert result["state"] == "PARTIAL_UNWOUND"


def test_history_is_immutable_at_ingest_and_lookup():
    books = CausalBooks()
    source = book(100)
    books.ingest(100,{"0":source})
    source["asks"][0][0] = ".9"
    assert books.at("0",100)["asks"][0][0] == ".2"
    books.at("0",100)["asks"][0][0] = ".8"
    assert books.at("0",100)["asks"][0][0] == ".2"
    assert books.at("0",99) is None


def test_censored_later_leg_preserves_known_fills_not_zero_risk():
    arrived = {str(i):book(101) for i in range(3)}
    arrived["1"]["depth_truncated"] = True
    result = simulate(opportunity(),history(arrival=arrived),"BATCH",1,0)
    assert result["state"] == "CENSORED"
    assert result["known_entry_fills"] == {"0":"2"}
    assert result["residual_exposure_verified"] is False
    assert result["realized_counterfactual_pnl"] is None


def test_latency_arms_do_not_sum_alternative_worlds_as_profit():
    modes, arms = defaultdict(Counter), {}
    one = simulate(opportunity(),history(),"PARALLEL",1,0)
    two = simulate(opportunity(),history(),"PARALLEL",2,0)
    record_summary(one,modes,arms); record_summary(two,modes,arms)
    assert len(arms) == 2
    assert modes["PARALLEL"]["sum_pnl_after_reserve"] is None
    assert all(a["locked_pnl_observations"] == 1 for a in arms.values())
    assert all(a["closed_pnl_observations"] == 0 for a in arms.values())
    assert all(a["sum_closed_counterfactual_pnl"] is None for a in arms.values())


def test_decision_limits_are_not_reselected_from_later_same_millisecond_book():
    op = opportunity()
    books = CausalBooks()
    books.ingest(100,{str(i):book(100,price=".3") for i in range(3)})
    books.ingest(200,{})
    result = simulate(op,books,"PARALLEL",1,0)
    assert result["state"] == "NO_LEGS_FILLED"
    assert all(Fraction(leg["limit_price"]) == Fraction(".2") for leg in result["legs"])


def test_missing_or_altered_decision_books_cannot_fall_back_to_history():
    op = opportunity(); del op["decision_books"]
    result = simulate(op,history(),"BATCH",1,0)
    assert result["reason"] == "decision_books_missing_or_digest"
    op = opportunity(); op["decision_books"]["0"]["asks"][0][0] = ".8"
    assert simulate(op,history(),"BATCH",1,0)["reason"] == "decision_books_missing_or_digest"


def test_false_payoff_and_lowered_reserve_do_not_make_positive_pnl():
    op = opportunity(); op["relation"]["legs"][0]["payout_vector"][0] = 0
    result = simulate(op,history(),"BATCH",1,0)
    assert result["state"] == "CENSORED" and result["net_locked_pnl"] is None
    op = opportunity(); op["relation"]["reserve_per_unit"] = "0"
    assert simulate(op,history(),"BATCH",1,0)["reason"] == "execution_reserve_below_baseline"


def test_fractional_native_time_does_not_truncate_decision_or_read_future_book():
    op = opportunity(); op["timestamp_ms"] = "100000001/1000000"
    books = CausalBooks(); books.ingest(100,{str(i):book(100) for i in range(3)})
    before = Fraction(1010000005,10000000)
    after = Fraction(101000002,1000000)
    books.ingest(before,{str(i):book(fstr(before),price=".3") for i in range(3)})
    books.ingest(after,{str(i):book(fstr(after),price=".1") for i in range(3)})
    books.ingest(200,{})
    result = simulate(op,books,"PARALLEL",1,0)
    assert result["state"] == "NO_LEGS_FILLED"
    assert all(Fraction(row["arrival_timestamp_ms"]) == Fraction(101000001,1000000) for row in result["legs"])
    assert all(Fraction(row["book_timestamp_ms"]) == before for row in result["legs"])


def test_crossed_arrival_book_is_data_defect_not_cheap_executable_liquidity():
    arrived={str(i):book(101) for i in range(3)}
    arrived["0"]["bids"]=[[".21","10"]]
    result=simulate(opportunity(),history(arrival=arrived),"BATCH",1,0)
    assert result["state"] == "CENSORED" and result["reason"] == "arrival_crossed_book"
