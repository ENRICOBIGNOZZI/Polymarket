from copy import deepcopy
import hashlib
import json

import pytest

from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_evidence import data, MODEL, SAFETY, SCHEMA, canonical
from test_v7_exact_arb_native_arrival import inputs, next_frame
from v7_exact_arb_native_arrival import NativeArrivalHistory
from v7_exact_arb_native_run import InputBudget, ReplayArchive, normalize_arms, run, finish_ns


def write_jsonl(path, rows):
    path.write_text("".join(canonical(row)+"\n" for row in rows))


def control_rows(full, transitions=None):
    transitions=transitions or [("INVALIDATE",990000000),("INVALIDATE",991000000),
                               ("ADMIT",992000000),("CHECKPOINT",1011000000)]
    rows=[];previous=""
    for sequence,(kind,stamp) in enumerate(transitions,1):
        row={**SAFETY,"schema":"polymarket_v7_native_exact_arb_control_v1","model_sha":MODEL,
            "observer_session_id":full["observer_session_id"],"session_manifest_sha256":full["session_manifest_sha256"],
            "sequence":sequence,"timestamp_monotonic_ns":stamp,"kind":kind,"previous_record_sha256":previous,
            "native_bundle_sha256":full["native_bundle_sha256"] if kind=="ADMIT" else "",
            "valid_until_monotonic_ns":full["source_valid_until_monotonic_ns"] if kind=="ADMIT" else 0}
        previous=hashlib.sha256(canonical(row).encode()).hexdigest();rows.append(row)
    return rows


@pytest.fixture
def study(native, inputs, tmp_path):
    full,episode,bundles=native
    raw,_,frame=inputs
    full=deepcopy(full)
    full.update(observation_sequence=2,feed_frame_sequence=2,leg_versions=[2,2])
    for b in full["leg_books"]: b["state_version"]=2
    before={k:deepcopy(v) for k,v in full.items() if k!="leg_books"}
    before.update(schema=SCHEMA,observation_sequence=1,feed_frame_sequence=1,
        decision_start_ns=999000000,decision_end_ns=999000010,receive_monotonic_ns=999000000,evaluation_accepted=False)
    for stage in ("raw","after_fee","after_reserve"): before["near_arbitrage"][stage+"_distance_nano"]=10000000
    positive={k:deepcopy(v) for k,v in full.items() if k!="leg_books"};positive["schema"]=SCHEMA
    first=deepcopy(frame)
    first.update(receive_monotonic_ns=999000000,availability_monotonic_ns=999000000,graph_decision_start_ns=999000000)
    for b in first["books"]: b.update(receive_monotonic_ns=999000000,asks_e4_microshares=[[6000,5000000]])
    second=next_frame(frame,2,1000000000,full["leg_books"])
    bad=deepcopy(full["leg_books"])
    for b in bad: b.update(receive_monotonic_ns=1000100000,state_version=3,asks_e4_microshares=[[6000,5000000]])
    recovery=deepcopy(full["leg_books"])
    for b in recovery: b.update(receive_monotonic_ns=1000300000,state_version=4)
    frames=[first,second,next_frame(frame,3,1000100000,bad),next_frame(frame,4,1000300000,recovery),
            next_frame(frame,5,1010000000)]
    receipt={**SAFETY,"schema":"polymarket_v7_native_exact_arb_ws_replay_receipt_v1","model_sha":MODEL,
        "observer_session_id":frame["observer_session_id"],"session_manifest_sha256":full["session_manifest_sha256"],
        "state":"INPUT_REPLAYED","frames_replayed":len(frames),"missing_frames":0,"invalid_frames":0,
        "last_feed_frame_sequence":5,"availability_monotonic_ns":1010000000,
        "producer_tail_completeness_verified":False,"venue_execution_verified":False}
    chain=""
    for row in frames: chain=hashlib.sha256((chain+canonical(row)).encode()).hexdigest()
    receipt["output_chain_sha256"]=chain
    manifest=tmp_path/"manifest.json";manifest.write_bytes(raw)
    replay=tmp_path/"replayed.jsonl";write_jsonl(replay,frames+[receipt])
    observations=tmp_path/"observations.jsonl";write_jsonl(observations,[before,positive])
    evidence=tmp_path/"full.jsonl";write_jsonl(evidence,[full])
    control=tmp_path/"control.jsonl";write_jsonl(control,control_rows(full))
    arms=[{"mode":"PARALLEL","delay_ms":d,"skew_ms":"0","unwind_delay_ms":"2","order_type":"FAK"}
          for d in ("1/5","1","100")]
    return {"manifest":manifest,"replay":replay,"observations":[observations],"full_evidence":[evidence],
            "bundles":bundles.directory,"model":MODEL,"output":tmp_path/"output","arms":arms,"control_events":[control]}


