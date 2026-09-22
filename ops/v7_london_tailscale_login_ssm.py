#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,re,subprocess,time

LOGIN_RE=re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9_-]+")

def aws(*args:str)->str:
    return subprocess.check_output(["aws",*args],text=True,stderr=subprocess.STDOUT)

def send(instance:str,region:str,script:str,timeout:int=120)->dict:
    enc=subprocess.check_output(["base64","-w0"],input=script,text=True).strip()
    wrapped=f"printf '%s' '{enc}' | base64 -d | bash"
    params=json.dumps({"commands":[wrapped],"executionTimeout":[str(timeout)]},separators=(",",":"))
    cid=aws("ssm","send-command","--instance-ids",instance,
            "--document-name","AWS-RunShellScript","--parameters",params,
            "--region",region,"--query","Command.CommandId","--output","text").strip()
    deadline=time.time()+timeout+30
    last={}
    while time.time()<deadline:
        try:
            raw=aws("ssm","get-command-invocation","--command-id",cid,
                    "--instance-id",instance,"--region",region,"--output","json")
            last=json.loads(raw)
        except Exception:
            time.sleep(1); continue
        status=str(last.get("Status") or "")
        if status=="Success":
            return last
        if status in {"Failed","TimedOut","Cancelled"}:
            raise RuntimeError("SSM_"+status+" stderr="+str(last.get("StandardErrorContent") or "")[-4000:])
        time.sleep(1)
    raise RuntimeError("SSM_TIMEOUT")

def begin(instance:str,region:str)->int:
    script=r"""
set -euo pipefail
systemctl enable --now tailscaled
if tailscale status --json 2>/dev/null | python3 -c 'import json,sys; s=(json.load(sys.stdin).get("Self") or {}); raise SystemExit(0 if (s.get("TailscaleIPs") or []) else 1)'; then
  echo ALREADY_AUTHENTICATED=1
  tailscale status --json | python3 -c 'import json,sys; s=(json.load(sys.stdin).get("Self") or {}); print("TAILSCALE_IP="+str((s.get("TailscaleIPs") or [""])[0])); print("DNS="+str(s.get("DNSName") or ""))'
  exit 0
fi
set +e
tailscale login --timeout=8s 2>&1
rc=$?
set -e
echo LOGIN_COMMAND_RC=$rc
exit 0
"""
    v=send(instance,region,script,60)
    out=str(v.get("StandardOutputContent") or "")+"\n"+str(v.get("StandardErrorContent") or "")
    print(out)
    m=LOGIN_RE.search(out)
    if m:
        print("TAILSCALE_LOGIN_URL="+m.group(0))
        return 0
    if "ALREADY_AUTHENTICATED=1" in out:
        return 0
    raise RuntimeError("TAILSCALE_LOGIN_URL_NOT_FOUND")

def finish(instance:str,region:str)->int:
    script=r"""
set -euo pipefail
systemctl enable --now tailscaled
tailscale up --hostname=polymarket-london --accept-dns=false --accept-routes=false --timeout=30s
curl -fsS --max-time 5 http://127.0.0.1:9090/-/ready
metric="$(curl -fsS --max-time 8 --get --data-urlencode 'query=polymarket_pure_arb_up' http://127.0.0.1:9090/api/v1/query)"
python3 - "$metric" <<'PY'
import json,sys
v=json.loads(sys.argv[1])
rows=v.get('data',{}).get('result',[])
assert rows and any(float(x['value'][1])==1.0 for x in rows), v
print('LOCAL_PROMETHEUS_PURE_ARB_OK=1')
PY
tailscale serve --bg --tcp=19091 tcp://127.0.0.1:9090
ip="$(tailscale ip -4 | head -1)"
test -n "$ip"
echo "LONDON_TAILSCALE_IP=$ip"
tailscale status --json | python3 -c 'import json,sys; s=(json.load(sys.stdin).get("Self") or {}); print("LONDON_TAILSCALE_DNS="+str(s.get("DNSName") or "")); print("LONDON_HOST="+str(s.get("HostName") or ""))'
tailscale serve status
curl -fsS --max-time 8 --get --data-urlencode 'query=polymarket_pure_arb_up' "http://$ip:19091/api/v1/query" >/tmp/v7-tailnet-prom.json
python3 - <<'PY'
import json
v=json.load(open('/tmp/v7-tailnet-prom.json'))
rows=v.get('data',{}).get('result',[])
assert rows and any(float(x['value'][1])==1.0 for x in rows), v
print('TAILNET_PROMETHEUS_PURE_ARB_OK=1')
PY
echo '=== PROTECTED PEER 100.65.195.111 ==='
tailscale status --json | python3 -c 'import json,sys; v=json.load(sys.stdin); [print(json.dumps({k:p.get(k) for k in ("HostName","DNSName","TailscaleIPs","Online","Active","Expired","Tags")},sort_keys=True)) for p in (v.get("Peer") or {}).values() if "100.65.195.111" in [str(x) for x in (p.get("TailscaleIPs") or [])]]'
"""
    v=send(instance,region,script,120)
    out=str(v.get("StandardOutputContent") or "")
    print(out)
    if "TAILNET_PROMETHEUS_PURE_ARB_OK=1" not in out:
        raise RuntimeError("TAILNET_PROMETHEUS_VERIFY_FAILED")
    return 0

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--instance-id",required=True)
    p.add_argument("--region",default="eu-west-2")
    p.add_argument("phase",choices=("begin","finish"))
    a=p.parse_args()
    return begin(a.instance_id,a.region) if a.phase=="begin" else finish(a.instance_id,a.region)

if __name__=="__main__":
    raise SystemExit(main())
