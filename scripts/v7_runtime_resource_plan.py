#!/usr/bin/env python3
"""Resolve deterministic disjoint CPU classes for the London PAPER runtime."""
from __future__ import annotations
import argparse,json,os
from pathlib import Path


def _parse_cpu_list(raw:str)->list[int]:
    values:set[int]=set()
    for part in raw.strip().split(","):
        part=part.strip()
        if not part:
            continue
        if "-" in part:
            left,right=part.split("-",1)
            start,end=int(left),int(right)
            if start<0 or end<start:
                raise ValueError("invalid cpuset range")
            values.update(range(start,end+1))
        else:
            value=int(part)
            if value<0:
                raise ValueError("invalid cpuset cpu")
            values.add(value)
    return sorted(values)


def _cgroup_effective_cpus()->list[int]:
    """Return the outer cgroup CPU allowance, not inherited task affinity.

    London intentionally keeps PID1/general work off isolated HFT cores. A
    child may still legally widen its affinity to those cores with taskset when
    the cgroup permits them. Therefore sched_getaffinity(0) is too narrow for
    resource planning on an isolated-core host.
    """
    root=Path("/sys/fs/cgroup")
    try:
        rows=Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
        unified=next((row.split(":",2)[2] for row in rows
                      if row.startswith("0::")),None)
        if unified is None:
            return []
        current=root/unified.lstrip("/")
        while True:
            path=current/"cpuset.cpus.effective"
            try:
                cpus=_parse_cpu_list(path.read_text(encoding="utf-8"))
            except (OSError,ValueError):
                cpus=[]
            if cpus:
                return cpus
            if current==root or root not in current.parents:
                break
            current=current.parent
    except OSError:
        pass
    return []


def _cpus() -> list[int]:
    cgroup=_cgroup_effective_cpus()
    if cgroup:
        return cgroup
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError,OSError):
        return list(range(max(1,os.cpu_count() or 1)))


def _fmt(values:list[int])->str:
    return ",".join(str(v) for v in values)


def _count(config:dict,name:str,default:int)->int:
    value=int((config.get(name) or {}).get("reserved_cores") or default)
    if value < 1:
        raise ValueError(f"{name}.reserved_cores must be positive")
    return value


def resolve(config:dict, cpus:list[int]|None=None)->dict:
    cpus=sorted(_cpus() if cpus is None else cpus)
    minimum=max(1,int(config.get("minimum_visible_cpus") or 8))
    hot_n=_count(config,"hot_path",4)
    collector_n=_count(config,"collector",2)
    control_n=_count(config,"control",1)
    housekeeping_n=_count(config,"housekeeping",1)
    required=hot_n+collector_n+control_n+housekeeping_n
    if minimum < required:
        minimum=required
    visible_ok=len(cpus)>=minimum
    if not visible_ok:
        return {
            "schema":"polymarket_v7_runtime_resource_plan_v2",
            "paper_only":True,"cpu_count":len(cpus),"all_cpus":cpus,
            "minimum_visible_cpus":minimum,"required_assigned_cpus":required,
            "hot_path_cpus":[],"collector_cpus":[],"control_cpus":[],
            "latency_observer_cpus":[],"latency_observer_isolated":False,
            "housekeeping_cpus":[],"spare_cpus":[],
            "cpu_classes_disjoint":False,
            "outer_cpuset_contract_satisfied":False,
            "runtime_training":False,"retrospective_analytics":False,
        }
    hot=cpus[-hot_n:]
    low=cpus[:-hot_n]
    housekeeping=low[:housekeeping_n]
    control=low[housekeeping_n:housekeeping_n+control_n]
    start=housekeeping_n+control_n
    collector=low[start:start+collector_n]
    spare=low[start+collector_n:]
    latency_cfg=config.get("latency_observer") or {}
    prefer_spare=latency_cfg.get("prefer_spare_core") is True
    if prefer_spare and spare:
        latency_observer=[spare[-1]]
        spare=spare[:-1]
        latency_observer_isolated=True
    else:
        latency_observer=list(collector)
        latency_observer_isolated=False
    classes=[set(hot),set(collector),set(control),set(housekeeping)]
    disjoint=all(not classes[i]&classes[j] for i in range(len(classes)) for j in range(i+1,len(classes)))
    if config.get("require_disjoint_cpu_classes") is True and not disjoint:
        raise ValueError("runtime CPU classes overlap")
    return {
        "schema":"polymarket_v7_runtime_resource_plan_v2",
        "paper_only":True,"cpu_count":len(cpus),"all_cpus":cpus,
        "minimum_visible_cpus":minimum,"required_assigned_cpus":required,
        "hot_path_cpus":hot,"collector_cpus":collector,"control_cpus":control,
        "latency_observer_cpus":latency_observer,
        "latency_observer_isolated":latency_observer_isolated,
        "housekeeping_cpus":housekeeping,"spare_cpus":spare,
        "cpu_classes_disjoint":disjoint,
        "outer_cpuset_contract_satisfied":True,
        "hot_nice":int((config.get("hot_path") or {}).get("nice") or 0),
        "collector_nice":int((config.get("collector") or {}).get("nice") or 5),
        "control_nice":int((config.get("control") or {}).get("nice") or 3),
        "latency_observer_nice":int((config.get("latency_observer") or {}).get("nice") or 0),
        "runtime_training":False,"retrospective_analytics":False,
    }


def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--config",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--shell",action="store_true")
    a=ap.parse_args();cfg=json.loads(a.config.read_text())
    if cfg.get("schema")!="polymarket_v7_runtime_resources_v1" or cfg.get("paper_only") is not True:
        raise SystemExit("invalid runtime resource config")
    try:
        out=resolve(cfg)
    except (TypeError,ValueError) as exc:
        raise SystemExit(f"invalid runtime CPU contract: {exc}") from exc
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(out,sort_keys=True,indent=2)+"\n")
    if not out["outer_cpuset_contract_satisfied"]:
        if a.shell:
            print("PM_V7_RESOURCE_PLAN_ERROR=VISIBLE_CPUSET_BELOW_HFT_MINIMUM")
        return 2
    if a.shell:
        pairs={
            "PM_V7_HOT_CPUSET":_fmt(out["hot_path_cpus"]),
            "PM_V7_COLLECTOR_CPUSET":_fmt(out["collector_cpus"]),
            "PM_V7_CONTROL_CPUSET":_fmt(out["control_cpus"]),
            "PM_V7_LATENCY_CPUSET":_fmt(out["latency_observer_cpus"]),
            "PM_V7_HOT_NICE":str(out["hot_nice"]),
            "PM_V7_COLLECTOR_NICE":str(out["collector_nice"]),
            "PM_V7_CONTROL_NICE":str(out["control_nice"]),
            "PM_V7_LATENCY_NICE":str(out["latency_observer_nice"]),
        }
        for k,v in pairs.items(): print(f"{k}={v}")
    return 0


if __name__=="__main__": raise SystemExit(main())
