"""Source receipt -> selection -> native admission -> causal execution tests."""
from copy import deepcopy
import hashlib
from itertools import count
import json

import pytest

from test_v7_exact_arb_native_runtime_bundle import fixture, MODEL
from test_v7_exact_arb_hotset_selection import _hotset
from test_v7_exact_arb_native_evidence import data, canonical
from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_arrival import inputs
from test_v7_exact_arb_native_run import study, artifacts, write_jsonl, control_rows
from test_v7_exact_arb_control_history import chain
from v7_exact_arb_hotset_selection import compile_selection
from v7_exact_arb_venue_terms import CLOB, collect, persist, validate_receipt
from v7_exact_arb_venue_selection import VenueTermsCache, attach_terms, admitted_terms, selected_conditions
from v7_exact_arb_native_run import run
from v7_unified_exact_arb_graph import GraphError, sha


def receipt(condition,tokens,*,start_ns=900000000,itode=False):
    compact={"c":condition,"t":[{"t":t} for t in tokens],"mos":5,"mts":".01",
             "fd":{"r":0,"e":1,"to":True},"itode":itode}
    market={"condition_id":condition,"tokens":[{"token_id":t} for t in tokens],
            "active":True,"closed":False,"accepting_orders":True,"enable_order_book":True,
            "minimum_order_size":5,"minimum_tick_size":".01","seconds_delay":0}
    raw={CLOB+"/clob-markets/"+condition:compact,CLOB+"/markets/"+condition:market}
    raw.update({CLOB+"/book?token_id="+t:{"market":condition,"asset_id":t,"tick_size":".01","min_order_size":"5"} for t in tokens})
    return collect(condition,tokens,lambda url:json.dumps(raw[url]).encode(),
                   count(start_ns).__next__,count(1).__next__)


def selection():
    universe,graph=fixture()
    hotset={**_hotset(graph,[graph["relations"][0]["relation_id"]]),"timestamp_ms":901}
    result,_=compile_selection(graph,universe,hotset,MODEL,64,as_of_ms=902)
    return result


def with_terms():
    chosen=selection();condition,tokens=next(iter(selected_conditions(chosen).items()))
    observed=receipt(condition,tokens)
    return attach_terms(chosen,{condition:observed},903),observed


def archive_for_study(study,itode=False,receipt_change=None):
    # The emitted native fixture keeps the same graph/bundle but shortens its
    # source lease. Rebind the EXACT selection, full observations and control
    # journal consistently; no current/latest lookup may satisfy this linkage.
    full=json.loads(study["full_evidence"][0].read_text())
    original=study["bundles"];isolated=original/"native_generations";isolated.mkdir()
    name=full["native_bundle_sha256"]+".json"
    (isolated/name).write_bytes((original/name).read_bytes());study["bundles"]=isolated
    selected=selection();condition,tokens=next(iter(selected_conditions(selected).items()))
    observed=receipt(condition,tokens,itode=itode)
    if receipt_change is not None:
        receipt_change(observed)
        observed["snapshot_sha256"]=sha({k:v for k,v in observed.items() if k!="snapshot_sha256"})
    selected=attach_terms(selected,{condition:observed},903)
    selected["valid_until_ms"]=1000
    wire=canonical(selected);digest=hashlib.sha256(wire.encode()).hexdigest()
    (study["bundles"]/(digest+".selection.json")).write_text(wire)
    persist(study["bundles"].parent/"native_venue_terms",observed)
    for path in study["observations"]+study["full_evidence"]:
        rows=[json.loads(s) for s in path.read_text().splitlines()]
        for row in rows: row.update(selection_receipt_sha256=digest,source_valid_until_wall_ms=1000)
        write_jsonl(path,rows)
    full=json.loads(study["full_evidence"][0].read_text())
    events=control_rows(full)
    for row in events: row["selection_receipt_sha256"]=digest if row["kind"]=="ADMIT" else ""
    write_jsonl(study["control_events"][0],chain(events))
    study["require_venue_terms"]=True
    return selected,observed,digest


def test_archive_terms_are_projected_from_raw_bytes_not_rehashed_flags():
    selected,observed=with_terms()
    changed=deepcopy(observed);changed["terms"]["mandatory_taker_delay_ms"]=250
    changed["snapshot_sha256"]=sha({k:v for k,v in changed.items() if k!="snapshot_sha256"})
    with pytest.raises(GraphError,match="projection_mismatch"): validate_receipt(changed)


