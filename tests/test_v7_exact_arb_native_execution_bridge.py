from copy import deepcopy
from fractions import Fraction
import hashlib
import json

import pytest

from test_v7_exact_arb_native_evidence import data
from test_v7_exact_arb_native_runtime_bundle import MODEL
from v7_exact_arb_native_evidence import Bundles, Evidence, SAFETY, SCHEMA, canonical
from v7_exact_arb_native_execution_bridge import native_candidate
from v7_exact_arb_causal import CausalBooks
from v7_unified_exact_arb_graph import GraphError, frac
from v7_unified_exact_arb_graph_execution_shadow import simulate


@pytest.fixture
def native(data):
    directory, observation, family=data
    full=deepcopy(observation)
    full.update(schema="polymarket_v7_native_exact_arb_full_evidence_v2",decision_start_ns=1000000000,
        feed_frame_sequence=1,session_manifest_sha256="a"*64,control_admission_sequence=3,
        decision_end_ns=1000000010,receive_monotonic_ns=1000000000,
        source_valid_until_monotonic_ns=1100000000,relation_valid_until_monotonic_ns=1100000000,
        evaluation_valid_until_monotonic_ns=1100000000,quantity_microunits=5000000,
        order_quantity_precision_ready=True,global_size_optimum_proven=False,
        after_reserve_pnl_microunits=997500,leg_versions=[1,1],leg_books=[])
    bundle=json.loads((directory/(full["native_bundle_sha256"]+".json")).read_text())
    for i,node in enumerate(bundle["nodes"]):
        full["leg_books"].append({"book_handle":i+1,"token_id":node["token_id"],"state_version":1,
            "receive_monotonic_ns":1000000000,"valid":True,"lineage_continuous":True,
            "ask_truncated":False,"bid_truncated":False,"tick_size_e4":100,
            "bids_e4_microshares":[[3900,5000000]],"asks_e4_microshares":[[4000,5000000]]})
    relation=bundle["relations"][0]
    identity=[MODEL,full["observer_session_id"],1,(relation["economic_identity"],family),"pre_allocation"]
    episode={**SAFETY,"schema":"polymarket_v7_native_exact_arb_diagnostic_episode_v1","model_sha":MODEL,
        "economic_execution_verified":False,"stage":"pre_allocation","left_censored":False,"right_censored":True,
        "economic_identity":relation["economic_identity"],"family":family,
        "observer_session_id":full["observer_session_id"],"native_bundle_sha256":full["native_bundle_sha256"],
        "first_observation_sequence":1,"first_positive_ns":1000000000,
        "episode_id":hashlib.sha256(canonical(identity).encode()).hexdigest()}
    return full,episode,Bundles(directory,MODEL)


def test_native_candidate_pins_full_books_proof_and_compute_end_time(native):
    full,episode,bundles=native
    candidate=native_candidate(full,episode,bundles,MODEL)
    assert frac(candidate["timestamp_ms"]) == Fraction(1000000010,1000000)
    assert frac(candidate["decision_timestamp_ms"]) == 1000
    assert candidate["resource_admission_verified"] is False
    assert candidate["venue_execution_verified"] is False
    assert candidate["global_size_optimum_proven"] is False
    assert candidate["result"]["quantity"] == "5"
    assert len(candidate["relation"]["legs"]) == 2
    # Synthetic exact-time arrival history, NOT reconstructed from the native
    # positive-only full-evidence tape. That distinction is part of the API.
    history=CausalBooks()
    history.ingest(frac(candidate["timestamp_ms"]),candidate["decision_books"])
    history.ingest(1010,{})
    result=simulate(candidate,history,"PARALLEL",1,0)
    assert result["state"] == "ALL_LEGS_FILLED"
    assert frac(result["net_locked_pnl"]) == Fraction(9975,10000)
    assert all(frac(r["submission_timestamp_ms"]) == Fraction(full["decision_end_ns"],1000000) for r in result["legs"])
    assert result["venue_execution_verified"] is False
    assert result["realized_counterfactual_pnl"] is None


