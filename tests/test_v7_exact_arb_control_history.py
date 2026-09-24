"""Control invalidation is not healed by a fresh book or later successful load."""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from test_v7_exact_arb_native_evidence import data, canonical
from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_arrival import inputs
from test_v7_exact_arb_native_run import study, control_rows, write_jsonl, artifacts
from v7_exact_arb_native_run import run
from v7_exact_arb_control_history import ControlHistory
from v7_exact_arb_native_arrival import NativeArrivalHistory
from test_v7_exact_arb_native_arrival import seal, next_frame
from v7_unified_exact_arb_graph_execution_shadow import simulate


def events(study,transitions=None):
    full=json.loads(study["full_evidence"][0].read_text())
    return control_rows(full,transitions)


def replace(study,rows):
    write_jsonl(study["control_events"][0],rows)


def chain(rows):
    previous=""
    for row in rows:
        row["previous_record_sha256"]=previous
        previous=hashlib.sha256(canonical(row).encode()).hexdigest()
    return rows


def test_missing_control_evidence_censors_instead_of_assuming_old_fees(study):
    study.pop("control_events")
    report,cycles,_=artifacts(run(**study))
    assert report["control_history"]["state"]=="UNAVAILABLE"
    assert cycles and all(c["state"]=="CENSORED" and c["net_locked_pnl"] is None for c in cycles)
    assert any(c["reason"]=="control_evidence_missing" for c in cycles)
    assert all(v is None for v in report["economic_evidence"].values())


def test_source_timeout_between_frames_invalidates_arrival_even_with_fresh_books(study):
    replace(study,events(study,[("INVALIDATE",990000000),("INVALIDATE",991000000),("ADMIT",992000000),
                              ("INVALIDATE",1000500000),("CHECKPOINT",1011000000)]))
    report,cycles,_=artifacts(run(**study))
    bydelay={a["delay_ms"]:next(c for c in cycles if c["arm_id"]==a["arm_id"]) for a in report["arms"]}
    assert bydelay["1/5"]["state"]=="NO_LEGS_FILLED"  # before invalidation; real observed adverse book
    assert bydelay["1"]["state"]=="CENSORED"
    assert bydelay["1"]["reason"]=="control_admission_ended_or_unobserved"
    assert bydelay["1"]["net_locked_pnl"] is None


def test_later_readmission_cannot_revive_old_candidate(study):
    replace(study,events(study,[("INVALIDATE",990000000),("INVALIDATE",991000000),("ADMIT",992000000),
        ("INVALIDATE",1000500000),("ADMIT",1000600000),("CHECKPOINT",1011000000)]))
    _,cycles,_=artifacts(run(**study))
    assert not any(c["state"]=="ALL_LEGS_FILLED" for c in cycles)
    assert any(c.get("reason")=="control_admission_ended_or_unobserved" for c in cycles)


def test_first_leg_fill_is_preserved_when_metadata_fails_before_second_leg(study):
    study["arms"]=[{"mode":"PARALLEL","delay_ms":"1","skew_ms":"1","unwind_delay_ms":"2","order_type":"FAK"}]
    replace(study,events(study,[("INVALIDATE",990000000),("INVALIDATE",991000000),("ADMIT",992000000),
                              ("INVALIDATE",1001500000),("CHECKPOINT",1011000000)]))
    path=run(**study);_,cycles,_=artifacts(path)
    assert cycles[0]["state"]=="CENSORED" and len(cycles[0]["legs"])==1
    assert list(cycles[0]["known_entry_fills"].values())==["5"]
    assert cycles[0]["residual_exposure_verified"] is False
    shared=[json.loads(line) for line in (path.parent/"shared_scenarios.jsonl").read_text().splitlines()]
    assert all(c["state"]=="CENSORED" and list(c["known_entry_fills"].values())==["5"] for c in shared)


def test_checkpoint_is_not_unlimited_coverage(study):
    rows=events(study);rows[-1]["timestamp_monotonic_ns"]=1000500000;replace(study,chain(rows))
    _,cycles,_=artifacts(run(**study))
    assert not any(c["state"]=="ALL_LEGS_FILLED" for c in cycles)
    assert any(c.get("reason")=="control_admission_ended_or_unobserved" for c in cycles)


@pytest.mark.parametrize("corruption",["drop","duplicate","hash","time","initial_admit","no_checkpoint",
    "identity","unsafe","lease","nonadmission_terms","double_admit"])
