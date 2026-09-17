#!/usr/bin/env python3
"""Delete only exact-hash London segments proven copied to the research plane."""
from __future__ import annotations
import argparse,fnmatch,hashlib,json,os,time
from pathlib import Path

def digest(path:Path)->str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''):h.update(b)
    return h.hexdigest()

def atomic(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f'.tmp.{os.getpid()}'); tmp.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n'); os.replace(tmp,path)

def matches(rel:str,patterns:list[str])->bool: return any(fnmatch.fnmatch(rel,p) for p in patterns)

def run(root:Path,cfg:dict,dry_run:bool=False)->dict:
    if cfg.get('schema')!='polymarket_v7_london_buffer_retention_v1' or cfg.get('paper_only') is not True: raise ValueError('config')
    target=int(cfg['target_managed_bytes']); maximum=int(cfg['maximum_managed_bytes']); min_age=int(cfg['minimum_age_seconds']); lag=int(cfg['offload_safety_lag_seconds'])
    if not 0<target<maximum or min_age<300 or lag<60: raise ValueError('bounds')
    # Measure the actual managed buffer even when offload evidence is absent.
    # Missing permission to prune does not imply an empty buffer.
    never=set(cfg.get('never_delete') or []); patterns=list(cfg.get('closed_segment_patterns') or [])
    observed=[]; total=0
    for p in root.rglob('*'):
        if not p.is_file() or p.is_symlink(): continue
        rel=str(p.relative_to(root))
        if rel in never or not matches(rel,patterns): continue
        try: st=p.stat()
        except FileNotFoundError: continue  # Concurrent segment rotation; next pass observes the new name.
        total+=st.st_size; observed.append((p,rel,st))
    receipt_path=root/str(cfg['offload_receipt']); receipt=json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    if receipt.get('schema')!='polymarket_v7_research_offload_receipt_v1':
        state='NO_VERIFIED_OFFLOAD' if total<=maximum else 'BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED'
        return {'schema':'polymarket_v7_london_buffer_retention_status_v1','timestamp':int(time.time()),'paper_only':True,'state':state,'before_bytes':total,'after_bytes':total,'target_bytes':target,'maximum_bytes':maximum,'deleted':[]}
    indexed={str(x['path']):x for x in receipt.get('files',[]) if isinstance(x,dict)}; through=int(receipt.get('synced_through_ns') or 0)-lag*1_000_000_000
    now=time.time_ns(); candidates=[]
    for p,rel,st in observed:
        # A matching filename and a historical copy receipt cannot close a live
        # writer. Active .open segments and unsegmented .bin tapes never prune.
        if rel.endswith('.open') or (rel.endswith('.bin') and '.segment-' not in Path(rel).name): continue
        meta=indexed.get(rel)
        if not meta or st.st_mtime_ns>through or now-st.st_mtime_ns<min_age*1_000_000_000: continue
        if int(meta.get('size') or -1)!=st.st_size or str(meta.get('sha256') or '')!=digest(p): continue
        candidates.append((st.st_mtime_ns,p,rel,st.st_size))
    before=total; deleted=[]
    if total>target:
        for _,p,rel,size in sorted(candidates):
            if total<=target: break
            if not dry_run: p.unlink()
            total-=size; deleted.append({'path':rel,'bytes':size})
    state='OK' if total<=maximum else 'BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED'
    return {'schema':'polymarket_v7_london_buffer_retention_status_v1','timestamp':int(time.time()),'paper_only':True,'state':state,'before_bytes':before,'after_bytes':total,'target_bytes':target,'maximum_bytes':maximum,'deleted':deleted,'synced_through_ns':int(receipt.get('synced_through_ns') or 0)}

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--run-root',type=Path,required=True);ap.add_argument('--config',type=Path,required=True);ap.add_argument('--dry-run',action='store_true');a=ap.parse_args();v=run(a.run_root.resolve(),json.loads(a.config.read_text()),a.dry_run);atomic(a.run_root/'control/london_buffer_retention_status.json',v);print(json.dumps(v,sort_keys=True));return 0 if v['state']!='BUFFER_LIMIT_EXCEEDED_UNSYNCED_DATA_PRESERVED' else 2
if __name__=='__main__':raise SystemExit(main())
