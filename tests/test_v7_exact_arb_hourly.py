"""Hourly diagnostics cannot create independent trades, coverage or profit."""
from collections import Counter
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_v7_exact_arb_native_evidence import data, MODEL, change
from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_arrival import inputs
from test_v7_exact_arb_native_run import study
from v7_exact_arb_native_evidence import Evidence, canonical
from v7_exact_arb_sota_report import LatencyEvidence
from v7_exact_arb_hourly import HourlyStudy, HOUR_NS
from v7_exact_arb_native_run import run


def at(row,seq,stamp,positive=True,**extra):
    return change(row,seq,positive,feed_frame_sequence=seq,decision_start_ns=stamp,
        decision_end_ns=stamp+1,receive_monotonic_ns=stamp,
        evaluation_valid_until_monotonic_ns=10000*HOUR_NS,source_valid_until_monotonic_ns=10001*HOUR_NS,**extra)


def reduce(data,rows,frames=None,transitions=()):
    directory,_,_=data
    evidence=Evidence(":memory:",directory,MODEL)
    db=sqlite3.connect(":memory:");replay=sqlite3.connect(":memory:")
    try:
        replay.execute("CREATE TABLE replay(seq INTEGER PRIMARY KEY,available INTEGER,wire TEXT)")
        if frames is None:
            frames=[{"seq":row["feed_frame_sequence"],"receive_monotonic_ns":row["receive_monotonic_ns"],
                "availability_monotonic_ns":row["receive_monotonic_ns"],"graph_decision_start_ns":row["decision_start_ns"],
                "source_frame_valid":True,"connection_epoch":1} for row in rows]
        for frame in frames:
            replay.execute("INSERT OR IGNORE INTO replay VALUES(?,?,?)",
                (frame["seq"],frame["availability_monotonic_ns"],canonical(frame)))
        archive=SimpleNamespace(db=replay)
        latency=LatencyEvidence(db,archive);hourly=HourlyStudy(db,archive)
        db.execute("CREATE TABLE capital_transitions(id TEXT,body TEXT)")
        for index,transition in enumerate(transitions):
            db.execute("INSERT INTO capital_transitions VALUES(?,?)",(str(index),canonical(transition)))
        for row in rows:
            before=evidence.counts["missing_observation_rows"]
            delta=evidence.ingest(row)
            if delta is not None:
                latency.ingest(row)
                hourly.ingest(row,*delta,evidence.bundles.relation(row),evidence.counts["missing_observation_rows"]-before)
        diagnostics=evidence.finish();latency.finish()
        return list(hourly.rows("study",MODEL,"session-a","b"*64)),diagnostics
    finally: evidence.close();db.close();replay.close()


def test_continuous_episode_crossing_hours_is_not_a_second_opportunity(data):
    _,row,family=data
    hours,all_rows=reduce(data,[at(row,1,HOUR_NS-30,False),at(row,2,HOUR_NS-20),
                              at(row,3,HOUR_NS+10),at(row,4,HOUR_NS+20,False)])
    assert len(hours)==2
    assert [h["families"][family]["evaluation_funnel"]["raw_observed_starts"] for h in hours]==[1,0]
    totals=Counter()
    for h in hours: totals.update(h["families"][family]["evaluation_funnel"])
    assert all(totals[k]==v for k,v in all_rows["families"][family]["evaluation_funnel"].items())
    assert all(h["economic_evidence"]["fully_filled_opportunities_per_hour"] is None for h in hours)


def test_emission_availability_not_decision_start_assigns_hour(data):
    _,row,family=data
    observation=at(row,1,HOUR_NS-1);observation["decision_end_ns"]=HOUR_NS+1
    hours,_=reduce(data,[observation])
    assert [h["engineering_evidence"]["recorded_frames"] for h in hours]==[1,0]
    assert [h["engineering_evidence"]["recorded_evaluations"] for h in hours]==[0,1]
    assert hours[1]["families"][family]["evaluation_funnel"]["raw_observed_starts"]==0


