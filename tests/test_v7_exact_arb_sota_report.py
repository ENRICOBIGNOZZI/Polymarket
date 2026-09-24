"""Automatic reports cannot manufacture economics or mislabel clock evidence."""
from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from test_v7_exact_arb_native_evidence import data
from test_v7_exact_arb_native_execution_bridge import native
from test_v7_exact_arb_native_arrival import inputs
from test_v7_exact_arb_native_run import study
from v7_exact_arb_native_run import run
from v7_exact_arb_sota_report import DOCUMENTS, LatencyEvidence, health_snapshot, render, table
from test_v7_runtime_identity_health import collect, resolve, target, probes, SHA


@pytest.fixture
def latency():
    db=sqlite3.connect(":memory:")
    replay=sqlite3.connect(":memory:")
    replay.execute("CREATE TABLE replay(seq INTEGER PRIMARY KEY,wire TEXT)")
    value=LatencyEvidence(db,SimpleNamespace(db=replay))
    yield value
    db.close();replay.close()


def observation(latency, seq, receive=100, decode=110, start=140, end=200, frame=None):
    frame=seq if frame is None else frame
    record={"receive_monotonic_ns":receive,"availability_monotonic_ns":decode,
            "graph_decision_start_ns":start,"connection_epoch":1,"source_frame_valid":True}
    latency.archive.db.execute("INSERT OR IGNORE INTO replay VALUES(?,?)",(frame,json.dumps(record)))
    return {"observation_sequence":seq,"feed_frame_sequence":frame,"receive_monotonic_ns":receive,
            "decision_start_ns":start,"decision_end_ns":end,"connection_epoch":1}


def test_paired_contribution_not_sum_of_marginal_quantiles(latency):
    latency.ingest(observation(latency,1,100,190,195,200))
    latency.ingest(observation(latency,2,300,305,310,400))
    result=latency.finish()
    stages=result["stages"]
    assert stages["receive_to_decode_ns"]["quantiles_ns"]["p50"]==5
    assert stages["receive_to_decode_ns"]["quantiles_ns"]["max"]==90
    assert stages["receive_to_relation_emit_ns"]["quantiles_ns"]["p50"]==100
    assert stages["receive_to_decode_ns"]["paired_elapsed_fraction"]=="19/40"
    assert stages["decode_to_graph_start_ns"]["paired_elapsed_fraction"]=="1/20"
    assert stages["graph_start_to_relation_emit_ns"]["paired_elapsed_fraction"]=="19/40"
    assert stages["receive_to_relation_emit_ns"]["paired_elapsed_fraction"] is None
    assert result["relation_observations"]==2 and result["distinct_frames"]==2


def test_same_frame_relations_are_explicitly_observation_weighted(latency):
    latency.ingest(observation(latency,1,end=200,frame=1))
    latency.ingest(observation(latency,2,end=300,frame=1))
    result=latency.finish()
    assert result["distinct_frames"]==1 and result["relation_observations"]==2
    assert result["stages"]["graph_start_to_relation_emit_ns"]["quantiles_ns"]["max"]==160


def test_empty_and_missing_latency_are_not_zero_measurements(latency):
    row=observation(latency,1)
    latency.archive.db.execute("DELETE FROM replay")
    latency.ingest(row)
    result=latency.finish()
    assert result["missing_raw_frame_observations"]==1
    assert result["relation_observations"]==0
    assert all(v is None for s in result["stages"].values() for v in s["quantiles_ns"].values())
    assert all(s["total_ns"] is None for s in result["stages"].values())


@pytest.mark.parametrize("field,value",[("connection_epoch",2),("receive_monotonic_ns",99),
    ("decision_start_ns",141),("decision_end_ns",139)])
def test_mismatched_frame_or_clock_cannot_produce_latency(latency,field,value):
    row=observation(latency,1);row[field]=value
    with pytest.raises(ValueError,match="latency_frame_identity_or_clock"): latency.ingest(row)


def test_zero_elapsed_has_no_invented_percentage(latency):
    latency.ingest(observation(latency,1,100,100,100,100))
    assert all(s["paired_elapsed_fraction"] is None for s in latency.finish()["stages"].values())


