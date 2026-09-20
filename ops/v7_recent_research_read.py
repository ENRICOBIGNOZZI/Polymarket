#!/usr/bin/env python3
"""Read-only recent London evidence audit. Only encrypted private output leaves SSM."""
import base64
import json
from pathlib import Path
import shlex
import sys
from v7_london_ssm_deploy import run, REGION

REMOTE = r'''
import base64,gzip,json,os,shlex,subprocess,tempfile,time
from pathlib import Path
now=time.time();start=START_MS/1000
unit='polymarket-v7-paper.service'
env=subprocess.check_output(['systemctl','show',unit,'-p','Environment','--value'],text=True)
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT=')))
status=json.loads((root/'control/runtime_status.json').read_text())
assert status.get('paper_only') is True and status.get('authenticated_execution') is False and status.get('real_order_submission') is False
files=[];samples=[];statuses=[]
for p in root.rglob('*'):
 if p.is_symlink() or not p.is_file():continue
 rel=str(p.relative_to(root));name=p.name
 if any(x in name.lower() for x in ('.env','secret','credential','.pem','.key')):continue
 if p.stat().st_mtime<start:continue
 if ('native' in rel or 'probability' in rel or 'settlement' in rel or 'crypto' in rel) and p.suffix in ('.json','.jsonl','.gz'):
  files.append(dict(path=rel,bytes=p.stat().st_size,modified=p.stat().st_mtime))
  if p.suffix=='.json' and 'status' in name and p.stat().st_size<100000 and len(statuses)<3:
   try:statuses.append(dict(path=rel,value=json.loads(p.read_text())))
   except ValueError:pass
  if '.jsonl' in name and len(samples)<6:
   try:
    with (gzip.open(p,'rt') if name.endswith('.gz') else p.open()) as f:
     for index,line in enumerate(f):
      if index>500:break
      v=json.loads(line)
      if len(samples)<6 and not any(s['value'].get('kind')==v.get('kind') for s in samples):samples.append(dict(path=rel,value=v))
   except (ValueError,OSError):pass
result=dict(root=str(root),start_ms=int(start*1000),end_ms=int(now*1000),runtime=status,
 file_count=len(files),files=files[:120],samples=samples,statuses=statuses)
payload=gzip.compress(json.dumps(result,separators=(',',':')).encode())
with tempfile.TemporaryDirectory(prefix='pm-readonly-research-') as tmp:
 cert=Path(tmp)/'recipient.pem';cert.write_bytes(base64.b64decode(CERT))
 encrypted=subprocess.check_output(['openssl','cms','-encrypt','-binary','-aes-256-cbc','-outform','DER',str(cert)],input=payload)
 encoded=base64.b64encode(encrypted).decode()
 if len(encoded)>23000:raise ValueError('encrypted_audit_too_large')
 print('ENCRYPTED_RESEARCH='+encoded)
'''


def main():
    req=json.loads(Path(sys.argv[1]).read_text())
    assert req['schema']=='v7_recent_research_read_v1' and req['operation']=='audit'
    assert req['paper_only'] is True and req['authenticated_execution'] is False and req['real_order_submission'] is False
    assert isinstance(req['start_ms'],int) and req['start_ms']>0
    cert=base64.b64encode(req['recipient_certificate'].encode()).decode()
    source='START_MS='+str(req['start_ms'])+'\nCERT='+repr(cert)+'\n'+REMOTE
    command='nice -n 15 python3 -c '+shlex.quote(source)
    stdout,_=run(REGION,'i-0fba2bac9fdc5cbeb',command,120)
    lines=[line for line in stdout.splitlines() if line.startswith('ENCRYPTED_RESEARCH=')]
    assert len(lines)==1
    print(lines[0])


if __name__=='__main__':main()