def test_selection_lease_is_capped_at_oldest_request_start_not_completion():
    selected,observed=with_terms()
    assert selected["valid_until_ms"]==30900
    assert selected["venue_terms"]["receipts"][0]["state"]=="OBSERVED_SUPPORTED_TERMS"
    assert selected["timestamp_ms"]==903


@pytest.mark.parametrize("now",[899,30900,40000])
def test_future_or_expired_source_cannot_supply_terms(now):
    selected=selection();selected["timestamp_ms"]=now
    condition,tokens=next(iter(selected_conditions(selected).items()))
    value=attach_terms(selected,{condition:receipt(condition,tokens)},now)
    assert value["venue_terms"]["receipts"][0]["state"]=="UNVERIFIED"


def test_missing_terms_do_not_stop_book_diagnostics_or_grant_zero_delay():
    value=attach_terms(selection(),{},903)
    assert value["source_valid"] is True
    assert value["venue_terms"]["receipts"][0]["terms"] is None
    assert value["venue_terms"]["receipts"][0]["state"]=="UNVERIFIED"


def test_cache_discards_success_before_failed_refresh(tmp_path):
    cache=VenueTermsCache(tmp_path,collector=receipt)
    chosen=selection();cache.refresh(chosen,902)
    assert cache.receipts and not cache.due(chosen,903)
    def fail(*args): raise OSError("disk_or_provider_failure")
    cache.collector=fail
    with pytest.raises(OSError): cache.refresh(chosen,904)
    assert cache.receipts=={} and cache.due(chosen,905)


def test_archive_capacity_is_hard_failure_not_evidence_deletion(tmp_path):
    cache=VenueTermsCache(tmp_path,collector=receipt,maximum_archive_bytes=1)
    with pytest.raises(GraphError,match="archive_budget"): cache.refresh(selection(),902)
    assert cache.receipts=={} and not list(tmp_path.iterdir())


def test_native_study_uses_exact_admitted_terms_and_preserves_unknown_economics(study):
    _,_,digest=archive_for_study(study)
    path=run(**study);report,cycles,_=artifacts(path)
    assert "PER_CANDIDATE_ADMITTED_RECEIPTS" in (path.parent/"ECONOMIC_FUNNEL.md").read_text()
    full=next(c for c in cycles if c["state"]=="ALL_LEGS_FILLED")
    assert full["venue_delay_policy"]=="RECORDED_PUBLIC_METADATA_AT_SELECTION_ADMISSION"
    assert full["venue_terms"]["selection_receipt_sha256"]==digest
    assert full["venue_delay_ms_by_token"]=={"1001":"0","2001":"0"}
    assert full["venue_execution_verified"] is False
    assert report["config"]["venue_terms_mode"]=="REQUIRE_RECORDED_SELECTION_TERMS"
    assert report["economic_evidence"]["net_pnl"] is None


def test_recorded_hold_extends_matching_beyond_short_native_tape(study):
    archive_for_study(study,itode=True)
    _,cycles,_=artifacts(run(**study))
    assert all(c["state"]=="CENSORED" for c in cycles)
    assert not any(c.get("net_locked_pnl") is not None for c in cycles)


def test_explicit_arm_cannot_override_documented_hold_to_zero(study):
    archive_for_study(study,itode=True)
    study["arms"]=[{**study["arms"][1],"venue_delay_ms_by_token":{"1001":0,"2001":0}}]
    _,cycles,_=artifacts(run(**study))
    assert cycles[0]["reason"]=="venue_candidate_delay_override_conflict"


@pytest.mark.parametrize("mutation",["missing_selection","selection_bytes","receipt_bytes","wrong_projection",
                                    "future_receipt","missing_receipt","missing_control_binding","other_control_selection"])
