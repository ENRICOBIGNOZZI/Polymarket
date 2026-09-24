#!/usr/bin/env python3
"""Canonical read-only runtime health receipt; never infer profitability."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path


def local_facts(repository_root):
    """Read host identity and local readiness off the event path. No AWS credentials.

    EC2 identity is obtained from host/cloud-init facts, never from the desired
    target alone. Missing local facts remain unknown (including on developer Macs).
    """
    import subprocess
    import urllib.request
    target=load(repository_root/"deploy/london/runtime_identity.json")
    if target.get("schema")!="polymarket_v7_runtime_target_v1" or any(target.get(k) is not v for k,v in SAFETY.items()):target={}
    cloud=load(Path("/run/cloud-init/instance-data.json")).get("v1",{})
    if not isinstance(cloud,dict):cloud={}
    try: instance=Path("/sys/devices/virtual/dmi/id/board_asset_tag").read_text().strip()
    except OSError: instance=None
    if not isinstance(instance,str) or not instance.startswith("i-"):instance=None
    try: release=(repository_root/"deploy/london/runtime_sha").read_text().strip()
    except OSError: release=None
    identity={"runtime_instance_id":instance,"runtime_az":cloud.get("availability_zone"),
              "runtime_release_sha":release,"runtime_model_sha":release,
              "target_instance_id":target.get("instance_id"),"runtime_started_at":None}
    facts={"service_active":None,"prometheus_ready":None,"grafana_ready":None}
    if instance is None:return identity,facts
    try:
        result=subprocess.run(["systemctl","show","polymarket-v7-paper.service",
            "-p","ActiveState","-p","ActiveEnterTimestamp"],capture_output=True,text=True,timeout=2)
        if result.returncode==0:
            values=dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)
            facts["service_active"]=values.get("ActiveState")=="active"
            identity["runtime_started_at"]=values.get("ActiveEnterTimestamp")
    except (OSError,subprocess.SubprocessError):pass
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for key,url in (("prometheus_ready","http://127.0.0.1:9090/-/ready"),
                    ("grafana_ready","http://127.0.0.1:3000/api/health")):
        try:
            with opener.open(url,timeout=1) as response:
                facts[key]=response.status==200
                if key=="grafana_ready":facts[key] &= json.loads(response.read(4096)).get("database")=="ok"
        except (OSError,ValueError):facts[key]=False
    return identity,facts

SAFETY=dict(paper_only=True,authenticated_execution=False,real_order_submission=False,
            real_capital_at_risk=False,automatic_promotion=False)


def load(path):
    try:
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def counter(value):
    return value if type(value) is int and value>=0 else None


def fresh(value,now_ms,age_ms=180000):
    stamp=value.get("timestamp_ms")
    if stamp is None and type(value.get("timestamp_ns")) is int:stamp=value["timestamp_ns"]//1000000
    if stamp is None and type(value.get("timestamp")) in (int,float):stamp=int(value["timestamp"]*1000)
    return type(stamp) is int and 0<=now_ms-stamp<=age_ms


def collect(root,identity,*,service_active,kill_exists,prometheus_ready,grafana_ready,now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    control=root/"control";base=root/"research/repricing_book"
    runtime=load(control/"runtime_status.json");native=load(control/"native_engine_manager_status.json")
    clock=load(control/"clock_guard.json");fencing=load(control/"fencing_supervisor_status.json")
    book=load(base/"fillability_ws_status.json");pure=load(base/"pure_arb_status.json")
    graph=load(base/"unified_exact_arb_graph_status.json")
    collectors={name:load(base/name) for name in ("pure_arb_arrival_survival_status.json",
        "pure_arb_deep_sizing_status.json","exchange_execution_status.json",
        "exact_arb_exchange_universe_status.json","unified_exact_arb_graph_execution_status.json")}
    queue=native.get("native_observations_queue_depth",runtime.get("native_observations_queue_depth"))
    drops=graph.get("dropped_observations")
    graph_drops=sum(drops.values()) if isinstance(drops,dict) and all(counter(v) is not None for v in drops.values()) else None
    out={"schema":"polymarket_v7_canonical_runtime_health_v1",**SAFETY,
         "runtime_instance_id":identity.get("runtime_instance_id"),"runtime_az":identity.get("runtime_az"),
         "runtime_model_sha":runtime.get("model_sha"),"runtime_release_sha":identity.get("runtime_release_sha"),
         "runtime_started_at":identity.get("runtime_started_at"),"runtime_root":str(root),
         "service_active":service_active,"kill_exists":kill_exists,
         "clock_state":clock.get("state"),"clock_offset_ms":clock.get("offset_ms"),"clock_safe":clock.get("safe"),
         "fencing_state":fencing.get("state"),"fencing_safe":fencing.get("safe"),
         "active_worker_count":counter(native.get("active_worker_count")),
         "target_worker_count":counter(native.get("target_context_count")),"native_state":native.get("state"),
         "native_observations_written":counter(native.get("native_observations_written")),
         "native_observations_dropped":counter(native.get("native_observations_dropped")),
         "native_queue_depth":counter(queue),"book_tape_generation":book.get("observer_session_id"),
         "book_rows_written_total":counter(book.get("book_events_written")),
         "candidate_rows_written_total":counter(pure.get("cycles_total")),
         "counter_scope":"CURRENT_WRITER_GENERATION_NOT_LIFETIME_ACROSS_RESTARTS",
         "graph_state":graph.get("state"),"graph_generation":graph.get("graph_generation"),
         "graph_relations":counter(graph.get("relations_compiled")),
         "graph_events_processed":counter(graph.get("events_processed")),"graph_dropped_events":graph_drops,
         "collector_states":{k:{"state":v.get("state"),"fresh":fresh(v,now_ms)} for k,v in collectors.items()},
         "prometheus_ready":prometheus_ready,"grafana_ready":grafana_ready,"timestamp_ms":now_ms,
         "economic_evidence":"NOT_ASSESSED_BY_HEALTH_RECEIPT"}
    checks={"service_active":service_active is True,"kill_absent":kill_exists is False,
        "instance_match":identity.get("runtime_instance_id") is not None and identity.get("runtime_instance_id")==identity.get("target_instance_id"),
        "identity_match":bool(re.fullmatch(r"[0-9a-f]{40}",str(runtime.get("model_sha") or ""))) and runtime.get("model_sha")==identity.get("runtime_model_sha")==identity.get("runtime_release_sha"),
        "paper_boundary":all(runtime.get(k) is SAFETY[k] for k in ("paper_only","authenticated_execution","real_order_submission")),
        "runtime_running":runtime.get("state")=="running","runtime_fresh":fresh(runtime,now_ms),
        "clock_fresh":fresh(clock,now_ms),"clock_safe":clock.get("safe") is True,
        "fencing_fresh":fresh(fencing,now_ms),"fencing_safe":fencing.get("safe") is True,
        "native_fresh":fresh(native,now_ms),"native_running":native.get("state")=="RUNNING",
        "book_fresh":fresh(book,now_ms),"book_running":book.get("state")=="running",
        "pure_arb_fresh":fresh(pure,now_ms),"pure_arb_running":pure.get("state")=="running",
        "workers_complete":out["target_worker_count"] is not None and out["target_worker_count"]>0 and out["active_worker_count"]==out["target_worker_count"],
        "native_no_drops":out["native_observations_dropped"]==0,"native_queue_known":out["native_queue_depth"] is not None,
        "native_writes_known":out["native_observations_written"] is not None,
        "book_counter_known":out["book_rows_written_total"] is not None and bool(out["book_tape_generation"]),
        "candidate_counter_known":out["candidate_rows_written_total"] is not None,
        "prometheus_ready":prometheus_ready is True,"grafana_ready":grafana_ready is True}
    for name,value in (("clock",clock),("fencing",fencing),("native",native),("book",book),("pure_arb",pure)):
        checks[name+"_model_match"]=runtime.get("model_sha") is not None and value.get("model_sha")==runtime.get("model_sha")
    out["checks"]=checks;out["engineering_health"]="HEALTHY" if all(checks.values()) else "UNSAFE_OR_INCOMPLETE"
    out["graph_health"]="HEALTHY" if (fresh(graph,now_ms) and graph.get("state")=="COLLECTING"
        and graph_drops==0 and graph.get("model_sha")==runtime.get("model_sha") and runtime.get("model_sha")) else "UNAVAILABLE_OR_DEGRADED"
    out["missing_fields"]=[k for k,v in out.items() if v is None]
    return out