def test_invalid_source_frame_cannot_have_emitted_native_relation(latency):
    row=observation(latency,1)
    frame=json.loads(latency.archive.db.execute("SELECT wire FROM replay").fetchone()[0])
    frame["source_frame_valid"]=False
    latency.archive.db.execute("UPDATE replay SET wire=?",(json.dumps(frame),))
    with pytest.raises(ValueError,match="latency_frame_identity_or_clock"): latency.ingest(row)


def test_large_totals_do_not_overflow_sqlite(latency):
    for seq in (1,2,3): latency.ingest(observation(latency,seq,1,2,3,2**62))
    result=latency.finish()
    assert result["stages"]["receive_to_relation_emit_ns"]["total_ns"]==str(3*(2**62-1))


def test_runner_publishes_four_reproducible_hash_bound_documents(study):
    path=run(**study)
    report=json.loads(path.read_text())
    documents=render(report)
    assert set(documents)==set(DOCUMENTS)
    for name in DOCUMENTS:
        payload=(path.parent/name).read_bytes()
        assert payload==documents[name].encode()
        assert hashlib.sha256(payload).hexdigest()==report["artifact_sha256"][name]
        assert report["study_id"] in documents[name]
    assert "v7_exact_arb_sota_report.py" in report["runner_source_sha256"]
    assert "INSUFFICIENT_EVIDENCE" in documents["SOTA_CHECKPOINT.md"]
    assert "SCENARIO_ONLY" in documents["SOTA_GAP_ANALYSIS.md"]
    assert "Hourly multi-session service not established" in documents["SOTA_GAP_ANALYSIS.md"]
    assert report["latency_attribution"]["relation_observations"]==2
    assert run(**study)==path


def test_duplicate_rows_do_not_bias_report_latency(study):
    source=study["observations"][0]
    source.write_text(source.read_text()*2)
    report=json.loads(run(**study).read_text())
    assert report["diagnostics"]["engineering_evidence"]["duplicate_rows_removed"]==2
    assert report["latency_attribution"]["relation_observations"]==2


@pytest.mark.parametrize("name",DOCUMENTS)
def test_corrupt_existing_document_is_not_silently_reused(study,name):
    path=run(**study)
    (path.parent/name).write_text("fake success")
    with pytest.raises(ValueError,match="existing_artifact_corrupt"): run(**study)


def test_report_cannot_grant_authority_or_invent_an_economic_decision(study):
    report=json.loads(run(**study).read_text())
    wrong=deepcopy(report);wrong["real_order_submission"]=True
    with pytest.raises(ValueError,match="paper_boundary"): render(wrong)
    wrong=deepcopy(report);wrong["economic_evidence"]["research_decision"]="CONTINUE_EXACT_ARB"
    with pytest.raises(ValueError,match="new_contract"): render(wrong)


def test_inconsistent_diagnostic_raw_clock_aborts_publication(study):
    source=study["observations"][0]
    rows=[json.loads(line) for line in source.read_text().splitlines()]
    rows[0]["receive_monotonic_ns"]-=1
    source.write_text("".join(json.dumps(row)+"\n" for row in rows))
    with pytest.raises(ValueError,match="latency_frame_identity_or_clock"): run(**study)
    assert not list(study["output"].iterdir())


def test_empty_diagnostic_tape_reports_unknown_latency_and_no_economic_decision(study):
    study["observations"][0].write_text("")
    study["full_evidence"][0].write_text("")
    path=run(**study);report=json.loads(path.read_text())
    assert report["latency_attribution"]["relation_observations"]==0
    assert all(v is None for v in report["economic_evidence"].values())
    assert "UNVERIFIED" in (path.parent/"LATENCY_ATTRIBUTION.md").read_text()


def test_table_preserves_unknown_and_escapes_cells():
    text=table(["Name","Value"],[("market|line\nbreak",None),("known",0)])
    assert "market&#124;line break | UNVERIFIED" in text
    assert "known | 0" in text


@pytest.fixture
def health_files(tmp_path):
    identity=resolve(target(),probes(),now_ms=100)
    # Use the actual canonical collector. Missing runtime components must stay
    # unsafe even when snapshot identity and age are eligible for display.
    receipt=collect(tmp_path,identity,service_active=True,kill_exists=False,
                    prometheus_ready=True,grafana_ready=True,now_ms=100)
    receipt["runtime_model_sha"]=SHA
    path=tmp_path/"health.json";path.write_text(json.dumps(receipt))
    manifest=tmp_path/"target.json";manifest.write_text(json.dumps(target()))
    return path,manifest