def artifacts(path):
    report=json.loads(path.read_text())
    cycles=[json.loads(row) for row in (path.parent/"scenarios.jsonl").read_text().splitlines()]
    admissions=[json.loads(row) for row in (path.parent/"admissions.jsonl").read_text().splitlines()]
    return report,cycles,admissions


def test_explicit_hold_and_response_arms_extend_runner_horizon(study):
    from test_v7_exact_arb_adversarial import opportunity, relation
    candidate=opportunity(relation(2))
    arm=normalize_arms([{**study["arms"][1],"venue_delay_ms_by_token":{"0":10,"1":0},"ack_delay_ms":3}])[0]
    assert finish_ns(candidate,arm)==129000000
    equivalent=normalize_arms([{**study["arms"][1],"venue_delay_ms_by_token":{"1":"0","0":"20/2"},"ack_delay_ms":"6/2"}])[0]
    assert arm==equivalent


def test_native_study_applies_holds_to_independent_and_shared_scenarios(study):
    # Actual fixture tokens are resolved from its archived native bundle.
    full=json.loads(study["full_evidence"][0].read_text())
    tokens=[row["token_id"] for row in full["leg_books"]]
    study["arms"]=[{**study["arms"][1],"venue_delay_ms_by_token":{token:250 for token in tokens},"ack_delay_ms":1}]
    path=run(**study); report,cycles,_=artifacts(path)
    assert cycles[0]["state"]=="CENSORED"
    shared=[json.loads(row) for row in (path.parent/"shared_scenarios.jsonl").read_text().splitlines()]
    assert shared and all(row["state"]=="CENSORED" for row in shared)
    assert report["arms"][0]["venue_delay_ms_by_token"]=={token:"250" for token in tokens}


def test_missing_token_delay_is_visible_as_censored_study_scenario(study):
    study["arms"]=[{**study["arms"][1],"venue_delay_ms_by_token":{"not-in-this-basket":250}}]
    _,cycles,_=artifacts(run(**study))
    assert cycles[0]["reason"]=="execution_mandatory_delay_unknown"


def slow_emission(study):
    full=json.loads(study["full_evidence"][0].read_text())
    full["decision_end_ns"]=full["evaluation_valid_until_monotonic_ns"]
    observations=[json.loads(line) for line in study["observations"][0].read_text().splitlines()]
    observations[-1]["decision_end_ns"]=full["decision_end_ns"]
    write_jsonl(study["full_evidence"][0],[full]);write_jsonl(study["observations"][0],observations)
    return full


def test_slow_computation_is_censored_not_a_failed_study_or_funded_trade(study):
    slow_emission(study)
    path=run(**study);report,cycles,admissions=artifacts(path)
    assert cycles==[] and report["episode_admission"]["decision_expired_episodes"]==1
    assert admissions[0]["state"]=="CENSORED" and admissions[0]["reason"]=="native_decision_expired"
    assert (path.parent/"capital_plans.jsonl").read_text()==""
    assert all(world["capital_lifecycle"]["closed_order_groups"]==0 for world in report["shared_execution"]["worlds"])


