from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from v7_exact_arb_native_evidence import SAFETY, canonical
from v7_exact_arb_pressure_bench import validate, run


def samples():
    return {"samples":1,"p50":100,"p90":100,"p99":100,"p99_9":100,"max":100,"tail_sample_ge_10000":False}


def counters(n):
    return {"frames":1,"relation_evaluations":n,"accepted_recorded_model":0,"search_exhausted":0,
        "quantities_evaluated":n,"telemetry_enqueued":min(n,64),"telemetry_dropped":max(0,n-64)}


@pytest.fixture
def result():
    profiles=[]
    cases=[(n,d,u,True) for n in (2,3,4,8,16) for d in (4,64,1024) for u in (1,128)]+[(16,1024,128,False)]
    for n,d,u,positive in cases:
        profiles.append({"legs":n,"depth":d,"tokens_updated":u,"raw_positive_fixture":positive,
            "relations":512,"dependencies_per_token":4*n,"wall_elapsed_ns":200,
            "update_and_dependency_ns":samples(),"evaluation_and_bounded_sink_ns":samples(),
            "total_frame_ns":samples(),"counters":counters(4*n if u==1 else 512)})
    return {**SAFETY,"schema":"polymarket_v7_native_exact_arb_pressure_benchmark_v1","units":"nanoseconds",
        "fixture_semantics_verified":False,"cpu_affinity_verified":False,"london_non_regression_verified":False,
        "timed_io":False,"case_ms":1,"max_samples":1,"profiles":profiles,
        "champion_same_host_pressure":{"champion_depth":4,"graph_depth":4,"duration_budget_ms_per_arm":1,
            "graph_frames_completed_during_sampling":0,"graph_relation_evaluations_completed_during_sampling":12,
            "graph_counters_after_join":counters(512),"before_ns":samples(),"during_ns":samples(),"after_ns":samples()}}


def test_full_matrix_and_accounted_drops_do_not_certify_london(result):
    assert validate(result)==result
    assert not result["london_non_regression_verified"]


@pytest.mark.parametrize("mutation",[
    lambda r:r.update(real_order_submission=True),lambda r:r.update(cpu_affinity_verified=True),
    lambda r:r.update(london_non_regression_verified=True),lambda r:r.update(units="microseconds"),
    lambda r:r["profiles"].pop(),lambda r:r["profiles"].__setitem__(0,deepcopy(r["profiles"][1])),
    lambda r:r["profiles"][0].update(tokens_updated=True),
    lambda r:r["profiles"][0]["counters"].update(relation_evaluations=512),
    lambda r:r["profiles"][0]["counters"].update(quantities_evaluated=4097),
    lambda r:r["profiles"][1]["counters"].update(telemetry_dropped=0),
    lambda r:r["profiles"][0]["total_frame_ns"].update(p99=101),
    lambda r:r["profiles"][0]["total_frame_ns"].update(tail_sample_ge_10000=True),
    lambda r:r["profiles"][-1]["counters"].update(accepted_recorded_model=1),
    lambda r:r["champion_same_host_pressure"].update(graph_relation_evaluations_completed_during_sampling=513),
    lambda r:r["profiles"][0].update(wall_elapsed_ns=99),
])
def test_malformed_or_exaggerated_pressure_claims_fail(result,mutation):
    mutation(result)
    with pytest.raises(ValueError): validate(result)


def test_runner_archives_exclusively_and_rejects_parameter_mismatch(result,tmp_path,monkeypatch):
    binary=tmp_path/"local-benchmark";binary.write_bytes(b"synthetic-test-binary-not-executed")
    output=tmp_path/"receipt.json"
    def fake(command,stdout,**kwargs):
        stdout.write(canonical(result).encode());return SimpleNamespace(returncode=0)
    monkeypatch.setattr("v7_exact_arb_pressure_bench.subprocess.run",fake)
    receipt=run(binary,output,1,1,1,4)
    assert json.loads(output.read_text())==receipt
    assert receipt["build_source_binding_verified"] is False and receipt["official_release_verified"] is False
    with pytest.raises(ValueError,match="output_exists"): run(binary,output,1,1,1,4)
    with pytest.raises(ValueError,match="config_mismatch"): run(binary,tmp_path/"wrong.json",1,1,2,4)
    assert not (tmp_path/"wrong.json").exists()


def test_failed_or_partial_native_output_never_publishes(result,tmp_path,monkeypatch):
    binary=tmp_path/"binary";binary.write_bytes(b"test")
    output=tmp_path/"receipt.json"
    def failed(command,stdout,**kwargs):
        stdout.write(b'{"partial":');return SimpleNamespace(returncode=2)
    monkeypatch.setattr("v7_exact_arb_pressure_bench.subprocess.run",failed)
    with pytest.raises(ValueError,match="process_failed"): run(binary,output,1,1,1,4)
    assert not output.exists()


def test_binary_change_during_benchmark_cannot_be_archived(result,tmp_path,monkeypatch):
    binary=tmp_path/"binary";binary.write_bytes(b"before")
    def changed(command,stdout,**kwargs):
        stdout.write(canonical(result).encode());binary.write_bytes(b"after");return SimpleNamespace(returncode=0)
    monkeypatch.setattr("v7_exact_arb_pressure_bench.subprocess.run",changed)
    with pytest.raises(ValueError,match="sources_changed"): run(binary,tmp_path/"receipt.json",1,1,1,4)
