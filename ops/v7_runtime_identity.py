#!/usr/bin/env python3
"""Resolve one read-only London identity; no historical host/SHA fallback."""
from __future__ import annotations
import argparse
import ipaddress
import json
from pathlib import Path
import re
import time
from v7_london_ssm_deploy import REGION, STACK, SsmDeployError, aws_json, probe, select_target, exact_sha

SCHEMA = "polymarket_v7_observed_runtime_identity_v1"
SAFETY = dict(paper_only=True, authenticated_execution=False, real_order_submission=False,
              real_capital_at_risk=False, automatic_promotion=False)


def validate_target(value):
    if value.get("schema") != "polymarket_v7_runtime_target_v1" or any(value.get(k) is not v for k,v in SAFETY.items()):
        raise ValueError("runtime_target_contract")
    if value.get("region") != REGION or not re.fullmatch(r"i-[0-9a-f]{17}",value.get("instance_id", "")):
        raise ValueError("runtime_target_location")
    if value.get("service") != "polymarket-v7-paper.service":raise ValueError("runtime_target_service")
    if value.get("tailscale_ip"):
        if ipaddress.ip_address(value["tailscale_ip"]) not in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("runtime_target_tailnet")
    if value.get("collection_root") != "/mnt/polymarket-data/polymarket_v7_collection":
        raise ValueError("runtime_target_collection_root")
    return value


def resolve(target, probes, expected_sha="", now_ms=None):
    validate_target(target)
    selected=select_target(probes,target.get("tailscale_ip") or "",target["instance_id"])
    # select_target's explicit instance path does not itself check the tailnet.
    if target.get("tailscale_ip") and target["tailscale_ip"] not in selected.get("tailscale_ips",[]):
        raise ValueError("runtime_tailnet_mismatch")
    model=selected.get("runtime_sha"); release=selected.get("release_sha")
    if not exact_sha(model) or not exact_sha(release) or model!=release:
        raise ValueError("runtime_model_release_mismatch")
    if expected_sha and (not exact_sha(expected_sha) or model!=expected_sha):
        raise ValueError("runtime_expected_sha_mismatch")
    root=selected.get("run_root")
    if not isinstance(root,str) or not root.startswith("/mnt/polymarket-data/") or ".." in Path(root).parts:
        raise ValueError("runtime_root_invalid")
    for key in ("paper_only","authenticated_execution","real_order_submission"):
        if selected.get(key) is not SAFETY[key]:raise ValueError("runtime_authority_mismatch")
    return {"schema":SCHEMA,**SAFETY,"runtime_instance_id":selected["instance_id"],
            "target_instance_id":target["instance_id"],
            "runtime_az":selected.get("availability_zone"),"region":REGION,
            "runtime_model_sha":model,"runtime_release_sha":release,"runtime_root":root,
            "runtime_started_at":selected.get("service_started_at"),"service_active":selected.get("unit_active"),
            "tailscale_ips":selected.get("tailscale_ips",[]),"collection_root":target["collection_root"],
            "app":selected["app"],"selection_reason":"CANONICAL_TARGET_AND_OBSERVED_RELEASE",
            "observed_at_ms":int(time.time()*1000) if now_ms is None else now_ms}


def read_receipt(path, now_ms=None):
    v=json.loads(path.read_text());now=int(time.time()*1000) if now_ms is None else now_ms
    if v.get("schema")!=SCHEMA or any(v.get(k) is not x for k,x in SAFETY.items()):raise ValueError("identity_receipt_contract")
    stamp=v.get("observed_at_ms")
    if type(stamp) is not int or not 0<=now-stamp<=300000:raise ValueError("identity_receipt_stale")
    if not re.fullmatch(r"i-[0-9a-f]{17}",v.get("runtime_instance_id","")):raise ValueError("identity_receipt_instance")
    if v.get("target_instance_id") != v["runtime_instance_id"] or v.get("region") != REGION:
        raise ValueError("identity_receipt_target")
    root=v.get("runtime_root")
    if not isinstance(root,str) or not root.startswith("/mnt/polymarket-data/") or ".." in Path(root).parts:
        raise ValueError("identity_receipt_root")
    if not exact_sha(v.get("runtime_model_sha")) or v["runtime_model_sha"]!=v.get("runtime_release_sha"):
        raise ValueError("identity_receipt_sha")
    return v


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target",type=Path,default=Path("deploy/london/runtime_identity.json"))
    p.add_argument("--expected-sha",default="");p.add_argument("--output",type=Path,required=True)
    p.add_argument("--github-output",type=Path);a=p.parse_args()
    target=validate_target(json.loads(a.target.read_text()))
    aws_json(REGION,["sts","get-caller-identity"])
    values=probe(REGION,[target["instance_id"]])
    # AZ is an EC2 fact, not an inference from an account-specific letter.
    ec2=aws_json(REGION,["ec2","describe-instances","--instance-ids",target["instance_id"]])
    instances=[i for r in ec2.get("Reservations",[]) for i in r.get("Instances",[])]
    if len(instances)!=1 or instances[0].get("InstanceId")!=target["instance_id"]:raise ValueError("runtime_ec2_identity")
    values[target["instance_id"]]["availability_zone"]=instances[0].get("Placement",{}).get("AvailabilityZone")
    receipt=resolve(target,values,a.expected_sha)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
    if a.github_output:
        with a.github_output.open("a") as stream:
            stream.write("sha="+receipt["runtime_model_sha"]+"\ninstance_id="+receipt["runtime_instance_id"]+"\n")
    print(json.dumps(receipt,sort_keys=True))


if __name__=="__main__":main()
