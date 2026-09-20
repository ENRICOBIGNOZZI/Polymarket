#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, sys, time, urllib.request
from pathlib import Path

CI_RE = re.compile(r"^(?:github-runner|gh-(?:deploy|health|freeze|inventory)-)", re.I)

def api_json(url: str, token: str, method: str = "GET") -> dict:
    req=urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "polymarket-v7-tailscale-cleanup",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        body=resp.read()
    if not body:
        return {}
    value=json.loads(body)
    if not isinstance(value,dict):
        raise RuntimeError("tailscale_api_object_required")
    return value

def hostname(d: dict) -> str:
    return str(d.get("hostname") or d.get("name") or "")

def online(d: dict) -> bool:
    for key in ("online","connectedToControl","connected"):
        if key in d:
            return bool(d.get(key))
    return False

def os_name(d: dict) -> str:
    return str(d.get("os") or "").lower()

def addresses(d: dict) -> list[str]:
    vals=d.get("addresses") or d.get("tailscaleIPs") or []
    return [str(x) for x in vals] if isinstance(vals,list) else []

def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--request", type=Path, required=True)
    p.add_argument("--token", required=True)
    a=p.parse_args()
    v=json.loads(a.request.read_text(encoding="utf-8"))
    required={"schema","version","expected_parent_sha","maximum_deletions","preserve_ips"}
    if set(v) != required or v["schema"] != "polymarket_v7_tailscale_cleanup_request_v1" or v["version"] != 1:
        p.error("request_contract")
    maximum=int(v["maximum_deletions"])
    if not 1 <= maximum <= 1200:
        p.error("maximum_deletions")
    preserve={str(x) for x in v["preserve_ips"]}
    listing=api_json("https://api.tailscale.com/api/v2/tailnet/-/devices", a.token)
    devices=listing.get("devices")
    if not isinstance(devices,list):
        raise SystemExit("device_list_missing")
    candidates=[]
    persistent=[]
    for d in devices:
        if not isinstance(d,dict):
            continue
        h=hostname(d)
        ips=addresses(d)
        is_ci=bool(CI_RE.match(h))
        if not is_ci:
            persistent.append({"hostname":h,"os":os_name(d),"online":online(d),"ips":ips})
            continue
        if online(d):
            continue
        if os_name(d) not in ("linux",""):
            continue
        if preserve.intersection(ips):
            continue
        device_id=d.get("id")
        if not isinstance(device_id,(str,int)) or not str(device_id):
            continue
        candidates.append((str(device_id),h,ips))
    if len(candidates) > maximum:
        raise SystemExit(f"candidate_count_exceeds_limit:{len(candidates)}>{maximum}")
    removed=[]
    for device_id,h,ips in candidates:
        api_json(f"https://api.tailscale.com/api/v2/device/{device_id}", a.token, method="DELETE")
        removed.append({"hostname":h,"ips":ips})
        time.sleep(0.03)
    after=api_json("https://api.tailscale.com/api/v2/tailnet/-/devices", a.token)
    remaining=[]
    for d in after.get("devices") or []:
        if not isinstance(d,dict):
            continue
        if CI_RE.match(hostname(d)) and not online(d):
            remaining.append(hostname(d))
    print(json.dumps({
        "schema":"polymarket_v7_tailscale_cleanup_result_v1",
        "before_total":len(devices),
        "candidate_count":len(candidates),
        "removed_count":len(removed),
        "remaining_offline_ci_count":len(remaining),
        "persistent_devices":persistent,
    },sort_keys=True))
    if remaining:
        raise SystemExit("offline_ci_devices_remain")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
