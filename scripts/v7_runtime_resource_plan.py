#!/usr/bin/env python3
"""Resolve deterministic CPU/nice classes for the London PAPER runtime."""
from __future__ import annotations
import argparse,json,os
from pathlib import Path

def _cpus() -> list[int]:
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError,OSError):
        return list(range(max(1,os.cpu_count() or 1)))

def _fmt(values:list[int])->str:
    return ",".join(str(v) for v in values)

def resolve(config:dict)->dict:
    cpus=_cpus(); n=len(cpus)
    wanted=max(1,int((config.get("hot_path") or {}).get("reserved_cores") or 1))
    hot_count=1 if n<4 else min(wanted,max(1,n//2))
    hot=cpus[-hot_count:]
    other=[c for c in cpus if c not in hot] or cpus
    control=[other[0]] if len(other)>1 else other
    return {
        "schema":"polymarket_v7_runtime_resource_plan_v1",
        "paper_only":True,
        "cpu_count":n,
        "all_cpus":cpus,
        "hot_path_cpus":hot,
        "collector_cpus":other,
        "control_cpus":control,
        "hot_nice":int((config.get("hot_path") or {}).get("nice") or 0),
        "collector_nice":int((config.get("collector") or {}).get("nice") or 5),
        "control_nice":int((config.get("control") or {}).get("nice") or 3),
        "runtime_training":False,
        "retrospective_analytics":False,
    }

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--config",type=Path,required=True); ap.add_argument("--output",type=Path,required=True); ap.add_argument("--shell",action="store_true")
    a=ap.parse_args(); cfg=json.loads(a.config.read_text());
    if cfg.get("schema")!="polymarket_v7_runtime_resources_v1" or cfg.get("paper_only") is not True: raise SystemExit("invalid runtime resource config")
    out=resolve(cfg); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,sort_keys=True,indent=2)+"\n")
    if a.shell:
        pairs={
            "PM_V7_HOT_CPUSET":_fmt(out["hot_path_cpus"]),
            "PM_V7_COLLECTOR_CPUSET":_fmt(out["collector_cpus"]),
            "PM_V7_CONTROL_CPUSET":_fmt(out["control_cpus"]),
            "PM_V7_HOT_NICE":str(out["hot_nice"]),
            "PM_V7_COLLECTOR_NICE":str(out["collector_nice"]),
            "PM_V7_CONTROL_NICE":str(out["control_nice"]),
        }
        for k,v in pairs.items(): print(f"{k}={v}")
    return 0
if __name__=="__main__": raise SystemExit(main())