def test_empty_intervening_hours_are_missing_not_observed_zero(data):
    _,row,_=data
    hours,_=reduce(data,[at(row,1,10),at(row,2,3*HOUR_NS+10)])
    assert len(hours)==4
    for h in hours[1:3]:
        assert h["data_state"]=="NO_RECORDED_ROWS_NOT_OBSERVED_ZERO_ACTIVITY"
        assert h["families"]=={} and h["runtime_health"] is None and h["causal_exchange_coverage"] is None
        assert h["engineering_evidence"]["missing_frames_detected"] is None
        assert h["engineering_evidence"]["native_observations_dropped"] is None
        assert all(v["samples"]==0 and all(q is None for q in v["quantiles_ns"].values()) for v in h["latency_attribution"].values())
    assert hours[0]["recorded_clock_reached_window_end"] and not hours[-1]["recorded_clock_reached_window_end"]


def test_sequence_loss_attributed_when_detected_not_fabricated_loss_hour(data):
    _,row,family=data
    hours,_=reduce(data,[at(row,1,10,False),at(row,4,2*HOUR_NS+10)])
    assert hours[2]["engineering_evidence"]["missing_observations_detected"]==2
    assert hours[2]["engineering_evidence"]["missing_frames_detected"]==2
    assert hours[1]["engineering_evidence"]["missing_frames_detected"] is None
    assert hours[2]["families"][family]["evaluation_funnel"]["raw_observed_starts"]==0


def test_duplicate_rows_do_not_change_hourly_statistics(data):
    _,row,_=data
    rows=[at(row,1,10,False),at(row,2,20)]
    unique,_=reduce(data,rows)
    repeated,_=reduce(data,rows+rows)
    assert unique==repeated


def test_near_quantiles_remain_exact_beyond_binary64_integer_precision(data):
    _,row,family=data
    rows=[at(row,1,10,False),at(row,2,20,False)]
    for r,d in zip(rows,(2**53+1,2**53)):
        r["near_arbitrage"]["raw_distance_nano"]=d
        r["near_arbitrage"]["actionable_tick_nano"]=1
    hours,_=reduce(data,rows)
    metric=hours[0]["families"][family]["near_arbitrage"]["raw"]
    assert metric["quantiles"]["ticks"]["0"]==str(2**53)
    assert Fraction(metric["quantiles"]["pusd"]["0"])==Fraction(2**53,1000000000)


def test_invalid_diagnostic_has_no_invented_distance_sample(data):
    _,row,family=data
    observation=at(row,1,10,False);observation["near_arbitrage"]["valid"]=False
    hours,_=reduce(data,[observation])
    metric=hours[0]["families"][family]["near_arbitrage"]["raw"]
    assert metric["observations"]==0
    assert all(q is None for values in metric["quantiles"].values() for q in values.values())
    assert all(value is None for value in metric["fraction_within_ticks"].values())


def test_missing_raw_frame_censors_latency_only_not_assumed_zero_elapsed(data):
    _,row,_=data
    hours,_=reduce(data,[at(row,1,10)],frames=[])
    assert hours[0]["engineering_evidence"]["missing_raw_frame_observations"]==1
    assert all(s["samples"]==0 for s in hours[0]["latency_attribution"].values())


def test_wall_clock_reversal_never_reorders_monotonic_hours(data):
    _,row,_=data
    rows=[at(row,1,HOUR_NS-10),at(row,2,HOUR_NS+10)]
    frames=[{"seq":r["observation_sequence"],"receive_monotonic_ns":r["receive_monotonic_ns"],
        "availability_monotonic_ns":r["receive_monotonic_ns"],"graph_decision_start_ns":r["decision_start_ns"],
        "source_frame_valid":True,"connection_epoch":1,"receive_wall_ms":wall} for r,wall in zip(rows,(20000,10000))]
    hours,_=reduce(data,rows,frames)
    assert [h["hour_index"] for h in hours]==[0,1]
    assert hours[1]["reported_host_wall_clock"]["backward_transitions_detected"]==1
    assert all(h["reported_host_wall_clock"]["utc_alignment_verified"] is False for h in hours)


def test_completely_empty_study_has_no_fabricated_hour(data):
    assert reduce(data,[])[0]==[]


def test_hour_span_has_an_explicit_bound(data):
    _,row,_=data
    with pytest.raises(ValueError,match="hourly_window_budget"):
        reduce(data,[at(row,1,10),at(row,2,4096*HOUR_NS+10)])


