#!/usr/bin/env python3
"""Read-only aggregate audit of the London PAPER retention timer/service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex

from v7_london_ssm_deploy import REGION, SsmDeployError, run

INSTANCE="i-0fba2bac9fdc5cbeb"

REMOTE=r"""
import json,shlex,subprocess,time
from pathlib import Path

def show(unit, props):
    command=['systemctl','show',unit]
    for prop in props:
        command.extend(['-p',prop])
    raw=subprocess.check_output(command,text=True)
    out={}
    for line in raw.splitlines():
        if '=' in line:
            key,value=line.split('=',1)
            out[key]=value
    return out

paper=show('polymarket-v7-paper.service',['Environment','ActiveState','SubState'])
env=paper.get('Environment','')
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT='))).resolve()
runtime=json.loads((root/'control/runtime_status.json').read_text())
assert runtime.get('paper_only') is True
assert runtime.get('authenticated_execution') is False
assert runtime.get('real_order_submission') is False

timer=show('polymarket-v7-retention.timer',[
    'LoadState','ActiveState','SubState','UnitFileState',
    'Result','LastTriggerUSec','NextElapseUSecRealtime'
])
service=show('polymarket-v7-retention.service',[
    'LoadState','ActiveState','SubState','UnitFileState','Result',
    'ExecMainCode','ExecMainStatus','ExecMainStartTimestamp',
    'ExecMainExitTimestamp','WorkingDirectory','FragmentPath'
])
status_path=root/'control/london_buffer_retention_status.json'
retention={}
status_age_seconds=None
if status_path.is_file() and not status_path.is_symlink():
    try:
        retention=json.loads(status_path.read_text())
    except (OSError,ValueError):
        retention={'parse_error':True}
    try:
        status_age_seconds=max(0.0,time.time()-status_path.stat().st_mtime)
    except OSError:
        pass

journal=subprocess.run(
    ['journalctl','-u','polymarket-v7-retention.service','-n','60',
     '--no-pager','-o','cat'],
    text=True,capture_output=True,check=False,
)
lines=[]
for line in journal.stdout.splitlines():
    # Retention logs should contain paths/states only. Bound and redact any
    # accidental long tokens before publishing to CI.
    line=line[:500]
    for marker in ('AWS_','SECRET','TOKEN','PASSWORD','PRIVATE KEY'):
        if marker in line.upper():
            line='[REDACTED_SENSITIVE_LINE]'
            break
    lines.append(line)

result={
    'schema':'polymarket_v7_retention_readonly_audit_v1',
    'paper_only':True,
    'authenticated_execution':False,
    'real_order_submission':False,
    'runtime_sha':runtime.get('model_sha'),
    'run_root_name':root.name,
    'timer':timer,
    'service':service,
    'retention_status_present':status_path.is_file(),
    'retention_status_age_seconds':status_age_seconds,
    'retention_state':retention.get('state'),
    'retention_timestamp':retention.get('timestamp'),
    'retention_before_bytes':retention.get('before_bytes'),
    'retention_after_bytes':retention.get('after_bytes'),
    'retention_managed_files':retention.get('managed_files'),
    'retention_verified_offload_receipt':retention.get('verified_offload_receipt'),
    'rolling_state':(retention.get('rolling_retirement') or {}).get('state'),
    'rolling_failures_count':len((retention.get('rolling_retirement') or {}).get('failures') or []),
    'compression_failures_count':len((retention.get('lossless_compression') or {}).get('failures') or []),
    'journal_tail':lines[-30:],
}
print('RETENTION_AUDIT='+json.dumps(result,sort_keys=True,separators=(',',':')))
"""

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    command="python3 -c "+shlex.quote(REMOTE)
    try:
        stdout,stderr=run(REGION,INSTANCE,command,120)
    except SsmDeployError as exc:
        parser.exit(2,f"retention audit failed: {exc}\n")
    marker=next(
        (line for line in stdout.splitlines() if line.startswith("RETENTION_AUDIT=")),
        None,
    )
    if marker is None:
        parser.exit(2,"retention audit marker missing\n")
    value=json.loads(marker.split("=",1)[1])
    assert value["paper_only"] is True
    assert value["authenticated_execution"] is False
    assert value["real_order_submission"] is False
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(value,sort_keys=True,indent=2)+"\n")
    print("retention_audit_result=success")
    print("retention_state="+str(value.get("retention_state")))
    print("retention_status_age_seconds="+str(value.get("retention_status_age_seconds")))
    print("retention_timer_active="+str((value.get("timer") or {}).get("ActiveState")))
    print("retention_service_result="+str((value.get("service") or {}).get("Result")))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
