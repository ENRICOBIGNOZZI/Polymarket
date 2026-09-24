"""Run and archive bounded native pressure evidence; never a release gate.

Uses the locally built benchmark only. No network, deployment, orders, mutable
latest pointers or automatic PASS claim about London latency or economics.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile

from v7_exact_arb_native_evidence import SAFETY, boundary, canonical, integer


def validate(result):
    boundary(result)
    if result.get("schema")!="polymarket_v7_native_exact_arb_pressure_benchmark_v1": raise ValueError("pressure_schema")
    if result.get("units")!="nanoseconds": raise ValueError("pressure_units")
    if integer(result,"case_ms",1)>30000 or integer(result,"max_samples",1)>1000000: raise ValueError("pressure_config_bound")
    for key in ("fixture_semantics_verified","cpu_affinity_verified","london_non_regression_verified","timed_io"):
        if result.get(key) is not False: raise ValueError("pressure_unverified_boundary")
    expected={(n,d,u,True) for n in (2,3,4,8,16) for d in (4,64,1024) for u in (1,128)}|{(16,1024,128,False)}
    profiles=result.get("profiles")
    if not isinstance(profiles,list) or len(profiles)!=len(expected): raise ValueError("pressure_profile_count")
    def samples(value,expected_count=None):
        count=integer(value,"samples",1)
        if count>integer(result,"max_samples",1) or expected_count is not None and count!=expected_count:
            raise ValueError("pressure_sample_count")
        values=[integer(value,key) for key in ("p50","p90","p99","p99_9","max")]
        if values!=sorted(values) or value.get("tail_sample_ge_10000") is not (count>=10000):
            raise ValueError("pressure_quantiles")
    def counters(value,fanout):
        frames=integer(value,"frames")
        evaluations=integer(value,"relation_evaluations")
        if evaluations!=frames*fanout: raise ValueError("pressure_affected_relations")
        points=integer(value,"quantities_evaluated")
        if not evaluations<=points<=512*evaluations: raise ValueError("pressure_sizing_bound")
        if any(integer(value,k)>evaluations for k in ("accepted_recorded_model","search_exhausted")):
            raise ValueError("pressure_evaluation_accounting")
        if integer(value,"telemetry_enqueued")!=min(evaluations,64) or integer(value,"telemetry_dropped")!=max(0,evaluations-64):
            raise ValueError("pressure_queue_accounting")
        return frames
    for row in profiles:
        for key in ("legs","depth","tokens_updated","relations","dependencies_per_token"): integer(row,key,1)
        identity=(row.get("legs"),row.get("depth"),row.get("tokens_updated"),row.get("raw_positive_fixture"))
        if type(row.get("raw_positive_fixture")) is not bool or identity not in expected: raise ValueError("pressure_profile_identity")
        expected.remove(identity)
        if row.get("relations")!=512 or row.get("dependencies_per_token")!=4*row["legs"]: raise ValueError("pressure_graph_bounds")
        count=counters(row["counters"],4*row["legs"] if row["tokens_updated"]==1 else 512)
        for key in ("update_and_dependency_ns","evaluation_and_bounded_sink_ns","total_frame_ns"): samples(row[key],count)
        if integer(row,"wall_elapsed_ns",1)<row["total_frame_ns"]["max"]: raise ValueError("pressure_wall_elapsed")
        if not row["raw_positive_fixture"] and row["counters"]["accepted_recorded_model"]: raise ValueError("pressure_false_positive")
    pressure=result["champion_same_host_pressure"]
    if (integer(pressure,"champion_depth",1)!=4 or integer(pressure,"graph_depth",1)>1024
            or integer(pressure,"duration_budget_ms_per_arm",1)>30000): raise ValueError("pressure_workload_bound")
    for key in ("before_ns","during_ns","after_ns"): samples(pressure[key])
    counters(pressure["graph_counters_after_join"],512)
    for key,total in (("graph_frames_completed_during_sampling","frames"),
                      ("graph_relation_evaluations_completed_during_sampling","relation_evaluations")):
        if integer(pressure,key)>pressure["graph_counters_after_join"][total]: raise ValueError("pressure_overlap_accounting")
    return result


def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""): h.update(block)
    return h.hexdigest()


def run(binary,output,case_ms=100,max_samples=1000000,pressure_ms=1000,pressure_depth=64,timeout=180):
    for value,maximum in ((case_ms,30000),(max_samples,1000000),(pressure_ms,30000),(pressure_depth,1024),(timeout,3600)):
        if type(value) is not int or not 0<value<=maximum: raise ValueError("pressure_argument_bound")
    output=Path(output)
    if output.exists(): raise ValueError("pressure_output_exists")
    root=Path(__file__).resolve().parents[1];binary=Path(binary).resolve()
    paths=["tests/bench_v7_exact_arb_pressure.cpp","include/pm/v7_exact_arb_graph_runtime.hpp",
        "include/pm/v7_exact_arb_graph_hotpath.hpp","include/pm/v7_exact_arb_order_sizing.hpp",
        "include/pm/v7_exact_arb_diagnostics.hpp","include/pm/v7_pure_arb_lane.hpp",
        "include/pm/v7_market_state.hpp","include/pm/v7_spsc.hpp","CMakeLists.txt",
        "scripts/v7_exact_arb_pressure_bench.py"]
    sources={path:digest(root/path) for path in paths};binary_hash=digest(binary)
    command=[str(binary),str(case_ms),str(max_samples),str(pressure_ms),str(pressure_depth)]
    with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
        completed=subprocess.run(command,stdout=stdout,stderr=stderr,timeout=timeout,check=False)
        stdout.seek(0);wire=stdout.read(1024*1024+1)
        if completed.returncode or len(wire)>1024*1024: raise ValueError("pressure_process_failed_or_output_budget")
    result=validate(json.loads(wire))
    if result["case_ms"]!=case_ms or result["max_samples"]!=max_samples: raise ValueError("pressure_config_mismatch")
    pressure=result["champion_same_host_pressure"]
    if pressure["graph_depth"]!=pressure_depth or pressure["duration_budget_ms_per_arm"]!=pressure_ms:
        raise ValueError("pressure_config_mismatch")
    if sources!={path:digest(root/path) for path in paths} or binary_hash!=digest(binary): raise ValueError("pressure_sources_changed")
    pressure=result["champion_same_host_pressure"]
    ratios={q:{"during_over_before":pressure["during_ns"][q]/pressure["before_ns"][q] if pressure["before_ns"][q] else None,
               "after_over_before":pressure["after_ns"][q]/pressure["before_ns"][q] if pressure["before_ns"][q] else None}
            for q in ("p50","p90","p99","p99_9","max")}
    receipt={**SAFETY,"schema":"polymarket_v7_exact_arb_pressure_receipt_v1","command":command,
        "host":{"system":platform.system(),"release":platform.release(),"architecture":platform.machine()},
        "source_sha256":sources,"binary_sha256":binary_hash,"stdout_sha256":hashlib.sha256(wire).hexdigest(),
        "build_source_binding_verified":False,"official_release_verified":False,"london_non_regression_verified":False,
        "champion_latency_ratios_descriptive_only":ratios,"result":result,
        "limitations":["Synthetic structural portfolios, not independently proved market relations or economic evidence.",
            "Observed stress cases, not a formal CPU worst-case bound; low sample tails are not robust latency estimates.",
            "One local worker and one champion sweep; no affinity, thermal/randomized-control, feed/30-worker, network or full-evidence serialization gate.",
            "A stalled 64-slot diagnostic sink is intentional. Drops are explicit; a drop-free real workload is not established.",
            "Hashing sources and a binary does not prove their build correspondence or deployment identity."]}
    output.parent.mkdir(parents=True,exist_ok=True)
    # Publish a complete file exclusively, without overwriting prior evidence.
    with tempfile.TemporaryDirectory(prefix="pressure-receipt-",dir=output.parent) as scratch:
        temporary=Path(scratch)/"receipt.json"
        with temporary.open("x") as stream:
            stream.write(canonical(receipt)+"\n");stream.flush();os.fsync(stream.fileno())
        os.link(temporary,output)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary",required=True,type=Path);parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--case-ms",type=int,default=100);parser.add_argument("--max-samples",type=int,default=1000000)
    parser.add_argument("--pressure-ms",type=int,default=1000);parser.add_argument("--pressure-depth",type=int,default=64)
    parser.add_argument("--timeout",type=int,default=180)
    args=parser.parse_args()
    receipt=run(args.binary,args.output,args.case_ms,args.max_samples,args.pressure_ms,args.pressure_depth,args.timeout)
    print(canonical({"output":str(args.output),"profiles":len(receipt["result"]["profiles"]),
        "champion_latency_ratios_descriptive_only":receipt["champion_latency_ratios_descriptive_only"]}))


if __name__=="__main__": main()