def test_results_belong_to_known_result_hour_not_earlier_opportunity_hour(data):
    _,row,_=data
    transition={"world_id":"world-a","available_ns":HOUR_NS+1,"execution_state":"PARTIAL_UNWOUND",
        "modeled_orders_closed":True,"modeled_flat_net_pnl":"-1/10","reservation_pusd_seconds":"1/2"}
    second={**transition,"world_id":"world-b","modeled_flat_net_pnl":"-3/10"}
    hours,_=reduce(data,[at(row,1,HOUR_NS-10),at(row,2,HOUR_NS+10)],transitions=[transition,second])
    assert hours[0]["shared_result_worlds"]=={}
    worlds=hours[1]["shared_result_worlds"]
    assert worlds["world-a"]["modeled_flat_net_pnl"]=="-1/10"
    assert worlds["world-b"]["modeled_flat_net_pnl"]=="-3/10"
    assert all(w["portfolio_net_pnl"] is None for w in worlds.values())


def test_unobserved_future_result_cannot_leak_into_hourly_report(data):
    _,row,_=data
    with pytest.raises(ValueError,match="hourly_future_capital_result"):
        reduce(data,[at(row,1,10)],transitions=[{"available_ns":HOUR_NS,"world_id":"world"}])


def test_result_before_capture_cannot_disappear_from_attribution(data):
    _,row,_=data
    with pytest.raises(ValueError,match="hourly_pre_capture_capital_result"):
        reduce(data,[at(row,1,HOUR_NS+10)],transitions=[{"available_ns":1,"world_id":"world"}])


def test_emission_clock_reversal_cannot_reassign_prior_knowledge(data):
    _,row,_=data
    first=at(row,1,10,False);first["decision_end_ns"]=HOUR_NS+1
    with pytest.raises(ValueError,match="hourly_emission_time_reversal"):
        reduce(data,[first,at(row,2,20)])


def test_invalid_frame_without_diagnostics_does_not_claim_graph_health(data):
    frame={"seq":1,"receive_monotonic_ns":10,"availability_monotonic_ns":10,"source_frame_valid":False}
    hours,_=reduce(data,[],frames=[frame])
    assert hours[0]["engineering_evidence"]["invalid_frames"]==1
    assert hours[0]["engineering_evidence"]["recorded_evaluations"]==0
    assert hours[0]["runtime_health"] is None and hours[0]["families"]=={}


def test_partitioned_funnel_reconciles_with_censored_global_reducer(data):
    _,row,family=data
    rows=[];seq=0
    for index in range(120):
        seq+=3 if index%19==0 else 1
        value=at(row,seq,(index//20)*HOUR_NS+(index%20)*10+10,index%3!=0)
        if index%7==0:
            value["near_arbitrage"]["valid"]=False;value["evaluation_accepted"]=False
        rows.append(value)
    hours,diagnostics=reduce(data,rows)
    counts=Counter()
    for hour in hours: counts.update(hour["families"][family]["evaluation_funnel"])
    assert all(counts[k]==v for k,v in diagnostics["families"][family]["evaluation_funnel"].items())
    assert sum(h["engineering_evidence"]["missing_observations_detected"] for h in hours)==diagnostics["engineering_evidence"]["missing_observation_rows"]
    for stage in ("raw","after_fee","after_reserve"):
        assert sum(h["families"][family]["near_arbitrage"][stage]["observations"] for h in hours)==diagnostics["families"][family]["near_arbitrage"][stage]["observations"]


def test_runner_publishes_hash_bound_hourly_receipts_and_censors_future_ack(study):
    path=run(**study);report=json.loads(path.read_text())
    wire=(path.parent/"hourly.jsonl").read_bytes()
    assert hashlib.sha256(wire).hexdigest()==report["artifact_sha256"]["hourly.jsonl"]
    hours=[json.loads(line) for line in wire.splitlines()]
    assert len(hours)==report["hourly_attribution"]["windows"]==1
    assert hours[0]["engineering_evidence"]["recorded_evaluations"]==2
    assert all(h["runtime_health"] is None and h["causal_exchange_coverage"] is None for h in hours)
    transitions=[json.loads(line) for line in (path.parent/"capital_transitions.jsonl").read_text().splitlines()]
    censored=[r for r in transitions if not r["modeled_orders_closed"]]
    assert censored and all(r["available_ns"]<=report["replay_receipt"]["availability_monotonic_ns"] for r in censored)
    assert all(r["reservation_duration_ns"] is None for r in censored)
    assert run(**study)==path
    (path.parent/"hourly.jsonl").write_bytes(wire+b"\n")
    with pytest.raises(ValueError,match="existing_artifact_corrupt"): run(**study)
