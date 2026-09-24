from copy import deepcopy
from fractions import Fraction
import hashlib
import json

import pytest

from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_evidence import data
from test_v7_exact_arb_native_runtime_bundle import MODEL
from v7_exact_arb_native_arrival import NativeArrivalHistory
from v7_exact_arb_native_evidence import SAFETY, canonical
from v7_exact_arb_native_execution_bridge import native_candidate
from v7_unified_exact_arb_graph import GraphError
from v7_unified_exact_arb_graph_execution_shadow import simulate


@pytest.fixture
def inputs(native):
    full,episode,bundles=native
    manifest={**SAFETY,"schema":"polymarket_v7_native_exact_arb_ws_session_v1",
        "model_sha":MODEL,"observer_session_id":full["observer_session_id"],
        "capture_scope":"PUBLIC_WS_FRAMES_NOT_REST","decoder_output_capacity":512,
        "maximum_frame_bytes":1024*1024,"bindings":[
            {k:b[k] for k in ("token_id","book_handle","tick_size_e4")} for b in full["leg_books"]]}
    raw=canonical(manifest).encode()
    full["session_manifest_sha256"]=hashlib.sha256(raw).hexdigest()
    candidate=native_candidate(full,episode,bundles,MODEL)
    frame={**SAFETY,"schema":"polymarket_v7_native_exact_arb_replayed_frame_v1","model_sha":MODEL,
        "observer_session_id":full["observer_session_id"],"session_manifest_sha256":full["session_manifest_sha256"],
        "feed_frame_sequence":1,"connection_epoch":full["connection_epoch"],"receive_monotonic_ns":1000000000,
        "availability_monotonic_ns":1000000000,"graph_decision_start_ns":1000000000,
        "reset_reason":"INITIAL_SESSION","replay_continuity_serial":1,"missing_frames_before":0,
        "source_frame_valid":True,"source_state_versions_comparable":True,"books":deepcopy(full["leg_books"])}
    return raw,candidate,frame


def next_frame(first, sequence, when, books=None, **extra):
    result={**deepcopy(first),"feed_frame_sequence":sequence,"receive_monotonic_ns":when,
        "availability_monotonic_ns":when,"graph_decision_start_ns":when,
        "reset_reason":"","books":deepcopy(books or [])}
    result.update(extra)
    return result


def seal(history):
    receipt={**SAFETY,"schema":"polymarket_v7_native_exact_arb_ws_replay_receipt_v1",
        "model_sha":history.model,"observer_session_id":history.session,"session_manifest_sha256":history.manifest_hash,
        "state":"INPUT_REPLAYED","frames_replayed":history.frames,"missing_frames":history.missing,
        "invalid_frames":history.invalid,"last_feed_frame_sequence":history.sequence,
        "availability_monotonic_ns":history.available,"output_chain_sha256":history.chain,
        "producer_tail_completeness_verified":False,"venue_execution_verified":False}
    history.seal(canonical(receipt))


@pytest.mark.parametrize("wall",[None,True,-1,0,"1000",2**63])
def test_recorded_wall_clock_if_present_must_be_valid_integer(inputs,wall):
    raw,_,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        with pytest.raises(ValueError,match="invalid_integer:receive_wall_ms"):
            h.ingest(canonical({**frame,"receive_wall_ms":wall}))
        assert h.failed
    finally: h.close()


def test_wall_clock_step_does_not_reorder_monotonic_execution(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical({**frame,"receive_wall_ms":2000}))
        h.ingest(canonical(next_frame(frame,2,1010000000,receive_wall_ms=1000)))
        seal(h)
        assert simulate(candidate,h.for_candidate(candidate),"PARALLEL",1,0)["state"]=="ALL_LEGS_FILLED"
    finally: h.close()


def test_candidate_joins_continuous_history_and_requires_receipt(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical(frame))
        h.ingest(canonical(next_frame(frame,2,1010000000)))
        view=h.for_candidate(candidate)
        assert simulate(candidate,view,"PARALLEL",1,0)["reason"]=="observation_watermark"
        seal(h)
        result=simulate(candidate,view,"PARALLEL",1,0)
        assert result["state"]=="ALL_LEGS_FILLED"
        assert result["venue_execution_verified"] is False
        assert result["arrival_evidence"]["replay_receipt_verified"] is True
        assert result["arrival_evidence"]["producer_tail_completeness_verified"] is False
        json.dumps(result)  # all exact rational times are JSON-safe
        assert view.at(next(iter(candidate["decision_books"])),1011) is None
    finally: h.close()


