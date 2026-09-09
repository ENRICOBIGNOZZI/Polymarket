#!/usr/bin/env python3
"""Incremental permanent source preservation in the existing economics loop.

The store sits outside every cutover run. Every immutable revision is readable
without its old model, original path or SQLite index. Collection budgets bound
work per pass; they defer evidence capture, never authorize deletion.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import time
from v7_evidence_catalog import classify
from v7_evidence_store import AUTH, EvidenceStore, canonical, digest, immutable
from v7_evidence_capacity import allocated_data_bytes


def load(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}


def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+f'.tmp.{os.getpid()}')
    with tmp.open('w') as f:json.dump(value,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


def sources(run_root,durable_root,archive_root,repository_root):
    permanent_root=(durable_root/'permanent_evidence').resolve()
    roots=[('live',run_root),('durable',durable_root)]
    roots.extend(('archive',p) for p in sorted(archive_root.glob('cutover-*')) if p.is_dir())
    universe=run_root.parent/'v7-universe'
    if universe.exists():roots.append(('universe',universe))
    if repository_root is not None:roots.append(('configuration',repository_root/'config'))
    entries=[]
    for scope,root in roots:
        runtime=load(root/'control/runtime_status.json') if scope in ('live','archive') else {}
        generation=runtime.get('run_id') or (root.name if scope=='archive' else scope)
        partition=f'{scope if scope not in ("archive","live") else "run"}:{generation}'
        for folder,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(folder)/d).is_symlink()
                     and not (Path(folder)/d).resolve().is_relative_to(permanent_root)]
            for name in files:
                p=Path(folder)/name
                if p.is_symlink() or '.tmp.' in name or name.endswith(('.lock','-wal','-shm')):continue
                if name in {'permanent_evidence_status.json','collection_cursor.json','index.sqlite'}:continue
                rel=str(p.relative_to(root));definition=classify(rel)
                kind=definition['source_family']
                priority=0 if kind in {'canonical_ledger','coordinator_decisions','authorization','markouts','profit_experiments','fair_predictions'} else 1
                contract={k:definition[k] for k in ['source_family','schema','source_kind','timestamp_semantics','model_dependence']}
                contract['generation']={'run_id':runtime.get('run_id'),'code_sha':runtime.get('model_sha'),
                    'config_hash':runtime.get('config_hash'),'policy_hash':runtime.get('policy_hash'),
                    'identity_source':'runtime generation metadata; not substituted for record-specific model identities'}
                if scope=='configuration':contract.update(source_family='configuration',model_dependence='EXPLICIT_CONFIG_VERSION')
                # Append semantics require a declared append stream, never a
                # mutable JSON snapshot merely because its filename is known.
                append=(('.jsonl' in name or '.csv' in name or '.log' in name or '.bin' in name)
                        and not name.endswith('.gz') and definition['source_kind'] not in {'DERIVED_MODEL_OR_FEATURE'})
                entries.append((priority,partition,rel,p,contract,append))
    return sorted(entries,key=lambda x:(x[0],x[1],x[2]))


def collect(run_root,durable_root,archive_root,repository_root=None,*,maximum_seconds=40,maximum_bytes=512*1024**2,
            maximum_total_data_bytes=30_000_000_000):
    run_root=Path(run_root).resolve();durable_root=Path(durable_root).resolve();archive_root=Path(archive_root).resolve()
    permanent=durable_root/'permanent_evidence';permanent.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();now=time.time_ns()
    usage=allocated_data_bytes([run_root.parent])
    # This guards additional archival copies. Producer storage remains separately
    # measured: do not misreport a paused backfill as enforcement on all writers.
    copy_budget=max(0,maximum_total_data_bytes-usage-256*1024**2)
    maximum_bytes=min(maximum_bytes,copy_budget)
    budget={'all_data_allocated_bytes':usage,'maximum_total_data_bytes':maximum_total_data_bytes,
            'copy_budget_bytes':copy_budget,'producer_budget_enforcement':'NOT_ATTESTED_BY_COPY_GUARD'}
    if maximum_bytes<=0:
        status={'schema':'polymarket_v7_permanent_evidence_status_v1',**AUTH,'timestamp_ms':now//1_000_000,
                'state':'ARCHIVE_COPY_DEFERRED_DATA_BUDGET','data_budget':budget,
                'no_unique_source_deletion':True,'backfill_pass_complete':False}
        atomic(run_root/'permanent_evidence_status.json',status)
        return status
    entries=sources(run_root,durable_root,archive_root,repository_root)
    counts=Counter();errors=[];new_bytes=0;captured=[];considered=0
    with EvidenceStore(permanent/'store') as store:
        # Round-robin cursor prevents frequently changing snapshots from
        # starving raw historical backfill. All unprocessed sources stay intact.
        cursor_path=permanent/'collection_cursor.json';cursor=load(cursor_path).get('next_key')
        if cursor:
            pivot=next((i for i,x in enumerate(entries) if str(x[0])+'|'+x[1]+'|'+x[2]>=cursor),0)
            entries=entries[pivot:]+entries[:pivot]
        next_key=None
        for priority,partition,rel,path,contract,append in entries:
            if time.monotonic()-started>=maximum_seconds or new_bytes>=maximum_bytes:
                next_key=str(priority)+'|'+partition+'|'+rel;break
            considered+=1
            try:
                if path.suffix=='.gz' and path.stat().st_size>maximum_bytes-new_bytes:
                    counts['CLOSED_GZIP_COPY_DEFERRED_BUDGET']+=1
                    continue
                result=store.capture(path,partition=partition,relative=rel,contract=contract,append=append,
                                     maximum_bytes=min(64*1024**2,max(1,maximum_bytes-new_bytes)))
                counts[result['state']]+=1;new_bytes+=result.get('new_bytes',0)
                if result['state']=='CAPTURED':captured.append(result)
            except (OSError,ValueError,EOFError) as exc:
                errors.append({'path':str(path),'state':'SOURCE_CAPTURE_DEFERRED','reason':str(exc)})
        atomic(cursor_path,{'next_key':next_key,'observed_ns':now})
        indexed=store.db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        revisions=store.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]
        incomplete=store.db.execute("SELECT COUNT(*) FROM sources WHERE state LIKE '%\"capture_complete\":false%'").fetchone()[0]
    # Immutable receipts prove which source revisions were actually preserved.
    receipt={'schema':'polymarket_v7_permanent_collection_receipt_v1',**AUTH,'observed_ns':now,
      'source_revisions':captured,'counts':dict(counts),'errors':errors}
    payload=canonical(receipt);receipt_path=permanent/'catalog'/('collection-'+str(now)+'-'+digest(payload)[:12]+'.json')
    immutable(receipt_path,payload)
    disk=shutil.disk_usage(durable_root)
    status={'schema':'polymarket_v7_permanent_evidence_status_v1',**AUTH,'timestamp_ms':now//1_000_000,
      'sources_discovered':len(entries),'sources_considered':considered,'sources_indexed':indexed,'source_revisions':revisions,
      'counts':dict(counts),'new_uncompressed_bytes':new_bytes,'capture_errors':errors,'receipt':str(receipt_path),
      'incomplete_source_prefixes':incomplete,'backfill_pass_complete':next_key is None and incomplete==0 and not counts['CLOSED_GZIP_COPY_DEFERRED_BUDGET'],'next_source':next_key,'collection_seconds':time.monotonic()-started,
      'disk_free_bytes':disk.free,'disk_total_bytes':disk.total,'no_unique_source_deletion':True,
      'data_budget':budget,
      'state':'CAPTURE_ERRORS_VISIBLE' if errors else 'BACKFILL_IN_PROGRESS' if next_key or incomplete or counts['CLOSED_GZIP_COPY_DEFERRED_BUDGET'] else 'COLLECTING'}
    atomic(run_root/'permanent_evidence_status.json',status)
    return status


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--run-root',type=Path,required=True)
    ap.add_argument('--durable-root',type=Path,required=True);ap.add_argument('--archive-root',type=Path,required=True)
    ap.add_argument('--repository-root',type=Path);ap.add_argument('--maximum-seconds',type=float,default=40)
    ap.add_argument('--maximum-bytes',type=int,default=512*1024**2)
    a=ap.parse_args();print(json.dumps(collect(a.run_root,a.durable_root,a.archive_root,a.repository_root,
      maximum_seconds=a.maximum_seconds,maximum_bytes=a.maximum_bytes),sort_keys=True))

if __name__=='__main__':main()