def test_inconsistent_or_late_metadata_never_supplies_native_fill(study,mutation):
    selected,observed,digest=archive_for_study(study)
    path=study["bundles"]/(digest+".selection.json")
    source=study["bundles"].parent/"native_venue_terms"/(observed["snapshot_sha256"]+".json")
    if mutation=="missing_selection": path.rename(path.with_suffix(".absent"))
    if mutation=="selection_bytes": path.write_text("{}")
    if mutation=="receipt_bytes": source.write_text("{}")
    if mutation=="missing_receipt": source.rename(source.with_suffix(".absent"))
    if mutation in {"wrong_projection","future_receipt"}:
        changed=deepcopy(observed)
        if mutation=="wrong_projection": changed["terms"]["mandatory_taker_delay_ms"]=250
        else:
            for request in changed["requests"]:
                request["started_at_ns"]+=100000000;request["finished_at_ns"]+=100000000
        changed["snapshot_sha256"]=sha({k:v for k,v in changed.items() if k!="snapshot_sha256"})
        persist(source.parent,changed)
        # Re-hash the entire selection: integrity alone must not prove causal availability.
        selected["venue_terms"]["receipts"][0]["snapshot_sha256"]=changed["snapshot_sha256"]
        raw=canonical(selected);new=hashlib.sha256(raw.encode()).hexdigest()
        (study["bundles"]/(new+".selection.json")).write_text(raw)
        for file in study["observations"]+study["full_evidence"]:
            rows=[json.loads(s) for s in file.read_text().splitlines()]
            for row in rows: row["selection_receipt_sha256"]=new
            write_jsonl(file,rows)
        control=[json.loads(s) for s in study["control_events"][0].read_text().splitlines()]
        control[2]["selection_receipt_sha256"]=new
        write_jsonl(study["control_events"][0],chain(control))
    if mutation in {"missing_control_binding","other_control_selection"}:
        rows=[json.loads(s) for s in study["control_events"][0].read_text().splitlines()]
        if mutation=="missing_control_binding": rows[2].pop("selection_receipt_sha256")
        else: rows[2]["selection_receipt_sha256"]="f"*64
        write_jsonl(study["control_events"][0],chain(rows))
    _,cycles,_=artifacts(run(**study))
    assert cycles and all(c["state"]=="CENSORED" for c in cycles)


def test_terms_failure_between_legs_preserves_earlier_fill_in_both_worlds(study):
    _,_,digest=archive_for_study(study)
    full=json.loads(study["full_evidence"][0].read_text())
    events=control_rows(full,[("INVALIDATE",990000000),("INVALIDATE",991000000),("ADMIT",992000000),
                            ("INVALIDATE",1001500000),("CHECKPOINT",1011000000)])
    for row in events: row["selection_receipt_sha256"]=digest if row["kind"]=="ADMIT" else ""
    write_jsonl(study["control_events"][0],chain(events))
    study["arms"]=[{**study["arms"][1],"skew_ms":"1"}]
    path=run(**study);_,cycles,_=artifacts(path)
    assert cycles[0]["known_entry_fills"]=={"1001":"5"}
    shared=[json.loads(line) for line in (path.parent/"shared_scenarios.jsonl").read_text().splitlines()]
    assert shared and all(c["known_entry_fills"]=={"1001":"5"} for c in shared)


def test_missing_required_receipts_changes_study_identity_not_silent_reuse(study):
    _,observed,_=archive_for_study(study)
    first=run(**study)
    source=study["bundles"].parent/"native_venue_terms"/(observed["snapshot_sha256"]+".json")
    source.rename(source.with_suffix(".absent"))
    second=run(**study)
    assert first!=second
    _,cycles,_=artifacts(second)
    assert all(c["state"]=="CENSORED" for c in cycles)


def test_public_rest_prices_never_supply_counterfactual_execution_depth(study):
    def change(observed):
        for request in observed["requests"]:
            if request["role"].startswith("book:"):
                raw=json.loads(request["raw_response"])
                raw.update(asks=[{"price":"0.001","size":"100000000"}],bids=[])
                request["raw_response"]=canonical(raw)
                request["raw_sha256"]=hashlib.sha256(request["raw_response"].encode()).hexdigest()
    archive_for_study(study,receipt_change=change)
    report,cycles,_=artifacts(run(**study))
    short=next(a for a in report["arms"] if a["delay_ms"]=="1/5")
    assert next(c for c in cycles if c["arm_id"]==short["arm_id"])["state"]=="NO_LEGS_FILLED"


def test_public_fee_mismatch_is_not_replaced_by_graph_zero_fee(study):
    def change(observed):
        request=observed["requests"][0];raw=json.loads(request["raw_response"])
        raw["fd"]["r"]=".04"
        request["raw_response"]=canonical(raw)
        request["raw_sha256"]=hashlib.sha256(request["raw_response"].encode()).hexdigest()
        observed["terms"]["fee_rate"]="1/25"
    archive_for_study(study,receipt_change=change)
    report,cycles,_=artifacts(run(**study))
    assert all("venue_native_operand_mismatch" in c.get("reason","") for c in cycles)
    assert report["episode_admission"]["venue_terms_unavailable_candidates"]==1