def test_same_millisecond_intervening_negative_state_is_used_not_future_recovery(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical(frame))
        bad=deepcopy(frame["books"])
        for b in bad:
            b.update(receive_monotonic_ns=1000100000,state_version=2,asks_e4_microshares=[[6000,5000000]])
        h.ingest(canonical(next_frame(frame,2,1000100000,bad)))
        recovered=deepcopy(frame["books"])
        for b in recovered: b.update(receive_monotonic_ns=1000300000,state_version=3)
        h.ingest(canonical(next_frame(frame,3,1000300000,recovered)))
        h.ingest(canonical(next_frame(frame,4,1010000000)))
        seal(h)
        view=h.for_candidate(candidate)
        result=simulate(candidate,view,"PARALLEL",Fraction(1,5),0)
        assert result["state"]!="ALL_LEGS_FILLED"
        assert all(r["filled_size"]=="0" for r in result["legs"])
        token=next(iter(candidate["decision_books"]))
        assert view.at(token,Fraction(1000200000,1000000))["asks"]==[["3/5","5"]]
        # Caller mutation cannot rewrite retained history.
        book=view.at(token,1001);book["asks"][0][0]="0"
        assert view.at(token,1001)["asks"][0][0]=="2/5"
    finally: h.close()


@pytest.mark.parametrize("kind",["gap","reconnect","invalid"])
def test_resets_invalidate_untouched_token_books(inputs,kind):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical(frame))
        kwargs={"replay_continuity_serial":2}
        sequence=2
        if kind=="gap":
            sequence=3;kwargs.update(reset_reason="FEED_SEQUENCE_GAP",missing_frames_before=1,source_state_versions_comparable=False)
        elif kind=="reconnect": kwargs.update(reset_reason="CONNECTION_EPOCH_CHANGED",connection_epoch=frame["connection_epoch"]+1)
        else: kwargs.update(reset_reason="SOURCE_FRAME_INVALID",source_frame_valid=False)
        h.ingest(canonical(next_frame(frame,sequence,1010000000,**kwargs)))
        seal(h)
        view=h.for_candidate(candidate)
        earlier=view.at(next(iter(candidate["decision_books"])),1001)
        if kind=="gap":
            # The missing frame may have changed this book before 1001ms even
            # though the next recorded frame exposing the gap is at 1010ms.
            assert earlier is None
        else:
            assert earlier is not None
        assert view.at(next(iter(candidate["decision_books"])),1010) is None
    finally: h.close()


def test_eviction_is_unknown_not_carried_forward_liquidity(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL,max_frames=2)
    try:
        h.ingest(canonical(frame));view=h.for_candidate(candidate)
        h.ingest(canonical(next_frame(frame,2,1001000000)))
        h.ingest(canonical(next_frame(frame,3,1010000000)))
        seal(h)
        assert h.retained_frames==2
        assert h.db.execute("SELECT COUNT(*) FROM books").fetchone()[0]==0
        assert view.at(next(iter(candidate["decision_books"])),1001) is None
        with pytest.raises(GraphError,match="candidate_frame_evicted_or_missing"): h.for_candidate(candidate)
    finally: h.close()


@pytest.mark.parametrize("field,value",[("session_manifest_sha256","b"*64),("connection_epoch",99),
    ("decision_timestamp_ms","1001"),("paper_only",False)])
def test_candidate_mismatches_fail_closed(inputs,field,value):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical(frame));candidate[field]=value
        with pytest.raises(GraphError): h.for_candidate(candidate)
    finally: h.close()


def test_counter_equal_but_changed_book_is_not_same_candidate(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        frame["books"][0]["asks_e4_microshares"]=[[4100,5000000]]
        h.ingest(canonical(frame))
        with pytest.raises(GraphError,match="candidate_book_mismatch"): h.for_candidate(candidate)
    finally: h.close()


def test_bad_receipt_poisons_history_and_duplicate_cannot_be_recovered(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical(frame))
        with pytest.raises(ValueError): h.seal("{}")
        with pytest.raises(GraphError,match="failed_history"): h.for_candidate(candidate)
    finally: h.close()
    h=NativeArrivalHistory(raw,MODEL)
    try:
        h.ingest(canonical(frame))
        with pytest.raises(GraphError,match="sequence_or_clock"): h.ingest(canonical(frame))
        with pytest.raises(GraphError,match="closed_history"): seal(h)
    finally: h.close()


def test_byte_budget_and_noncomparable_native_counters_fail_closed(inputs):
    raw,candidate,frame=inputs
    h=NativeArrivalHistory(raw,MODEL,max_bytes=32)
    try:
        with pytest.raises(GraphError,match="frame_exceeds_budget"): h.ingest(canonical(frame))
    finally: h.close()
    h=NativeArrivalHistory(raw,MODEL)
    try:
        frame["source_state_versions_comparable"]=False
        h.ingest(canonical(frame))
        with pytest.raises(GraphError,match="candidate_frame_join"): h.for_candidate(candidate)
    finally: h.close()


def test_different_arrival_tapes_do_not_share_cycle_identity(inputs):
    raw,candidate,frame=inputs
    ids=[]
    for horizon in (1010000000,1011000000):
        h=NativeArrivalHistory(raw,MODEL)
        try:
            h.ingest(canonical(frame));h.ingest(canonical(next_frame(frame,2,horizon)))
            seal(h)
            ids.append(simulate(candidate,h.for_candidate(candidate),"PARALLEL",1,0)["cycle_id"])
        finally: h.close()
    assert ids[0]!=ids[1]
