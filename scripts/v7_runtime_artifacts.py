#!/usr/bin/env python3
"""Validate and stage an immutable research-built artifact bundle for London."""
from __future__ import annotations
import argparse,hashlib,json,os,shutil
from pathlib import Path

SCHEMA="polymarket_v7_runtime_artifact_bundle_v1"
MAKER_SCHEMA="polymarket_v7_maker_execution_model_v1"

def sha256(path:Path)->str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
    return h.hexdigest()

def fnv1a64(path:Path)->str:
    value=14695981039346656037
    for b in path.read_bytes():
        value ^= b; value=(value*1099511628211)&0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"

def atomic_copy(src:Path,dst:Path)->None:
    dst.parent.mkdir(parents=True,exist_ok=True); tmp=dst.with_name(dst.name+f".tmp.{os.getpid()}")
    shutil.copyfile(src,tmp); os.replace(tmp,dst)

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("--artifact-root",type=Path,required=True); ap.add_argument("--run-root",type=Path,required=True); ap.add_argument("--model-sha",required=True); ap.add_argument("--policy",type=Path,required=True); ap.add_argument("--allocation",type=Path,required=True); ap.add_argument("--receipt",type=Path,required=True)
    a=ap.parse_args(); root=a.artifact_root.resolve(); manifest_path=root/"manifest.json"
    if len(a.model_sha)!=40 or any(c not in "0123456789abcdef" for c in a.model_sha): raise SystemExit("artifact target SHA invalid")
    m=json.loads(manifest_path.read_text())
    if m.get("schema")!=SCHEMA or m.get("paper_only") is not True or m.get("authenticated_execution") is not False or m.get("real_order_submission") is not False or m.get("target_model_sha")!=a.model_sha: raise SystemExit("runtime artifact manifest contract invalid")
    cv_meta=m.get('candidate_validation') or {}; cv=root/str(cv_meta.get('path') or '')
    if not cv.is_file() or sha256(cv)!=cv_meta.get('sha256'): raise SystemExit('candidate validation hash mismatch')
    cv_value=json.loads(cv.read_text())
    if cv_value.get('schema')!='polymarket_v7_candidate_validation_v1' or cv_value.get('target_sha')!=a.model_sha or cv_value.get('state')!='PROMOTABLE' or cv_value.get('promotable') is not True or cv_value.get('automatic_deployment') is not False: raise SystemExit('candidate validation rejected')
    maker_meta=m.get("maker_execution_model") or {}; maker=root/str(maker_meta.get("path") or "")
    if not maker.is_file() or sha256(maker)!=maker_meta.get("sha256"): raise SystemExit("maker artifact hash mismatch")
    model=json.loads(maker.read_text())
    if model.get("schema")!=MAKER_SCHEMA or model.get("paper_only") is not True or model.get("authenticated_execution") is not False or model.get("model_sha")!=a.model_sha: raise SystemExit("maker artifact identity invalid")
    if str(model.get("policy_hash") or "")!=fnv1a64(a.policy) or str(model.get("config_hash") or "")!=fnv1a64(a.allocation): raise SystemExit("maker artifact policy/config mismatch")
    staged=a.run_root/"micro_maker/execution_model.json"; atomic_copy(maker,staged)
    rich_meta=m.get("rich_research_model") or {}; rich_state=str(rich_meta.get("state") or "UNAVAILABLE")
    rich_path=None
    if rich_state=="AVAILABLE":
        rich_path=root/str(rich_meta.get("path") or "")
        if not rich_path.is_file() or sha256(rich_path)!=rich_meta.get("sha256"): raise SystemExit("rich artifact hash mismatch")
        raw=json.loads(rich_path.read_text())
        if raw.get("artifact_role")!="RESEARCH" or raw.get("code_sha")!=a.model_sha: raise SystemExit("rich artifact identity invalid")
    receipt={
        "schema":"polymarket_v7_runtime_artifact_receipt_v1","paper_only":True,"authenticated_execution":False,"real_order_submission":False,
        "target_model_sha":a.model_sha,"bundle_manifest_sha256":sha256(manifest_path),"maker_execution_model_sha256":sha256(staged),"candidate_validation_sha256":sha256(cv),
        "maker_staged_path":str(staged),"rich_model_state":rich_state,"rich_model_path":str(rich_path) if rich_path else None,"runtime_training":False,
    }
    a.receipt.parent.mkdir(parents=True,exist_ok=True); a.receipt.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
    print(json.dumps(receipt,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