def test_fresh_matching_receipt_is_not_current_health_or_profit_attestation(health_files):
    result,artifacts=health_snapshot(*health_files,101,SHA)
    assert result["state"]=="FRESH_MATCHING_CANONICAL_SNAPSHOT"
    assert result["reported_engineering_health"]=="UNSAFE_OR_INCOMPLETE"
    assert result["reported_graph_health"]=="UNAVAILABLE_OR_DEGRADED"
    assert result["current_health_verified"] is False
    assert artifacts["runtime_health.json"]==health_files[0].read_bytes()


@pytest.mark.parametrize("as_of",[99,5101])
def test_stale_or_future_receipt_does_not_retain_health(health_files,as_of):
    result,_=health_snapshot(*health_files,as_of,SHA)
    assert result["state"]=="INELIGIBLE_SNAPSHOT"
    assert "STALE_OR_FUTURE_RECEIPT" in result["reasons"]
    assert result["reported_engineering_health"] is None
    assert result["reported_graph_health"] is None


@pytest.mark.parametrize("key,value,reason",[("runtime_instance_id","i-"+"f"*17,"WRONG_INSTANCE"),
    ("runtime_az","eu-west-1a","WRONG_OR_UNKNOWN_AZ"),("runtime_model_sha","b"*40,"WRONG_OR_UNKNOWN_SHA"),
    ("runtime_release_sha",None,"WRONG_OR_UNKNOWN_SHA")])
def test_wrong_runtime_identity_cannot_be_presented_as_matching(health_files,key,value,reason):
    path,manifest=health_files
    receipt=json.loads(path.read_text());receipt[key]=value;path.write_text(json.dumps(receipt))
    result,_=health_snapshot(path,manifest,101,SHA)
    assert reason in result["reasons"] and result["reported_engineering_health"] is None


@pytest.mark.parametrize("key,value",[("real_order_submission",True),("execution_authority",True),
    ("paper_only",1),("schema","old"),("checks",{}),("engineering_health","HEALTHY")])
def test_unsafe_or_inconsistent_receipt_fails_closed(health_files,key,value):
    path,manifest=health_files
    receipt=json.loads(path.read_text());receipt[key]=value;path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError): health_snapshot(path,manifest,101,SHA)


def test_health_input_requires_explicit_target_and_frozen_as_of(health_files):
    with pytest.raises(ValueError): health_snapshot(health_files[0],None,101,SHA)
    with pytest.raises(ValueError): health_snapshot(*health_files,None,SHA)
    with pytest.raises(ValueError): health_snapshot(*health_files,True,SHA)
    result,artifacts=health_snapshot(None,None,None,SHA)
    assert result["state"]=="UNVERIFIED" and not artifacts


def test_runner_binds_health_artifacts_without_changing_economic_authority(study,health_files):
    path,manifest=health_files
    receipt=json.loads(path.read_text())
    receipt.update(runtime_model_sha=study["model"],runtime_release_sha=study["model"])
    path.write_text(json.dumps(receipt))
    kwargs={**study,"runtime_health":path,"runtime_target":manifest,"report_as_of_ms":101}
    output=run(**kwargs);report=json.loads(output.read_text())
    assert report["runtime_health_snapshot"]["state"]=="FRESH_MATCHING_CANONICAL_SNAPSHOT"
    assert all(v is None for v in report["economic_evidence"].values())
    for name in ("runtime_health.json","runtime_target.json"):
        assert hashlib.sha256((output.parent/name).read_bytes()).hexdigest()==report["artifact_sha256"][name]
    stale=json.loads(run(**{**kwargs,"report_as_of_ms":5101}).read_text())
    assert stale["study_id"]!=report["study_id"]
    assert stale["runtime_health_snapshot"]["reported_engineering_health"] is None
    # Source failure cannot reuse the last successful health input.
    path.unlink()
    with pytest.raises(FileNotFoundError): run(**kwargs)