def test_expiration_cannot_hide_corrupt_full_book_evidence(study):
    full=slow_emission(study)
    full["leg_books"][0]["asks_e4_microshares"][0][1]=-1
    write_jsonl(study["full_evidence"][0],[full])
    with pytest.raises(ValueError) as error: run(**study)
    assert str(error.value)!="native_decision_expired"


def test_runner_links_observed_episode_native_arrival_and_separate_arms(study):
    path=run(**study)
    report,cycles,admissions=artifacts(path)
    assert report["episode_admission"]["observed_episode_starts"]==1
    assert len(admissions)==1 and admissions[0]["state"]=="SCENARIOS_SCHEDULED"
    states={r["arm_id"]:r["state"] for r in cycles}
    arms={a["delay_ms"]:a for a in report["arms"]}
    assert states[arms["1/5"]["arm_id"]]=="NO_LEGS_FILLED"
    assert states[arms["1"]["arm_id"]]=="ALL_LEGS_FILLED"
    assert states[arms["100"]["arm_id"]]=="CENSORED"
    assert all(a["portfolio_net_pnl"] is None for a in report["arms"])
    assert all(v is None for v in report["economic_evidence"].values())
    assert run(**study)==path
    assert len(list(study["output"].iterdir()))==1
    assert all(not r["venue_execution_verified"] for r in cycles)


def test_repeated_observations_and_full_rows_do_not_repeat_episodes(study):
    for path in study["observations"]+study["full_evidence"]:
        original=path.read_text();path.write_text(original+original)
    report,cycles,_=artifacts(run(**study))
    assert report["diagnostics"]["engineering_evidence"]["duplicate_rows_removed"]==2
    assert report["episode_admission"]["duplicate_full_rows_removed"]==1
    assert len(cycles)==3


def test_missing_full_evidence_is_censored_not_zero_profit(study):
    study["full_evidence"][0].write_text("")
    report,cycles,admissions=artifacts(run(**study))
    assert not cycles
    assert admissions[0]["reason"]=="missing_full_evidence"
    assert report["economic_evidence"]["net_pnl"] is None


def test_left_censored_initial_positive_is_not_a_new_opportunity(study):
    path=study["observations"][0]
    path.write_text(path.read_text().splitlines(keepends=True)[1])
    report,cycles,admissions=artifacts(run(**study))
    assert report["episode_admission"]["left_censored_segments_excluded"]==1
    assert not cycles and not admissions


@pytest.mark.parametrize("corruption",["missing_receipt","changed_receipt","extra_row","partial_line","wrong_session"])
def test_bad_native_replay_never_publishes_report(study,corruption):
    rows=[json.loads(row) for row in study["replay"].read_text().splitlines()]
    if corruption=="missing_receipt": rows.pop()
    if corruption=="changed_receipt": rows[-1]["output_chain_sha256"]="f"*64
    if corruption=="extra_row": rows.append(rows[0])
    if corruption=="wrong_session": rows[0]["observer_session_id"]="other"
    write_jsonl(study["replay"],rows)
    if corruption=="partial_line": study["replay"].write_text(study["replay"].read_text()[:-1])
    with pytest.raises(ValueError): run(**study)
    assert not list(study["output"].iterdir())


def test_full_and_diagnostic_evidence_must_be_same_native_observation(study):
    row=json.loads(study["full_evidence"][0].read_text())
    row["quantity_microunits"]+=10000
    write_jsonl(study["full_evidence"][0],[row])
    with pytest.raises(ValueError,match="full_observation_mismatch"): run(**study)
    assert not list(study["output"].iterdir())


def test_eviction_censors_capacity_instead_of_inventing_old_liquidity(study):
    report,cycles,_=artifacts(run(**study,max_frames=2))
    assert all(r["state"]=="CENSORED" for r in cycles)
    assert any("evicted_or_missing" in r["reason"] for r in cycles)
    assert report["economic_evidence"]["net_pnl"] is None


