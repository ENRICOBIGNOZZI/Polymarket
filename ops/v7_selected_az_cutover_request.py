#!/usr/bin/env python3
"""Validate one selected-AZ PAPER cutover request and one London shootout."""
from __future__ import annotations
import argparse, json, re, subprocess
from pathlib import Path

SHA=re.compile(r"^[0-9a-f]{40}$")
IID=re.compile(r"^i-[0-9a-f]+$")
RID=re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
ZONES={"euw2-az1","euw2-az2","euw2-az3"}

def request(path:Path,parent:str,role:str)->dict:
    v=json.loads(path.read_text())
    expected={
      "schema","version","request_id","target_sha","latency_run_id",
      "selected_physical_zone_id","expected_instance_id","role_arn",
      "paper_only","authenticated_execution","real_order_submission",
      "cutover_approved","trigger"
    }
    assert set(v)==expected
    assert v["schema"]=="polymarket_v7_selected_az_paper_cutover_request_v1"
    assert v["version"]==1 and RID.fullmatch(v["request_id"])
    assert SHA.fullmatch(v["target_sha"]) and isinstance(v["latency_run_id"],int) and v["latency_run_id"]>0
    assert v["selected_physical_zone_id"] in ZONES
    assert IID.fullmatch(v["expected_instance_id"])
    assert v["role_arn"]==role
    assert v["paper_only"] is True and v["authenticated_execution"] is False and v["real_order_submission"] is False
    assert v["cutover_approved"] is True
    assert v["trigger"]=="GITHUB_AWS_OIDC_SSM_SELECTED_AZ"
    subprocess.run(["git","merge-base","--is-ancestor",v["target_sha"],parent],check=True)
    return v

def shootout(path:Path,sha:str,zone:str)->dict:
    v=json.loads(path.read_text())
    assert v["schema"]=="polymarket_v7_london_latency_shootout_v1"
    assert v["expected_sha"]==sha and v["selected_physical_zone_id"]==zone
    assert v["paper_only"] is True and v["authenticated_execution"] is False and v["real_order_submission"] is False
    assert v["automatic_cutover"] is False
    return v

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--request",type=Path,required=True)
    p.add_argument("--parent-sha",required=True)
    p.add_argument("--role-arn",required=True)
    p.add_argument("--shootout",type=Path)
    p.add_argument("--output",type=Path)
    a=p.parse_args()
    v=request(a.request,a.parent_sha,a.role_arn)
    if a.shootout: shootout(a.shootout,v["target_sha"],v["selected_physical_zone_id"])
    if a.output:
      a.output.write_text(json.dumps(v,sort_keys=True,indent=2)+"\n")
    print(json.dumps(v,sort_keys=True,separators=(",",":")))
    return 0
if __name__=="__main__": raise SystemExit(main())
