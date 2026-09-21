#!/usr/bin/env python3
"""Terminate one stale decomposed benchmark generation on London, and nothing else."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path
from v7_london_ssm_deploy import REGION, run

INSTANCE_RE=re.compile(r"^i-[0-9a-f]+$")
PREFIX_RE=re.compile(r"^/tmp/polymarket-decomposed-benchmark-[0-9a-f]{12}$")

def main()->int:
    p=argparse.ArgumentParser()
    p.add_argument("--instance-id",required=True)
    p.add_argument("--stale-prefix",required=True)
    p.add_argument("--active-prefix",required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    if not INSTANCE_RE.fullmatch(a.instance_id): p.error("invalid instance")
    if not PREFIX_RE.fullmatch(a.stale_prefix): p.error("invalid stale prefix")
    if not PREFIX_RE.fullmatch(a.active_prefix): p.error("invalid active prefix")
    if a.stale_prefix==a.active_prefix: p.error("stale and active prefixes must differ")
    cmd=f'''set -euo pipefail
STALE={a.stale_prefix!r}
ACTIVE={a.active_prefix!r}
PAPER=polymarket-v7-paper.service
before="$(systemctl show "$PAPER" -p MainPID --value)"
python3 - "$STALE" "$ACTIVE" <<'PY'
import os,signal,subprocess,sys,time
stale,active=sys.argv[1:]
rows=subprocess.run(
    ["ps","-eo","pid=,etime=,%cpu=,%mem=,args="],
    capture_output=True,text=True,check=True).stdout.splitlines()
targets=[]
active_rows=[]
for line in rows:
    line=line.strip()
    if not line: continue
    parts=line.split(None,4)
    if len(parts)<5: continue
    pid_text,etime,cpu,mem,args=parts
    if stale in args and active not in args:
        try: targets.append(int(pid_text))
        except ValueError: pass
    if active in args:
        active_rows.append({
            "pid":int(pid_text),"elapsed":etime,
            "cpu":float(cpu),"mem":float(mem),
        })
print("stale_targets="+",".join(map(str,targets)))
print("active_processes_json="+__import__("json").dumps(active_rows,separators=(",",":")))
for pid in targets:
    try: os.kill(pid,signal.SIGTERM)
    except ProcessLookupError: pass
deadline=time.time()+5
while time.time()<deadline:
    alive=[]
    for pid in targets:
        try: os.kill(pid,0); alive.append(pid)
        except ProcessLookupError: pass
    if not alive: break
    time.sleep(.1)
for pid in alive:
    try: os.kill(pid,signal.SIGKILL)
    except ProcessLookupError: pass
print("stale_killed="+str(len(targets)))
PY
rm -rf -- "$STALE"
after="$(systemctl show "$PAPER" -p MainPID --value)"
[[ "$before" == "$after" ]]
echo "STALE_BENCHMARK_CLEANUP=OK"
'''
    stdout,stderr=run(REGION,a.instance_id,cmd,120)
    marker=next((x for x in stdout.splitlines() if x=="STALE_BENCHMARK_CLEANUP=OK"),None)
    if marker is None: raise SystemExit("cleanup marker missing")
    killed=0
    targets=[]
    active_processes=[]
    for line in stdout.splitlines():
        if line.startswith("stale_killed="):
            killed=int(line.split("=",1)[1])
        elif line.startswith("stale_targets="):
            raw=line.split("=",1)[1]
            targets=[int(v) for v in raw.split(",") if v]
        elif line.startswith("active_processes_json="):
            active_processes=json.loads(line.split("=",1)[1])
    value={
        "stale_prefix":a.stale_prefix,
        "active_prefix":a.active_prefix,
        "paper_pid_unchanged":True,
        "stale_targets":targets,
        "stale_killed":killed,
        "active_processes":active_processes,
    }
    value["stdout_tail"]=stdout[-1000:]
    value["stderr_tail"]=stderr[-1000:]
    a.output.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n")
    print(json.dumps(value,sort_keys=True))
    return 0

if __name__=="__main__": raise SystemExit(main())