def test_tiny_input_budget_and_duplicate_arms_fail_closed(study):
    with pytest.raises(ValueError,match="input_budget_exceeded"): run(**study,maximum_rows=1)
    with pytest.raises(ValueError,match="duplicate_scenario_arm"): normalize_arms([study["arms"][0]]*2)
    assert not list(study["output"].iterdir())


def test_prefix_does_not_become_trusted_by_ingesting_arbitrary_rows(study,tmp_path):
    archive=ReplayArchive(tmp_path/"archive.sqlite",study["manifest"].read_bytes(),MODEL,study["replay"],InputBudget(),4096,64*1024*1024)
    h=archive.history()
    try:
        assert not h.receipt_verified()
        row=archive.db.execute("SELECT wire FROM replay ORDER BY seq LIMIT 1").fetchone()[0]
        h.ingest(row)
        assert not h.receipt_verified()
    finally: h.close();archive.close()


def test_existing_corrupt_artifact_is_not_reused(study):
    path=run(**study)
    (path.parent/"scenarios.jsonl").write_text("corrupt\n")
    with pytest.raises(ValueError,match="existing_artifact_corrupt"): run(**study)


def test_working_disk_budget_cannot_be_bypassed_by_small_input(study):
    with pytest.raises(ValueError,match="working_budget_exceeded"): run(**study,maximum_working_bytes=1)
    assert not list(study["output"].iterdir())


def test_gap_before_arrival_censors_even_if_detected_after_arrival(study):
    rows=[json.loads(row) for row in study["replay"].read_text().splitlines()]
    # Drop the adverse update entirely; a later recovery exposes the gap. It
    # must not look like the inexpensive decision quote survived until arrival.
    rows.pop(2)
    rows[2].update(missing_frames_before=1,reset_reason="FEED_SEQUENCE_GAP",
                   source_state_versions_comparable=False,replay_continuity_serial=2)
    rows[3].update(source_state_versions_comparable=False,replay_continuity_serial=2)
    receipt=rows[-1];receipt.update(frames_replayed=4,missing_frames=1)
    chain=""
    for row in rows[:-1]: chain=hashlib.sha256((chain+canonical(row)).encode()).hexdigest()
    receipt["output_chain_sha256"]=chain
    write_jsonl(study["replay"],rows)
    report,cycles,_=artifacts(run(**study))
    assert all(row["state"]=="CENSORED" for row in cycles)
    assert report["data_quality"]["missing_feed_frames"]==1


def test_same_timestamp_final_frame_is_not_sufficient_tail(study):
    # The slow arm's finish is exactly final replay availability. Until there
    # is a strictly later frame, another update at that time may be absent.
    study["arms"]=[{"mode":"PARALLEL","delay_ms":"799999/100000","skew_ms":"0",
                   "unwind_delay_ms":"2","order_type":"FAK"}]
    _,cycles,_=artifacts(run(**study))
    assert cycles[0]["state"]=="CENSORED"
    assert "no_strict_following_frame" in cycles[0]["reason"]


def test_history_configuration_is_part_of_study_and_scenario_identity(study):
    normal,cycles,_=artifacts(run(**study))
    evicted,other,_=artifacts(run(**study,max_frames=2))
    assert normal["study_id"]!=evicted["study_id"]
    assert not ({row["research_scenario_id"] for row in cycles}&{row["research_scenario_id"] for row in other})


