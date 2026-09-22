#!/usr/bin/env python3
"""Gate pure-arb native CPU latency benchmarks against repository SLOs."""
from __future__ import annotations
import argparse,json
from pathlib import Path

def load(path:Path):
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError("json object required")
    return value

def metric(row:dict,name:str,key:str)->int:
    v=((row.get("latency_ns") or {}).get(name) or {}).get(key)
    if not isinstance(v,(int,float)):raise ValueError(f"missing {name}.{key}")
    return int(v)

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",type=Path,required=True)
    ap.add_argument("--prewire",type=Path,required=True)
    ap.add_argument("--signal-to-oms",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    a=ap.parse_args()
    cfg,pre,oms=load(a.config),load(a.prewire),load(a.signal_to_oms)
    if cfg.get("schema")!="polymarket_v7_pure_arb_performance_slo_v1":raise SystemExit(64)
    if pre.get("schema")!="polymarket_v7_native_clob_full_prewire_bench_v2":raise SystemExit(65)
    if oms.get("schema")!="polymarket_v7_crypto_to_oms_bench_v1":raise SystemExit(66)
    p=cfg["native_clob_prewire"];o=cfg["signal_to_oms"]
    checks={
        "prewire_p99":(metric(pre,"total","p99"),int(p["p99_hard_max"])),
        "prewire_p999":(metric(pre,"total","p99_9"),int(p["p999_hard_max"])),
        "rate_limit_p99":(metric(pre,"rate_limit_check","p99"),int(p["rate_limit_check_p99_hard_max"])),
        "frame_p99":(metric(pre,"zero_copy_post","p99"),int(p["zero_copy_post_p99_hard_max"])),
        "signal_to_oms_p99":(int((oms["latency_ns"])["p99"]),int(o["p99_hard_max"])),
        "signal_to_oms_p999":(int((oms["latency_ns"])["p999"]),int(o["p999_hard_max"])),
    }
    failed={k:{"observed":v,"limit":limit} for k,(v,limit) in checks.items() if v>limit}
    report={
        "schema":"polymarket_v7_pure_arb_performance_gate_v1",
        "paper_only":True,"passed":not failed,
        "checks":{k:{"observed":v,"limit":limit,"passed":v<=limit} for k,(v,limit) in checks.items()},
        "stretch":{
            "prewire_p99_target":int(p["stretch_p99"]),
            "prewire_p99_observed":metric(pre,"total","p99"),
            "signal_to_oms_p99_target":int(o["stretch_p99"]),
            "signal_to_oms_p99_observed":int(oms["latency_ns"]["p99"]),
        },
        "failures":failed,
    }
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,sort_keys=True,indent=2)+"\n")
    print(json.dumps(report,sort_keys=True))
    return 0 if report["passed"] else 2

if __name__=="__main__":raise SystemExit(main())