def test_corrupt_control_history_never_publishes_study(study,corruption):
    rows=events(study)
    if corruption=="drop": rows.pop(1)
    if corruption=="duplicate": rows.insert(2,deepcopy(rows[1]))
    if corruption=="hash": rows[2]["previous_record_sha256"]="f"*64
    if corruption=="time": rows[2]["timestamp_monotonic_ns"]=980000000;chain(rows)
    if corruption=="initial_admit": rows[0]["kind"]="ADMIT";chain(rows)
    if corruption=="no_checkpoint": rows.pop()
    if corruption=="identity": rows[0]["observer_session_id"]="other";chain(rows)
    if corruption=="unsafe": rows[0]["authenticated_execution"]=True;chain(rows)
    if corruption=="lease": rows[2]["valid_until_monotonic_ns"]=rows[2]["timestamp_monotonic_ns"];chain(rows)
    if corruption=="nonadmission_terms": rows[-1]["valid_until_monotonic_ns"]=100;chain(rows)
    if corruption=="double_admit":
        rows[1].update(kind="ADMIT",native_bundle_sha256=rows[2]["native_bundle_sha256"],
                       valid_until_monotonic_ns=rows[2]["valid_until_monotonic_ns"]);chain(rows)
    replace(study,rows)
    with pytest.raises(ValueError): run(**study)
    assert not list(study["output"].iterdir())


@pytest.mark.parametrize("change",["admission","bundle","deadline"])
def test_candidate_must_match_exact_control_admission(study,change):
    rows=events(study)
    if change=="admission": rows[2].update(kind="INVALIDATE",native_bundle_sha256="",valid_until_monotonic_ns=0)
    if change=="bundle": rows[2]["native_bundle_sha256"]="f"*64
    if change=="deadline": rows[2]["valid_until_monotonic_ns"]+=1
    replace(study,chain(rows))
    _,cycles,_=artifacts(run(**study))
    assert all(c["state"]=="CENSORED" for c in cycles)


def test_readmission_is_valid_for_new_candidate_not_old_one(study):
    rows=events(study,[("INVALIDATE",990000000),("INVALIDATE",991000000),("ADMIT",992000000),
                      ("INVALIDATE",993000000),("ADMIT",994000000),("CHECKPOINT",1011000000)])
    replace(study,rows)
    for path in study["observations"]+study["full_evidence"]:
        values=[json.loads(line) for line in path.read_text().splitlines()]
        for value in values: value["control_admission_sequence"]=5
        write_jsonl(path,values)
    report,cycles,_=artifacts(run(**study))
    assert any(c["state"]=="ALL_LEGS_FILLED" for c in cycles)
    assert report["control_history"]["records"]==6


def test_canonical_control_artifact_is_hash_bound_and_repeatable(study):
    path=run(**study);report=json.loads(path.read_text())
    payload=(path.parent/"control_events.jsonl").read_bytes()
    assert hashlib.sha256(payload).hexdigest()==report["artifact_sha256"]["control_events.jsonl"]
    assert payload==study["control_events"][0].read_bytes()
    assert run(**study)==path
    (path.parent/"control_events.jsonl").write_text("broken")
    with pytest.raises(ValueError,match="existing_artifact_corrupt"): run(**study)


def test_oversized_control_record_is_rejected(study):
    rows=events(study);rows[0]["extra"]="x"*16384
    db=sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError,match="control_record_size"):
            ControlHistory(db,[canonical(rows[0])],study["model"],rows[0]["observer_session_id"],rows[0]["session_manifest_sha256"])
    finally: db.close()


def test_different_admission_bindings_have_distinct_execution_identity(inputs,native):
    raw,candidate,frame=inputs
    full,_,_=native
    rows=control_rows(full)
    db=sqlite3.connect(":memory:");books=NativeArrivalHistory(raw,candidate["model_sha"])
    try:
        control=ControlHistory(db,map(canonical,rows),candidate["model_sha"],candidate["observer_session_id"],candidate["session_manifest_sha256"])
        books.ingest(canonical(frame));books.ingest(canonical(next_frame(frame,2,1010000000)));seal(books)
        good=simulate(candidate,control.view(books.for_candidate(candidate),candidate),"PARALLEL",1,0)
        wrong=deepcopy(candidate);wrong["control_admission_sequence"]=2
        bad=simulate(wrong,control.view(books.for_candidate(wrong),wrong),"PARALLEL",1,0)
        assert good["state"]=="ALL_LEGS_FILLED" and bad["state"]=="CENSORED"
        assert good["cycle_id"]!=bad["cycle_id"]
        assert good["arrival_evidence"]["control_candidate_interval"]["boundary_reasons"]==["CONTROL_PREFIX_WATERMARK"]
    finally: books.close();db.close()