def test_capital_plans_are_distinct_from_independent_execution_outcomes(study):
    path=run(**study,capital_budgets=["1","10","1000"])
    report,cycles,_=artifacts(path)
    plans=[json.loads(row) for row in (path.parent/"resource_plans.jsonl").read_text().splitlines()]
    assert len(plans)==3
    by_capital={row["hypothetical_paper_capital"]:row for row in plans}
    assert not by_capital["1"]["selected_ids"]
    assert len(by_capital["10"]["selected_ids"])==1
    assert by_capital["10"]["holds_after"]==by_capital["1000"]["holds_after"]
    assert all(row["expected_pnl"] is None for row in plans)
    shared=[json.loads(row) for row in (path.parent/"shared_scenarios.jsonl").read_text().splitlines()]
    assert len(shared)==6  # two admitted capital budgets, three distinct arms
    assert all(row["resource_release_verified"] is False for row in shared)
    assert report["shared_execution"]["portfolio_net_pnl"] is None
    assert all(world["pending_jobs"]==0 for world in report["shared_execution"]["worlds"])
    assert any(row["state"]=="ALL_LEGS_FILLED" for row in cycles)
    assert report["economic_evidence"]["net_pnl"] is None


def competing_unwinds(study):
    """Two separately funded entries converge on the same unchanged unwind bid."""
    a=json.loads(study["full_evidence"][0].read_text())
    original=[json.loads(row) for row in study["replay"].read_text().splitlines()]
    frame=original[0]
    times=[999000000,1000000000,1000500000,1001000000,1002000000,1003000000,1004000000,1006000000]
    prices=[(6000,6000),(4000,4000),(6000,6000),(4100,4100),(4000,5000),(4100,5000),(4100,5000),None]
    frames=[]
    for index,(when,levels) in enumerate(zip(times,prices),1):
        books=deepcopy(a["leg_books"]) if levels else []
        for book,price in zip(books,levels or []):
            book.update(state_version=index,receive_monotonic_ns=when,asks_e4_microshares=[[price,5000000]])
        row=next_frame(frame,index,when,books)
        if index==1: row["reset_reason"]="INITIAL_SESSION"
        frames.append(row)
    b=deepcopy(a)
    b.update(observation_sequence=4,feed_frame_sequence=4,decision_start_ns=times[3],decision_end_ns=times[3]+10,
        receive_monotonic_ns=times[3],leg_books=deepcopy(frames[3]["books"]),leg_versions=[4,4],after_reserve_pnl_microunits=897500)
    for row,raw in ((a,-200000000),(b,-180000000)):
        row["near_arbitrage"].update(raw_distance_nano=raw,after_fee_distance_nano=raw,after_reserve_distance_nano=raw+500000)
    observations=[]
    for index in range(4):
        full=a if index<2 else b
        observation={k:deepcopy(v) for k,v in full.items() if k!="leg_books"}
        observation.update(schema=SCHEMA,observation_sequence=index+1,feed_frame_sequence=index+1,
            decision_start_ns=times[index],decision_end_ns=times[index]+10,receive_monotonic_ns=times[index])
        if index%2==0:
            observation["evaluation_accepted"]=False
            observation["near_arbitrage"].update(raw_distance_nano=200000000,after_fee_distance_nano=200000000,
                                                 after_reserve_distance_nano=200500000)
        observations.append(observation)
    receipt=deepcopy(original[-1]);receipt.update(frames_replayed=len(frames),last_feed_frame_sequence=len(frames),
                                                   availability_monotonic_ns=times[-1])
    chain=""
    for row in frames: chain=hashlib.sha256((chain+canonical(row)).encode()).hexdigest()
    receipt["output_chain_sha256"]=chain
    write_jsonl(study["replay"],frames+[receipt]);write_jsonl(study["observations"][0],observations)
    write_jsonl(study["full_evidence"][0],[a,b])
    study["arms"]=[{"mode":"PARALLEL","delay_ms":"2","skew_ms":"0","unwind_delay_ms":"2","order_type":"FAK"}]
    study["capital_budgets"]=["10"]
    return study


