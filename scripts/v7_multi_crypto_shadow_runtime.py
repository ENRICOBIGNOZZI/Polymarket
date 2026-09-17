#!/usr/bin/env python3
"""Single zero-authority orchestrator for six-asset multi-crypto SHADOW research."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA="polymarket_v7_multi_crypto_shadow_runtime_policy_v1"
ASSETS=("BTC","ETH","SOL","XRP","DOGE","BNB")
STOP=False

def stop_handler(_signum:int,_frame:Any)->None:
    global STOP; STOP=True

def load(path:Path)->dict[str,Any]:
    value=json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value,dict):raise ValueError(f"{path}: object required")
    return value

def atomic_json(path:Path,value:dict[str,Any])->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8"); os.replace(tmp,path)

def validate_policy(value:dict[str,Any])->dict[str,Any]:
    if (value.get("schema")!=SCHEMA or value.get("version")!=1 or value.get("paper_only") is not True
            or value.get("authenticated_execution") is not False or value.get("real_order_submission") is not False
            or value.get("real_capital_at_risk") is not False or value.get("execution_authority") is not False
            or value.get("automatic_promotion") is not False or value.get("research_only") is not True):
        raise ValueError("shadow_runtime_policy_identity_or_authority")
    for name,low,high in (("discovery_interval_seconds",5,300),("discovery_windows",2,12),("state_publish_ms",10,1000),
                          ("feature_interval_ms",10,1000),("feature_tape_minimum_interval_ms",50,60000),("feature_tape_segment_mb",1,4096),
                          ("public_proxy_port",1024,65535),("minimum_free_gib",1,10000),("child_shutdown_seconds",1,60)):
        raw=int(value.get(name) or 0)
        if not low<=raw<=high:raise ValueError(f"shadow_runtime_policy:{name}")
    if value.get("public_proxy_host") not in {"127.0.0.1","::1"} or value.get("compact_label_tape") is not True:
        raise ValueError("shadow_runtime_network_or_tape_policy")
    return value


def exact_sha(value:str)->bool:
    return len(value)==40 and all(ch in "0123456789abcdef" for ch in value)


def git_head(root:Path)->str:
    return subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip()


def ensure_port_free(host:str,port:int)->None:
    family=socket.AF_INET6 if ":" in host else socket.AF_INET
    sock=socket.socket(family,socket.SOCK_STREAM)
    try:sock.bind((host,port))
    except OSError as exc:raise RuntimeError(f"shadow proxy port unavailable: {host}:{port}") from exc
    finally:sock.close()

@dataclass
class Child:
    name:str
    process:subprocess.Popen
    log_handle:Any
    command:list[str]

class Supervisor:
    def __init__(self,*,repository_root:Path,run_root:Path,expected_sha:str,policy:dict[str,Any],build_dir:Path):
        self.repository_root=repository_root; self.run_root=run_root; self.expected_sha=expected_sha; self.policy=policy; self.build_dir=build_dir
        self.children:dict[str,Child]={}; self.logs=run_root/"logs"; self.logs.mkdir(parents=True,exist_ok=True)
        self.started_ns=time.time_ns(); self.discovery_error=""; self.discovery_updates=0; self.last_discovery=0.0
    def start(self,name:str,command:list[str],*,env:dict[str,str]|None=None)->None:
        if name in self.children:raise RuntimeError(f"duplicate child: {name}")
        handle=(self.logs/f"{name}.log").open("a",encoding="utf-8",buffering=1)
        process=subprocess.Popen(command,cwd=self.repository_root,stdout=handle,stderr=subprocess.STDOUT,env=env)
        self.children[name]=Child(name,process,handle,list(command))
    def stop_all(self)->None:
        timeout=int(self.policy["child_shutdown_seconds"]); deadline=time.monotonic()+timeout
        for child in reversed(list(self.children.values())):
            if child.process.poll() is None:child.process.terminate()
        for child in reversed(list(self.children.values())):
            if child.process.poll() is None:
                try:child.process.wait(timeout=max(.1,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:child.process.kill(); child.process.wait(timeout=2)
            child.log_handle.close()
    def assert_alive(self)->None:
        failed=[name for name,child in self.children.items() if child.process.poll() is not None]
        if failed:raise RuntimeError("shadow child exited: "+",".join(failed))
    def child_status(self)->dict[str,Any]:
        return {name:{"pid":child.process.pid,"alive":child.process.poll() is None,"returncode":child.process.poll(),"command_role":name}
                for name,child in sorted(self.children.items())}

def venue_symbol(row:dict[str,Any],key:str)->str:
    value=(row.get("venues") or {}).get(key) if isinstance(row.get("venues"),dict) else None
    return str(value.get("symbol") or "NONE") if isinstance(value,dict) else "NONE"


def external_commands(asset_config:dict[str,Any],binary:Path,run_root:Path,sha:str)->dict[str,list[str]]:
    rows=asset_config.get("assets") if isinstance(asset_config.get("assets"),list) else []
    by_asset={str(row.get("asset") or ""):row for row in rows if isinstance(row,dict)}
    if set(by_asset)!=set(ASSETS):raise ValueError("shadow_runtime_asset_partition")
    output={}
    for asset in ASSETS:
        row=by_asset[asset]; out=run_root/"external"/f"{asset}.json"
        output[asset]=[str(binary),"--output",str(out),"--model-sha",sha,"--asset",asset,
            "--binance-spot-symbol",venue_symbol(row,"binance_spot"),"--coinbase-spot-symbol",venue_symbol(row,"coinbase_spot"),
            "--bybit-spot-symbol",venue_symbol(row,"bybit_spot"),"--binance-usdm-symbol",venue_symbol(row,"binance_usdm"),
            "--bybit-linear-symbol",venue_symbol(row,"bybit_linear"),"--deribit-symbol",venue_symbol(row,"deribit")]
    return output


def refresh_discovery(root:Path,run_root:Path,sha:str,windows:int)->dict[str,Any]:
    registry=run_root/"registry"; registry.mkdir(parents=True,exist_ok=True)
    subprocess.check_call([sys.executable,str(root/"scripts/v7_multi_crypto_discovery.py"),"--output-dir",str(registry),"--windows",str(windows)],cwd=root,stdout=subprocess.DEVNULL)
    selection=run_root/"selection.json"; active=run_root/"active_selection.json"
    common=[sys.executable,str(root/"scripts/v7_multi_crypto_book_selection.py"),"--discovery",str(registry/"current.json"),"--model-sha",sha]
    subprocess.check_call(common+["--output",str(selection)],cwd=root,stdout=subprocess.DEVNULL)
    subprocess.check_call(common+["--output",str(active),"--active-only"],cwd=root,stdout=subprocess.DEVNULL)
    return {"selection":load(selection),"active_selection":load(active)}


def safe_status(path:Path,sha:str)->dict[str,Any]:
    try:value=load(path)
    except (OSError,ValueError,json.JSONDecodeError):return {}
    if value.get("model_sha") not in {None,sha}:return {}
    if value.get("paper_only") is not True or value.get("authenticated_execution") is not False or value.get("real_order_submission") is not False:return {}
    return value

def launch(supervisor:Supervisor,asset_config:dict[str,Any])->None:
    root=supervisor.repository_root; run_root=supervisor.run_root; sha=supervisor.expected_sha; p=supervisor.policy
    for directory in ("external","book","labelbook","compact_labels","research","control","contract_state"): (run_root/directory).mkdir(parents=True,exist_ok=True)
    selections=refresh_discovery(root,run_root,sha,int(p["discovery_windows"])); supervisor.discovery_updates+=1; supervisor.last_discovery=time.monotonic()
    proxy_port=int(p["public_proxy_port"]); proxy_host=str(p["public_proxy_host"]); ensure_port_free(proxy_host,proxy_port)
    supervisor.start("public_https_proxy",[sys.executable,str(root/"scripts/v7_public_https_proxy.py"),"--host",proxy_host,"--port",str(proxy_port)])
    time.sleep(.3)
    ws_ips=subprocess.check_output([sys.executable,str(root/"scripts/v7_public_https_proxy.py"),"--resolve","ws-subscriptions-clob.polymarket.com"],cwd=root,text=True).strip()
    proxy=f"http://{proxy_host}:{proxy_port}"; book_env=os.environ.copy(); book_env.update({"PM_V7_HTTPS_PROXY":proxy,"HTTPS_PROXY":proxy,"https_proxy":proxy,"HTTP_PROXY":proxy,"http_proxy":proxy,"PM_V7_WS_RESOLVE_IPS":ws_ips})
    ext_binary=supervisor.build_dir/"polymarket_v7_external_venue_runtime"; book_binary=supervisor.build_dir/"polymarket_v7_maker_fillability_observer"
    commands=external_commands(asset_config,ext_binary,run_root,sha)
    for asset,command in commands.items():supervisor.start(f"external_{asset.lower()}",command)
    supervisor.start("oracle_hub",[sys.executable,str(root/"scripts/v7_multi_crypto_oracle_hub.py"),"--output",str(run_root/"oracle.json"),"--model-sha",sha,"--selection",str(run_root/"selection.json")])
    common=[str(book_binary),"--config",str(root/"config/paper_v7.json"),"--selection-only","--state-only","--state-publish-ms",str(int(p["state_publish_ms"])),"--model-sha",sha]
    supervisor.start("pm_book_hub",common+["--selection",str(run_root/"selection.json"),"--output-dir",str(run_root/"book")],env=book_env)
    supervisor.start("pm_label_hub",common+["--selection",str(run_root/"active_selection.json"),"--compact-label-tape-dir",str(run_root/"compact_labels"),"--output-dir",str(run_root/"labelbook")],env=book_env)
    contract_state=run_root/"contract_state/current.json"
    supervisor.start("contract_state",[sys.executable,str(root/"scripts/v7_multi_crypto_contract_state.py"),"--selection",str(run_root/"selection.json"),"--oracle-status",str(run_root/"oracle.json"),"--book-features-dir",str(run_root/"book/book_features"),"--output",str(contract_state),"--model-sha",sha,"--loop","--interval-ms",str(int(p["feature_interval_ms"]))])
    features=run_root/"features.json"; feature_command=[sys.executable,str(root/"scripts/v7_multi_crypto_feature_engine.py"),"--oracle-status",str(run_root/"oracle.json"),"--selection",str(run_root/"selection.json"),"--book-features-dir",str(run_root/"book/book_features"),"--contract-state",str(contract_state),"--output",str(features),"--model-sha",sha,"--loop","--interval-ms",str(int(p["feature_interval_ms"]))]
    for asset in ASSETS:feature_command += ["--external",f"{asset}={run_root/'external'/f'{asset}.json'}"]
    supervisor.start("feature_engine",feature_command)
    supervisor.start("feature_tape",[sys.executable,str(root/"scripts/v7_multi_crypto_feature_tape.py"),"--snapshot",str(features),"--output",str(run_root/"research/features.jsonl"),"--status",str(run_root/"research/feature_tape_status.json"),"--model-sha",sha,"--poll-ms",str(int(p["feature_interval_ms"])),"--minimum-interval-ms",str(int(p["feature_tape_minimum_interval_ms"])),"--segment-mb",str(int(p["feature_tape_segment_mb"]))])
    manifest={"schema":"polymarket_v7_multi_crypto_shadow_run_manifest_v1","code_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False,"automatic_promotion":False,"run_root":str(run_root),"started_at_ns":supervisor.started_ns,"components":sorted(supervisor.children),"selection_generation":selections["selection"].get("generation_sha256"),"active_selection_generation":selections["active_selection"].get("generation_sha256")}
    atomic_json(run_root/"control/run_manifest.json",manifest)


def runtime_status(supervisor:Supervisor)->dict[str,Any]:
    root=supervisor.run_root; sha=supervisor.expected_sha
    external={asset:safe_status(root/"external"/f"{asset}.json",sha) for asset in ASSETS}
    oracle=safe_status(root/"oracle.json",sha); book=safe_status(root/"book/fillability_ws_status.json",sha); label=safe_status(root/"labelbook/fillability_ws_status.json",sha)
    contract=safe_status(root/"contract_state/current.json",sha); features=safe_status(root/"features.json",sha); tape=safe_status(root/"research/feature_tape_status.json",sha)
    external_ready=sum(int(v.get("valid") is True) for v in external.values()); all_children=all(c.process.poll() is None for c in supervisor.children.values())
    contract_ready=contract.get("all_active_ready") is True and int(contract.get("active_markets") or 0)>0
    state="RUNNING_SHADOW" if all_children and external_ready==6 and oracle.get("all_assets_fresh") is True and book.get("evidence_complete") is True and label.get("evidence_complete") is True and contract_ready else "WARMING_OR_DEGRADED"
    return {"schema":"polymarket_v7_multi_crypto_shadow_runtime_status_v1","timestamp_ns":time.time_ns(),"started_at_ns":supervisor.started_ns,"code_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,"execution_authority":False,"automatic_promotion":False,"state":state,"children":supervisor.child_status(),"external_ready_assets":external_ready,"oracle_healthy_assets":int(oracle.get("healthy_assets") or 0),"book_evidence_complete":book.get("evidence_complete") is True,"label_evidence_complete":label.get("evidence_complete") is True,"contract_active_markets":int(contract.get("active_markets") or 0),"contract_active_ready_markets":int(contract.get("active_ready_markets") or 0),"contract_all_active_ready":contract_ready,"feature_state":features.get("state"),"feature_tape_emitted":int(tape.get("emitted") or 0),"discovery_updates":supervisor.discovery_updates,"discovery_error":supervisor.discovery_error}

def run(supervisor:Supervisor,asset_config:dict[str,Any],duration_seconds:int)->None:
    launch(supervisor,asset_config); start=time.monotonic(); last_status=0.0
    try:
        while not STOP:
            supervisor.assert_alive(); now=time.monotonic()
            if now-supervisor.last_discovery>=float(supervisor.policy["discovery_interval_seconds"]):
                try:
                    refresh_discovery(supervisor.repository_root,supervisor.run_root,supervisor.expected_sha,int(supervisor.policy["discovery_windows"])); supervisor.discovery_updates+=1; supervisor.discovery_error=""
                except Exception as exc:supervisor.discovery_error=f"{type(exc).__name__}:{exc}"
                supervisor.last_discovery=now
            free=shutil.disk_usage(supervisor.run_root).free
            if free<int(supervisor.policy["minimum_free_gib"])*1024**3:raise RuntimeError("shadow runtime stopped: disk free below policy")
            if now-last_status>=1.0:
                atomic_json(supervisor.run_root/"control/runtime_status.json",runtime_status(supervisor)); last_status=now
            if duration_seconds>0 and now-start>=duration_seconds:break
            time.sleep(.1)
    finally:
        final_before_stop=runtime_status(supervisor)
        supervisor.stop_all()
        stopped=dict(final_before_stop); stopped["last_runtime_state"]=final_before_stop.get("state")
        stopped["state"]="STOPPED"; stopped["stopped_at_ns"]=time.time_ns(); stopped["children"]=supervisor.child_status()
        atomic_json(supervisor.run_root/"control/runtime_status.json",stopped)


def preflight(root:Path,build_dir:Path,sha:str,policy:dict[str,Any],asset_config:dict[str,Any])->dict[str,Any]:
    if git_head(root)!=sha:raise ValueError("shadow runtime checkout SHA mismatch")
    ext=build_dir/"polymarket_v7_external_venue_runtime"; book=build_dir/"polymarket_v7_maker_fillability_observer"
    required=[ext,book,root/"scripts/v7_multi_crypto_discovery.py",root/"scripts/v7_multi_crypto_book_selection.py",root/"scripts/v7_multi_crypto_oracle_hub.py",root/"scripts/v7_multi_crypto_contract_state.py",root/"scripts/v7_multi_crypto_feature_engine.py",root/"scripts/v7_multi_crypto_feature_tape.py"]
    missing=[str(p) for p in required if not p.exists()]
    if missing:raise ValueError("shadow runtime missing artifacts: "+",".join(missing))
    commands=external_commands(asset_config,ext,Path("<RUN_ROOT>"),sha)
    return {"schema":"polymarket_v7_multi_crypto_shadow_preflight_v1","code_sha":sha,"paper_only":True,"authenticated_execution":False,"real_order_submission":False,"execution_authority":False,"automatic_promotion":False,"components":["public_https_proxy"]+[f"external_{a.lower()}" for a in ASSETS]+["oracle_hub","pm_book_hub","pm_label_hub","contract_state","feature_engine","feature_tape"],"external_commands":commands,"policy":policy}


def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("--repository-root",type=Path,default=Path.cwd()); parser.add_argument("--run-root",type=Path,required=True); parser.add_argument("--expected-sha",required=True)
    parser.add_argument("--policy",type=Path,default=Path("config/v7_multi_crypto_shadow_runtime.json")); parser.add_argument("--assets",type=Path,default=Path("config/v7_multi_crypto_assets.json")); parser.add_argument("--build-dir",type=Path,default=Path("build")); parser.add_argument("--duration-seconds",type=int,default=0); parser.add_argument("--preflight-only",action="store_true")
    args=parser.parse_args(); root=args.repository_root.resolve(); policy=validate_policy(load((root/args.policy) if not args.policy.is_absolute() else args.policy)); assets=load((root/args.assets) if not args.assets.is_absolute() else args.assets)
    if not exact_sha(args.expected_sha):raise ValueError("exact expected SHA required")
    build_dir=(root/args.build_dir).resolve() if not args.build_dir.is_absolute() else args.build_dir.resolve(); info=preflight(root,build_dir,args.expected_sha,policy,assets)
    if args.preflight_only:print(json.dumps(info,indent=2,sort_keys=True)); return 0
    if args.duration_seconds<0:raise ValueError("duration-seconds must be nonnegative")
    run_root=args.run_root.resolve(); run_root.mkdir(parents=True,exist_ok=True); signal.signal(signal.SIGINT,stop_handler); signal.signal(signal.SIGTERM,stop_handler)
    supervisor=Supervisor(repository_root=root,run_root=run_root,expected_sha=args.expected_sha,policy=policy,build_dir=build_dir); run(supervisor,assets,args.duration_seconds); return 0

if __name__=="__main__":raise SystemExit(main())