def test_future_episode_closure_is_never_a_selection_feature(native):
    full,episode,bundles=native
    first=native_candidate(full,episode,bundles,MODEL)
    episode.update(right_censored=False,complete_lifetime_ns=1000,end_ns=2000000000,end_reason="OBSERVED_NONPOSITIVE")
    assert native_candidate(full,episode,bundles,MODEL) == first


@pytest.mark.parametrize("field,value,reason",[
    ("order_quantity_precision_ready",False,"native_candidate_identity"),
    ("evaluation_accepted",False,"native_candidate_identity"),
    ("decision_end_ns",1100000000,"native_decision_expired"),
    ("decision_end_ns",999999999,"native_decision_clock"),
    ("quantity_microunits",5000001,"native_order_precision"),
    ("quantity_microunits",4000000,"native_minimum_order"),
    ("leg_versions",[2,1],"native_book_terms_or_version"),
    ("leg_books",[],"native_missing_full_evidence"),
])
def test_unusable_native_observations_cannot_enter_scenario(native,field,value,reason):
    full,episode,bundles=native;full[field]=value
    with pytest.raises(ValueError,match=reason): native_candidate(full,episode,bundles,MODEL)


def test_censored_start_and_wrong_episode_join_fail_closed(native):
    full,episode,bundles=native;episode["left_censored"]=True
    with pytest.raises(GraphError,match="native_episode_not_observed_start"):
        native_candidate(full,episode,bundles,MODEL)
    episode["left_censored"]=False;episode["first_observation_sequence"]=2
    with pytest.raises(GraphError,match="native_episode_join"):
        native_candidate(full,episode,bundles,MODEL)


def test_future_book_and_misbound_token_fail_closed(native):
    full,episode,bundles=native
    full["leg_books"][0]["receive_monotonic_ns"]+=1
    with pytest.raises(GraphError,match="native_book_causality"):
        native_candidate(full,episode,bundles,MODEL)
    full["leg_books"][0]["receive_monotonic_ns"]-=1
    full["leg_books"][0]["token_id"]="unrelated"
    with pytest.raises(GraphError,match="native_token_binding"):
        native_candidate(full,episode,bundles,MODEL)


def test_off_tick_or_unsorted_depth_is_not_repaired_into_liquidity(native):
    full,episode,bundles=native
    full["leg_books"][0]["asks_e4_microshares"]=[[4001,5000000]]
    with pytest.raises(GraphError,match="native_depth_level"):
        native_candidate(full,episode,bundles,MODEL)
    full["leg_books"][0]["asks_e4_microshares"]=[[4100,1000000],[4000,5000000]]
    with pytest.raises(GraphError,match="native_depth_order"):
        native_candidate(full,episode,bundles,MODEL)


def test_reducer_episode_identity_joins_exactly_to_its_first_full_evidence(native):
    full,_,bundles=native
    before={**deepcopy(full),"schema":SCHEMA,"evaluation_accepted":False}
    for stage in ("raw","after_fee","after_reserve"):
        before["near_arbitrage"][stage+"_distance_nano"]=200000000
    full.update(observation_sequence=2,decision_start_ns=1000000100,decision_end_ns=1000000110)
    evidence=Evidence(":memory:",bundles.directory,MODEL)
    try:
        evidence.ingest(before); evidence.ingest({**full,"schema":SCHEMA})
        evidence.finish()
        episode=json.loads(evidence.db.execute("SELECT payload FROM episodes WHERE stage='pre_allocation'").fetchone()[0])
        assert episode["left_censored"] is False and episode["right_censored"] is True
        candidate=native_candidate(full,episode,bundles,MODEL)
        assert candidate["opportunity_id"] == episode["episode_id"]
        assert candidate["observation_sequence"] == 2
    finally:
        evidence.close()


def test_native_crossed_book_is_not_an_executable_candidate(native):
    full,episode,bundles=native
    full["leg_books"][0]["bids_e4_microshares"]=[[4100,5000000]]
    with pytest.raises(GraphError,match="native_crossed_book"):
        native_candidate(full,episode,bundles,MODEL)
