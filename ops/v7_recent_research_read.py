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




EXPORT = r"""
import base64,gzip,hashlib,json,shlex,subprocess,sys,tempfile
from pathlib import Path
unit='polymarket-v7-paper.service'
env=subprocess.check_output(['systemctl','show',unit,'-p','Environment','--value'],text=True)
root=Path(next(v.split('=',1)[1] for v in shlex.split(env) if v.startswith('PM_V7_RUN_ROOT=')))
status=json.loads((root/'control/runtime_status.json').read_text())
assert status.get('paper_only') is True and status.get('authenticated_execution') is False and status.get('real_order_submission') is False
folder=Path('/tmp')/('pm-recent-research-'+str(START)+'-'+str(END));folder.mkdir(mode=0o700,exist_ok=True)
source=gzip.decompress(base64.b64decode(SOURCE)).decode();namespace={'__name__':'recent_adapter'};exec(compile(source,'recent_adapter.py','exec'),namespace)
summary=namespace['build'](root,START,END,folder/'data.json.gz',delete_old=DELETE_OLD)
cert=folder/'recipient.pem';cert.write_bytes(base64.b64decode(CERT))
subprocess.run(['openssl','cms','-encrypt','-binary','-aes-256-cbc','-outform','DER','-in',str(folder/'data.json.gz'),'-out',str(folder/'encrypted.der'),str(cert)],check=True)
p=folder/'encrypted.der';payload=p.read_bytes()
print('RESEARCH_EXPORT='+json.dumps({'path':str(p),'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}))
"""


def export(request_path):
    import gzip,hashlib
    from concurrent.futures import ThreadPoolExecutor
    req=json.loads(Path(request_path).read_text())
    assert req['operation']=='export_recent' and req['paper_only'] is True
    assert req['authenticated_execution'] is False and req['real_order_submission'] is False
    start,end=req['start_ms'],req['end_ms']
    assert isinstance(start,int) and isinstance(end,int) and 0<end-start<=24*3600*1000
    source=base64.b64encode(gzip.compress(Path('research/backtest/data.py').read_bytes())).decode()
    cert=base64.b64encode(req['recipient_certificate'].encode()).decode()
    code='START='+str(start)+'\nEND='+str(end)+'\nSOURCE='+repr(source)+'\nCERT='+repr(cert)+'\nDELETE_OLD='+repr(req.get('delete_closed_old_captures') is True)+'\n'+EXPORT
    stdout,_=run(REGION,'i-0fba2bac9fdc5cbeb','nice -n 15 python3 -c '+shlex.quote(code),900)
    info=json.loads(next(x.split('=',1)[1] for x in stdout.splitlines() if x.startswith('RESEARCH_EXPORT=')))
    assert 0<info['bytes']<=32*1024*1024
    assert info['path'].startswith('/tmp/pm-recent-research-') and info['path'].endswith('/encrypted.der')
    def get(offset):
        code="import base64;f=open("+repr(info['path'])+",'rb');f.seek("+str(offset)+");print(base64.b64encode(f.read(12000)).decode())"
        value,_=run(REGION,'i-0fba2bac9fdc5cbeb','python3 -c '+shlex.quote(code),60)
        return base64.b64decode(value.strip(),validate=True)
    with ThreadPoolExecutor(max_workers=4) as pool:payload=b''.join(pool.map(get,range(0,info['bytes'],12000)))
    assert len(payload)==info['bytes'] and hashlib.sha256(payload).hexdigest()==info['sha256']
    for index,offset in enumerate(range(0,len(payload),12000)):
        print('ENCRYPTED_RESEARCH_CHUNK_'+str(index)+'='+base64.b64encode(payload[offset:offset+12000]).decode())
    print('ENCRYPTED_RESEARCH_SHA256='+info['sha256'])


if __name__=='__main__':
    request=json.loads(Path(sys.argv[1]).read_text())
    if request.get('operation')=='export_recent':export(sys.argv[1])
    else:main()