def test_two_selected_episodes_cannot_both_unwind_into_same_bid(study):
    config=competing_unwinds(study)
    path=run(**config)
    report,independent,_=artifacts(path)
    assert len(independent)==2 and all(row["state"]=="PARTIAL_UNWOUND" for row in independent)
    shared=[json.loads(row) for row in (path.parent/"shared_scenarios.jsonl").read_text().splitlines()]
    assert sorted(row["state"] for row in shared)==["EXPOSURE_REMAINS","PARTIAL_UNWOUND"]
    exposed=next(row for row in shared if row["state"]=="EXPOSURE_REMAINS")
    assert list(exposed["unhedged_exposure"].values())==["5"]
    assert exposed["realized_counterfactual_pnl"] is None
    assert report["shared_execution"]["worlds"][0]["events_processed"]==6
    assert run(**config)==path


def recycling_study(study):
    config=competing_unwinds(study)
    tape=[json.loads(line) for line in config["replay"].read_text().splitlines()]
    frames,receipt=tape[:-1],tape[-1]
    times=[999000000,1000000000,1000500000,1005000000,1006000000,1007000000,1008000000,1010000000]
    for frame,when in zip(frames,times):
        frame.update(receive_monotonic_ns=when,availability_monotonic_ns=when,graph_decision_start_ns=when)
        for book in frame["books"]:
            book["receive_monotonic_ns"]=when
            if frame["feed_frame_sequence"]>=4: book["asks_e4_microshares"]=[[4100,5000000]]
    observations=[json.loads(line) for line in config["observations"][0].read_text().splitlines()]
    for row,when in zip(observations,times):
        row.update(receive_monotonic_ns=when,decision_start_ns=when,decision_end_ns=when+10)
    full=[json.loads(line) for line in config["full_evidence"][0].read_text().splitlines()]
    full[1].update(receive_monotonic_ns=times[3],decision_start_ns=times[3],decision_end_ns=times[3]+10,
                   leg_books=deepcopy(frames[3]["books"]))
    receipt["availability_monotonic_ns"]=times[-1]
    chain=""
    for row in frames: chain=hashlib.sha256((chain+canonical(row)).encode()).hexdigest()
    receipt["output_chain_sha256"]=chain
    write_jsonl(config["replay"],frames+[receipt]);write_jsonl(config["observations"][0],observations)
    write_jsonl(config["full_evidence"][0],full)
    config["capital_budgets"]=["21/5"]
    config["arms"]=[{**config["arms"][0],"ack_delay_ms":delay} for delay in (0,5)]
    return config


def test_world_capital_recycles_only_after_its_own_known_model_ack(study):
    config=recycling_study(study);path=run(**config)
    report,_,_=artifacts(path)
    baseline=[json.loads(line) for line in (path.parent/"resource_plans.jsonl").read_text().splitlines()]
    assert [len(p["selected_ids"]) for p in sorted(baseline,key=lambda p:p["batch_id"])] in ([0,1],[1,0])
    worlds={w["arm_id"]:w for w in report["shared_execution"]["worlds"]}
    arms={int(a["ack_delay_ms"]):a["arm_id"] for a in report["arms"]}
    fast,slow=worlds[arms[0]],worlds[arms[5]]
    assert fast["capital_lifecycle"]["closed_order_groups"]==2
    assert fast["capital_lifecycle"]["modeled_balances"]["PUSD"]=="1/10"
    assert slow["capital_lifecycle"]["closed_order_groups"]==1
    assert slow["capital_lifecycle"]["modeled_balances"]["PUSD"]=="21/5"
    plans=[json.loads(line) for line in (path.parent/"capital_plans.jsonl").read_text().splitlines()]
    assert sum(len(p["selected_ids"]) for p in plans if p["arm_id"]==arms[0])==2
    assert sum(len(p["selected_ids"]) for p in plans if p["arm_id"]==arms[5])==1
    transitions=[json.loads(line) for line in (path.parent/"capital_transitions.jsonl").read_text().splitlines()]
    assert len(transitions)==3
    assert all(t["reservation_duration_ns"]>0 and not t["settlement_release_verified"] for t in transitions)
    assert all(w["capital_lifecycle"]["portfolio_net_pnl"] is None for w in worlds.values())
    assert run(**config)==path
