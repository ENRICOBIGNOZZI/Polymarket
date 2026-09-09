#!/usr/bin/env python3
"""Measured lossless archive throughput and conservative capacity alerts.

Backfill/copy growth is not recurring producer throughput. Measurements below
use repeated active-run closed-segment compression publications. Open streams,
other observers and replica overhead are explicitly outside the measured scope.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import time
from v7_evidence_store import AUTH,digest,canonical,immutable
from v7_storage_budget import MAX_MANAGED_DATA_BYTES, RETENTION_TRIGGER_BYTES, RETENTION_TARGET_BYTES, WARNING_DATA_BYTES, CRITICAL_DATA_BYTES


def allocated_data_bytes(roots):
    seen=set();total=0
    def fail(error):raise error
    for root in roots:
        if Path(root).is_symlink() or not Path(root).is_dir():raise ValueError('unsafe or missing data-budget root')
        for folder,dirs,files in os.walk(root,followlinks=False,onerror=fail):
            dirs[:]=[d for d in dirs if not (Path(folder)/d).is_symlink()]
            for p in [Path(folder)]+[Path(folder)/name for name in files]:
                try:s=p.lstat()
                except FileNotFoundError:continue
                key=(s.st_dev,s.st_ino)
                if p.is_symlink() or key in seen:continue
                seen.add(key);total+=s.st_blocks*512
    return total


def capacity(records,free_bytes,total_bytes,*,reserve_bytes=20*1024**3,data_bytes=None,maximum_data_bytes=MAX_MANAGED_DATA_BYTES,compaction_trigger_bytes=RETENTION_TRIGGER_BYTES):
    groups=defaultdict(dict)
    for row in records:
        if row.get('scope')!='ACTIVE_RUN_CLOSED_SEGMENT' or row.get('decompressed_sha256_verified') is not True:continue
        if not row.get('source_sha256'):continue
        groups[row['timestamp']][row['source_sha256']]=row
    stamps=sorted(groups)
    result={'schema':'polymarket_v7_permanent_evidence_capacity_v1',**AUTH,'timestamp_ms':time.time_ns()//1_000_000,
      'free_bytes':free_bytes,'total_bytes':total_bytes,'reserve_bytes':reserve_bytes,'rate_estimate':None,
      'data_allocated_bytes':data_bytes,'maximum_total_data_bytes':maximum_data_bytes,'external_storage_allowed':False,
      'warning_bytes':WARNING_DATA_BYTES,'retention_target_bytes':RETENTION_TARGET_BYTES,'retention_trigger_bytes':compaction_trigger_bytes,'critical_bytes':CRITICAL_DATA_BYTES,
      'budget_state':'UNKNOWN_USAGE' if data_bytes is None else 'CAP_EXCEEDED_COMPACTION_REQUIRED' if data_bytes>maximum_data_bytes else 'WITHIN_CAP',
      'state':'DATA_BUDGET_COMPACTION_REQUIRED' if data_bytes is not None and data_bytes>=compaction_trigger_bytes else 'INSUFFICIENT_REPEAT_COMPRESSION_MEASUREMENTS',
      'minimum_reduction_bytes_to_current_cap':max(0,data_bytes-maximum_data_bytes) if data_bytes is not None else None,
      'automatic_deletion':False}
    if len(stamps)<2:return result
    # First retained batch can contain initial backlog. Never use it as a
    # recurring-production sample without an earlier measurement boundary.
    last=stamps[-1];first=next((s for s in stamps if s>=last-86400),stamps[0])
    if first==last:first=stamps[-2]
    earlier={sha for stamp in stamps if stamp<=first for sha in groups[stamp]}
    selected={sha:r for stamp in stamps if first<stamp<=last for sha,r in groups[stamp].items() if sha not in earlier}
    duration=last-first;raw=sum(r['source_bytes'] for r in selected.values());compressed=sum(r['gzip_bytes'] for r in selected.values())
    raw_daily=raw*86400/duration;compressed_daily=compressed*86400/duration;usable=max(0,free_bytes-reserve_bytes)
    runway=usable/compressed_daily if compressed_daily else None
    budget_days=max(0,maximum_data_bytes-data_bytes)/compressed_daily if data_bytes is not None and compressed_daily else None
    result.update(state='DATA_BUDGET_COMPACTION_REQUIRED' if data_bytes is not None and data_bytes>=compaction_trigger_bytes else 'CAPACITY_WARNING' if runway is not None and runway<30 else 'OBSERVED_SCOPE_CAPACITY_AVAILABLE',
      rate_estimate={'start_s':first,'end_s':last,'measurement_seconds':duration,'verified_unique_segments':len(selected),
       'source_sha256s':sorted(selected),'raw_bytes':raw,'compressed_bytes':compressed,
       'raw_GB_per_day':raw_daily/1e9,'compressed_GB_per_day':compressed_daily/1e9,
       'compression_ratio':compressed/raw if raw else None,'scope':'CLOSED_ACTIVE_RAW_AND_BOOK_SEGMENTS_ONLY; OTHER_STREAMS_AND_REPLICA_OVERHEAD_NOT_INCLUDED',
       'stationarity_assumed_for_projection':True,'backfill_excluded':True},
      projected_GB={str(days):{'raw':raw_daily*days/1e9,'compressed_observed_scope':compressed_daily*days/1e9,
                             'compressed_2x_rate_stress':compressed_daily*days*2/1e9} for days in (7,30,90)},
      maximum_days_to_reserve_at_observed_scope_rate=runway,
      maximum_days_to_data_cap_at_observed_scope_rate=budget_days,
      minimum_reduction_bytes_to_current_cap=max(0,data_bytes-maximum_data_bytes) if data_bytes is not None else None,
      limitation='Runway is an upper bound if unmeasured streams or duplicate replicas consume additional space. Compression cadence and market regimes can vary.')
    return result


def main():
    from v7_permanent_evidence import atomic
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--compression-manifest',type=Path,action='append',required=True)
    ap.add_argument('--storage-root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--data-root',type=Path,action='append',help='All managed data roots; physical hardlinks counted once')
    ap.add_argument('--history-root',type=Path);a=ap.parse_args();records=[];sources=[]
    for path in a.compression_manifest:
        raw=path.read_bytes();raw=raw[:raw.rfind(b'\n')+1]
        sources.append({'path':str(path),'prefix_sha256':digest(raw),'prefix_bytes':len(raw)})
        records.extend(json.loads(line) for line in raw.splitlines() if line.strip())
    disk=shutil.disk_usage(a.storage_root);data_roots=a.data_root or [a.storage_root]
    result=capacity(records,disk.free,disk.total,data_bytes=allocated_data_bytes(data_roots));result['sources']=sources
    result['data_roots']=[str(p) for p in data_roots]
    if a.history_root:
        encoded=canonical(result);immutable(a.history_root/(str(result['timestamp_ms'])+'-'+digest(encoded)[:12]+'.json'),encoded)
    atomic(a.output,result)
    summary={k:result[k] for k in ['state','free_bytes','data_allocated_bytes','budget_state','projected_GB'] if k in result}
    summary['rate_estimate']={k:v for k,v in (result.get('rate_estimate') or {}).items() if k!='source_sha256s'}
    print(json.dumps(summary))

if __name__=='__main__':main()
