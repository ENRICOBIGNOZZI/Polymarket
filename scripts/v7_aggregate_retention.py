#!/usr/bin/env python3
"""Explicitly lossy, verified retirement of closed external tapes.

Canonical ledgers, decisions, labels, model identities and PM books are outside
this allowlist. Aggregates are descriptive observations, never tick replay or
execution evidence. All storage figures are measured; this worker is not a
filesystem quota on independent producers.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import time
from v7_evidence_capacity import allocated_data_bytes
from v7_evidence_store import AUTH, EvidenceStore, canonical, digest, immutable, fsync_dir
from v7_lossless_data_compaction import closed, file_hash, stable
from v7_permanent_evidence import atomic
from v7_storage_budget import MAX_MANAGED_DATA_BYTES, RETENTION_TARGET_BYTES, RETENTION_TRIGGER_BYTES

HEADER=struct.Struct('<8sIIq41s65s65s33s4x')
RAW=struct.Struct('<QQqqB3xI')
RECORD=struct.Struct('<QqQHHI')
EVENT=struct.Struct('<QQQBBH4xqqq10dbBBBB3x')
SCHEMA='polymarket_v7_lossy_external_aggregate_v1'
POLICY='USER_AUTHORIZED_CONTINUOUS_COLLECTION_WITH_OLDER_DETAIL_AGGREGATED_20260909'
METRICS=('bid','ask','bid_size','ask_size','trade_price','trade_size','mark_price','index_price','funding_rate','open_interest')
IMPLEMENTATION=Path(__file__).read_bytes()


def eligible(path, runs, now, age):
    path=Path(path).absolute();runs=Path(runs).absolute()
    if not path.is_relative_to(runs) or path.is_symlink() or not path.is_file():return False
    rel=path.relative_to(runs)
    if any((runs/Path(*rel.parts[:i])).is_symlink() for i in range(1,len(rel.parts))):return False
    if len(rel.parts)<4 or rel.parts[-3:-1] not in [('external_fair','raw'),('external_fair','normalized_events')]:return False
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.bin(?:\.gz)?',path.name):return False
    archived=rel.parts[0]=='paper_v7_archives' and len(rel.parts)==5 and re.fullmatch(r'cutover-[0-9a-f]{40}-[0-9]+-[0-9]+',rel.parts[1])
    segmented=rel.parts[0]=='paper_v7_live' and len(rel.parts)==4 and '.segment-' in path.name
    return bool((archived or segmented) and now-path.stat().st_mtime>=age)


def stats_add(stats, name, value, stamp):
    if not math.isfinite(value):raise ValueError('nonfinite normalized metric')
    if name not in stats:stats[name]=[1,value,value,value,value,stamp,value,stamp]
    else:
        s=stats[name];s[0]+=1;s[1]+=value;s[2]=min(s[2],value);s[3]=max(s[3],value)
        if stamp<s[5]:s[4:6]=[value,stamp]
        if stamp>=s[7]:s[6:8]=[value,stamp]


def summarize(path, *, interval_seconds=60, now_ns=None, minimum_age_seconds=3600):
    """Stream schema-3 tapes; reject unknown ABI/kinds/truncation before expiry."""
    checksum=hashlib.sha256();count=0;bins={};last_sequence=None;end_ns=0
    opener=gzip.open if path.suffix=='.gz' else open
    with opener(path,'rb') as f:
        def read(n):
            data=f.read(n);checksum.update(data);return data
        h=read(HEADER.size)
        if len(h)!=HEADER.size:raise ValueError('partial tape header')
        magic,version,width,created,sha,run,session,source=HEADER.unpack(h)
        if version!=3 or magic not in (b'PMV7RAW!',b'PMV7TAPE') or width!=(0 if magic==b'PMV7RAW!' else 544):raise ValueError('unsupported tape ABI')
        strings=[s.split(b'\0',1)[0].decode('ascii') for s in (sha,run,session,source)]
        if not re.fullmatch('[0-9a-f]{40}',strings[0]):raise ValueError('missing tape code identity')
        while True:
            r=read(RAW.size if magic==b'PMV7RAW!' else 544)
            if not r:break
            if len(r)!=(RAW.size if magic==b'PMV7RAW!' else 544):raise ValueError('partial sealed record')
            metrics={}; flags={};asset=None;kind=None
            if magic==b'PMV7RAW!':
                seq,epoch,mono,wall,venue,n=RAW.unpack(r)
                if n>2*1024**2:raise ValueError('invalid raw payload size')
                if len(read(n))!=n:raise ValueError('partial sealed payload')
            else:
                seq,mono,handle,record_kind,reserved,n=RECORD.unpack(r[:32])
                if record_kind!=2 or n!=EVENT.size:raise ValueError('unsupported normalized event kind or ABI')
                e=EVENT.unpack(r[32:32+n]);asset,source_seq,epoch,venue,kind,reserved=e[:6]
                exchange,receive,wall=e[6:9];values=e[9:19];side,mask,gap,stale,healthy=e[19:]
                if kind not in (1,2,3,4) or receive!=mono:raise ValueError('invalid normalized event')
                indexes=range(4) if kind==1 else range(4,6) if kind==2 else [i+6 for i in range(4) if mask&(1<<i)] if kind==4 else []
                metrics={METRICS[i]:values[i] for i in indexes}
                if kind==1 and values[0]>0 and values[1]>0:metrics['spread']=values[1]-values[0]
                if kind==2:
                    metrics['signed_trade_size']=values[5]*side
                    metrics['trade_price_times_native_size']=values[4]*values[5]
                flags={'gap_events':int(bool(gap)),'stale_events':int(bool(stale)),'unhealthy_events':int(not healthy),'missing_exchange_clock':int(exchange<=0)}
            if wall<=0 or mono<=0 or not 1<=venue<=6:raise ValueError('invalid receive clock or venue')
            regression=last_sequence is not None and seq<=last_sequence
            if regression and magic!=b'PMV7RAW!':raise ValueError('nonincreasing normalized tape sequence')
            key=(wall//(interval_seconds*10**9),venue,asset,kind,epoch)
            if key not in bins:
                if len(bins)>=100000:raise ValueError('aggregate cardinality exceeds bounded memory')
                bins[key]={'interval_start_ns':key[0]*interval_seconds*10**9,'venue_id':venue,'asset_handle':asset,'event_type':kind,'connection_epoch':epoch,'records':0,'payload_bytes':0,'first_receive_wall_ns':wall,'last_receive_wall_ns':wall,'first_sequence':seq,'last_sequence':seq,'sequence_gaps':0,'sequence_regressions':0,'flags':{},'metrics':{}}
            b=bins[key];b['records']+=1;b['payload_bytes']+=n;b['last_sequence']=seq
            b['sequence_regressions']+=int(regression)
            b['sequence_gaps']+=max(0,seq-last_sequence-1) if last_sequence is not None else 0
            b['first_receive_wall_ns']=min(wall,b['first_receive_wall_ns']);b['last_receive_wall_ns']=max(wall,b['last_receive_wall_ns'])
            for name,v in flags.items():b['flags'][name]=b['flags'].get(name,0)+v
            for name,v in metrics.items():stats_add(b['metrics'],name,v,(wall,seq))
            count+=1;last_sequence=seq;end_ns=max(end_ns,wall)
        if end_ns and (now_ns or time.time_ns())-end_ns<minimum_age_seconds*10**9:raise ValueError('recent observations protected')
    if sum(b['records'] for b in bins.values())!=count:raise ValueError('aggregate record conservation failed')
    return {'schema':SCHEMA,**AUTH,'policy':POLICY,'raw_detail_available':False,'interval_seconds':interval_seconds,
      'tape_schema_version':version,'tape_code_sha':strings[0],'tape_run_id':strings[1],'tape_session_id':strings[2],'tape_source':strings[3],
      'source_decoded_sha256':checksum.hexdigest(),'source_decoded_bytes':sum(b['payload_bytes'] for b in bins.values())+count*RAW.size+HEADER.size if magic==b'PMV7RAW!' else count*544+HEADER.size,
      'record_count':count,'bins':sorted(bins.values(),key=lambda b:(b['interval_start_ns'],b['venue_id'],b['asset_handle'] or 0,b['event_type'] or 0,b['connection_epoch'])),
      'metric_layout':['count','sum','minimum','maximum','first_value','first_receive_wall_ns_and_sequence','last_value','last_receive_wall_ns_and_sequence'],
      'scope':'RAW_TRANSPORT_COUNTS_ONLY' if magic==b'PMV7RAW!' else 'NORMALIZED_MARKET_STATISTICS_IN_VENUE_NATIVE_UNITS',
      'limitations':['Lossy: no tick replay, order-level queue reconstruction or exact latency replay.',
       'Raw ordinary/large queues can reorder sequences; forward gaps are observed jumps, not confirmed message loss.',
       'Raw payload content is not represented by transport counters; normalized market aggregates are separate sources.',
       'Numeric sums are event-weighted; snapshot sizes and open interest sums are not traded volume.',
       'Intervals are receive-wall-clock bins, never exchange-time or decision-time observations.',
       'Asset handles, venue, session, connection and code identity remain separate; no implicit cross-venue normalization.']}


def retire(path, runs, store, *, now=None, minimum_age_seconds=3600, check_closed=closed):
    """Called under store writer lock. Publish aggregate and tombstones before unlink."""
    now=time.time() if now is None else now
    if not eligible(path,runs,now,minimum_age_seconds):raise ValueError('source outside expiry allowlist')
    path=Path(path).absolute();before=path.lstat()
    if not check_closed(path):raise ValueError('source open or closure unverified')
    original_sha=file_hash(path)
    pack=store.root/'packs'/original_sha[:2]/(original_sha+'.pack')
    aliases=[path]
    # Other aliases are never inferred solely from a path spelling.
    if not hasattr(store,'_expiry_manifests'):
        store._expiry_manifests=[json.loads(m.read_text()) for m in (store.root/'pack_manifests').glob('*.json')]
    manifests=[]
    for value in store._expiry_manifests:
        if value.get('pack_sha256')==original_sha:
            manifests.append(value)
            for alias in value['source_aliases']:
                p=Path(alias)
                if p==pack:continue
                if p.exists() and p not in aliases:
                    if not eligible(p,runs,now,minimum_age_seconds) or file_hash(p)!=original_sha:raise ValueError('protected or changed source alias')
                    aliases.append(p)
    if pack.exists():
        if pack.is_symlink() or file_hash(pack)!=original_sha:raise ValueError('unsafe pack')
        aliases.append(pack)
    inode_groups=defaultdict(list)
    for p in aliases:
        st=p.lstat();inode_groups[(st.st_dev,st.st_ino)].append(p)
    if any(ps[0].stat().st_nlink!=len(ps) for ps in inode_groups.values()):raise ValueError('uninspected hardlink')
    age=now-before.st_mtime
    interval=3600 if age>=90*86400 else 900 if age>=7*86400 else 60
    aggregate=summarize(path,interval_seconds=interval,now_ns=int(now*1e9),minimum_age_seconds=minimum_age_seconds)
    aggregate.update(source_path=str(path),source_file_sha256=original_sha,implementation_sha256=digest(IMPLEMENTATION))
    # Only exact, whole decoded source representations can be retired here.
    # Prefix/overwritten histories remain retained until separately summarized.
    candidates=[];refs=[]
    alias_names={str(p) for p in aliases}
    # The writer lock held by the caller keeps this index stable for the pass.
    if not hasattr(store,'_expiry_revisions'):
        store._expiry_revisions=[(p.stem,store.revision(p.stem)) for p in (store.root/'revisions').glob('*/*.json')]
    all_revisions=store._expiry_revisions
    for sha,value in all_revisions:
        if value['original_path_at_capture'] in alias_names:
            if value.get('contract',{}).get('source_family') not in {'external_raw','external_normalized'}:raise ValueError('source has protected or unknown catalog contract')
            if value.get('previous_revision') or len(value['chunks'])!=1:raise ValueError('captured prefix needs separate aggregation')
            ref=value['chunks'][0]
            if ref['offset']!=0 or ref['bytes']!=aggregate['source_decoded_bytes'] or ref['sha256']!=aggregate['source_decoded_sha256']:raise ValueError('captured revision differs from aggregate source')
            candidates.append((sha,value));refs.append(ref)
    retired_ids={v['source_id'] for _,v in candidates}
    candidate_shas={s for s,_ in candidates}
    for sha,v in all_revisions:
        if v['source_id'] in retired_ids and sha not in candidate_shas:raise ValueError('unaggregated source revision')
    protected_objects={r['sha256'] for _,v in all_revisions if v['source_id'] not in retired_ids and not (store.root/'retired_sources'/(v['source_id']+'.json')).exists() for r in v['chunks']}
    locator_paths=[]
    if pack.exists():
        if not hasattr(store,'_expiry_locators'):
            store._expiry_locators=[(p,json.loads(p.read_text())) for p in (store.root/'objects').glob('*/*.locator.json')]
        for locator,ref in store._expiry_locators:
            if ref.get('pack_sha256')==original_sha:
                if ref['object_sha256'] in protected_objects:raise ValueError('pack has protected object references')
                locator_paths.append(locator)
    encoded=canonical(aggregate);aggregate_sha=digest(encoded)
    destination=store.root/'aggregates'/aggregate_sha[:2]/(aggregate_sha+'.json.gz')
    immutable(destination,gzip.compress(encoded,compresslevel=9,mtime=0))
    if gzip.decompress(destination.read_bytes())!=encoded:raise ValueError('aggregate verification failed')
    immutable(store.root/'implementation_sources'/(digest(IMPLEMENTATION)+'.py'),IMPLEMENTATION)
    receipt={'schema':'polymarket_v7_raw_expiry_receipt_v1',**AUTH,'policy':POLICY,'raw_detail_available':False,
      'aggregate':str(destination.relative_to(store.root)),'aggregate_sha256':aggregate_sha,
      'source_file_sha256':original_sha,'source_decoded_sha256':aggregate['source_decoded_sha256'],
      'source_paths':[str(p) for p in aliases],'source_ids':sorted(retired_ids),'revisions':[s for s,_ in candidates],
      'record_count':aggregate['record_count'],'retired_at_ns':int(now*1e9)}
    receipt_path=store.root/'expiry_receipts'/(aggregate_sha+'.json')
    identities={p:stable(p.lstat()) for p in aliases}
    if stable(path.lstat())!=stable(before) or not all(check_closed(p) for p in aliases):raise ValueError('source changed or opened during aggregation')
    immutable(receipt_path,canonical(receipt))
    for source_id in retired_ids:immutable(store.root/'retired_sources'/(source_id+'.json'),canonical(receipt))
    # Durable intent makes interrupted deletion recoverable and readers explicit.
    for p in aliases:
        if stable(p.lstat())!=identities[p]:raise ValueError('alias changed during retirement')
        p.unlink();fsync_dir(p.parent)
    for ref in refs:
        if ref['sha256'] not in protected_objects:
            obj=store.root/ref['object']
            if obj.is_symlink():raise ValueError('unsafe evidence object')
            obj.unlink(missing_ok=True)
    # Retain locator metadata; old readers get a missing pack, never fabricated raw.
    immutable(store.root/'completed_expiries'/(aggregate_sha+'.json'),canonical({'receipt_sha256':digest(canonical(receipt))}))
    return {**receipt,'state':'AGGREGATED_AND_DETAIL_RETIRED','aggregate_compressed_bytes':destination.stat().st_size}


def resume_expiries(store,runs,check_closed=closed):
    """Complete durable expiry intents after interruption; never trust missing raw as success."""
    completed=[]
    for p in (store.root/'expiry_receipts').glob('*.json'):
        marker=store.root/'completed_expiries'/p.name
        if marker.exists():continue
        receipt=json.loads(p.read_text());aggregate=store.root/receipt['aggregate']
        if aggregate.is_symlink() or not aggregate.resolve().is_relative_to(store.root/'aggregates'):raise ValueError('unsafe aggregate receipt')
        encoded=gzip.decompress(aggregate.read_bytes())
        if digest(encoded)!=receipt['aggregate_sha256']:raise ValueError('aggregate receipt mismatch')
        ids=set(receipt['source_ids']);protected=set()
        for rev in (store.root/'revisions').glob('*/*.json'):
            v=store.revision(rev.stem)
            if v['source_id'] not in ids and not store.retirement(v['source_id']):protected.update(r['sha256'] for r in v['chunks'])
        for locator in (store.root/'objects').glob('*/*.locator.json'):
            ref=json.loads(locator.read_text())
            if ref.get('pack_sha256')==receipt['source_file_sha256'] and ref['object_sha256'] in protected:raise ValueError('pending expiry pack acquired protected reference')
        remaining=[]
        for name in receipt['source_paths']:
            source=Path(name)
            if not source.exists():continue
            pack=store.root/'packs'/receipt['source_file_sha256'][:2]/(receipt['source_file_sha256']+'.pack')
            if source!=pack and not eligible(source,runs,time.time(),3600):raise ValueError('pending expiry source no longer eligible')
            if source.is_symlink() or file_hash(source)!=receipt['source_file_sha256'] or not check_closed(source):raise ValueError('pending expiry source changed or open')
            remaining.append(source)
        for sid in ids:immutable(store.root/'retired_sources'/(sid+'.json'),canonical(receipt))
        for source in remaining:source.unlink();fsync_dir(source.parent)
        # Orphan gzip copies are harmless; only known exclusive objects are reclaimed.
        for sha in receipt['revisions']:
            for ref in store.revision(sha)['chunks']:
                if ref['sha256'] not in protected:
                    obj=store.root/ref['object']
                    if obj.is_symlink():raise ValueError('unsafe pending expiry object')
                    obj.unlink(missing_ok=True)
        immutable(marker,canonical({'receipt_sha256':digest(canonical(receipt))}));completed.append(p.name)
    return completed


def run(runs, *, target_bytes=RETENTION_TARGET_BYTES, trigger_bytes=RETENTION_TRIGGER_BYTES, maximum_seconds=300, minimum_age_seconds=3600, dry_run=False):
    if not 0<target_bytes<trigger_bytes<MAX_MANAGED_DATA_BYTES or minimum_age_seconds<3600 or not 0<maximum_seconds<=600:raise ValueError('unsafe aggregate retention bounds')
    runs=Path(runs).resolve();root=runs/'paper_v7_durable/permanent_evidence/store'
    if runs.name!='runs':raise ValueError('explicit managed runs directory required')
    started=time.monotonic();usage=allocated_data_bytes([runs]);result={'policy':POLICY,**AUTH,'before_bytes':usage,'maximum_total_data_bytes':MAX_MANAGED_DATA_BYTES,'hard_quota_enforced':False,'retired':[],'deferred':[]}
    if usage>=trigger_bytes:
        paths=[]
        for parent in [runs/'paper_v7_live',runs/'paper_v7_archives']:
            for folder,dirs,files in os.walk(parent,followlinks=False):
                dirs[:]=[d for d in dirs if not (Path(folder)/d).is_symlink()]
                for name in files:
                    p=Path(folder)/name
                    if eligible(p,runs,time.time(),minimum_age_seconds):paths.append(p)
        paths.sort(key=lambda p:(p.stat().st_mtime,str(p)))
        with EvidenceStore(root) as store, (root/'.writer.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if not dry_run:result['resumed']=resume_expiries(store,runs)
            for path in paths:
                if not path.exists():continue
                if time.monotonic()-started>=maximum_seconds or allocated_data_bytes([runs])<=target_bytes:break
                if dry_run:result['deferred'].append({'path':str(path),'reason':'DRY_RUN_NO_MUTATION'});continue
                try:result['retired'].append(retire(path,runs,store,minimum_age_seconds=minimum_age_seconds))
                except (OSError,ValueError,EOFError) as exc:result['deferred'].append({'path':str(path),'reason':str(exc)})
    result.update(timestamp_ms=time.time_ns()//1_000_000,after_bytes=allocated_data_bytes([runs]))
    result['state']='CAP_EXCEEDED' if result['after_bytes']>MAX_MANAGED_DATA_BYTES else 'ABOVE_TARGET_MORE_RETENTION_NEEDED' if result['after_bytes']>target_bytes else 'WITHIN_TARGET'
    if not dry_run:atomic(runs/'paper_v7_durable/permanent_evidence/aggregate_retention_status.json',result)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--runs-root',type=Path,required=True);ap.add_argument('--dry-run',action='store_true');ap.add_argument('--maximum-seconds',type=float,default=300);ap.add_argument('--minimum-age-seconds',type=int,default=3600)
    a=ap.parse_args();print(json.dumps(run(a.runs_root,maximum_seconds=a.maximum_seconds,minimum_age_seconds=a.minimum_age_seconds,dry_run=a.dry_run),sort_keys=True))

if __name__=='__main__':main()
