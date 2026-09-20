#!/usr/bin/env python3
"""Cold-path storage/collector measurement. No trading or credential access."""
from __future__ import annotations
from collections import defaultdict
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time


def category(relative):
    if 'external_fair/' in relative and '/raw/' in relative:
        return 'cex_raw'
    if 'external_fair/' in relative and '/normalized_events/' in relative:
        return 'cex_normalized'
    if 'native_observations/' in relative:
        return 'native_research'
    if any(s in relative for s in ('crypto_book/', 'book_observations/', 'compact_pm')):
        return 'pm_book'
    if 'hft_permanent/' in relative:
        return 'permanent'
    return 'other'


def snapshot(root):
    files = {}; totals = defaultdict(int); compressed = raw = allocated = 0
    seen = set()
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        identity = f'{info.st_dev}:{info.st_ino}'
        if identity in seen:
            continue
        seen.add(identity)
        rel = str(path.relative_to(root)); kind = category(rel)
        # A rename during compression is not new collection. Compressed bytes
        # are accounted in usage, never added to event-tape ingress rate.
        zipped = path.name.endswith(('.gz', '.zst', '.pack'))
        files[identity] = [info.st_size, kind, zipped, info.st_mtime_ns]
        totals[kind] += info.st_size
        allocated += info.st_blocks * 512
        if zipped: compressed += info.st_size
        else: raw += info.st_size
    return dict(at_ns=time.time_ns(), files=files, category_bytes=dict(totals),
                total_bytes=sum(totals.values()), allocated_bytes=allocated,
                raw_bytes=raw, compressed_bytes=compressed,
                filesystem_free_bytes=shutil.disk_usage(root).free,
                filesystem_total_bytes=shutil.disk_usage(root).total)


def compare(before, after, *, ceiling=60_000_000_000, target=50_000_000_000):
    elapsed = (after['at_ns']-before['at_ns'])/1e9
    if elapsed <= 0:
        raise ValueError('NONPOSITIVE_MEASUREMENT_INTERVAL')
    growth = defaultdict(int)
    for key, (size, kind, zipped, modified) in after['files'].items():
        if zipped:
            continue
        previous = before['files'].get(key)
        if previous:
            growth[kind] += max(0, size-previous[0])
        elif modified >= before['at_ns']:
            growth[kind] += size
    rates = {k: growth[k]/elapsed*3600/1e9 for k in
             ('cex_raw', 'cex_normalized', 'pm_book', 'native_research', 'permanent', 'other')}
    total = sum(rates.values())
    return dict(measurement_seconds=elapsed, gb_per_hour=rates, total_gb_per_hour=total,
                rate_semantics='OBSERVED_UNCOMPRESSED_FILE_GROWTH; deleted-between-samples segments may undercount',
                safe_target_bytes=target, maximum_managed_bytes=ceiling,
                estimated_uncompressed_raw_hours=(max(0,target-after['category_bytes'].get('permanent',0))/1e9/total if total else None),
                hours_until_ceiling_without_compression=(max(0,ceiling-after['total_bytes'])/1e9/total if total else None))


def collectors(root):
    result = []
    for path in sorted((root/'external_fair').rglob('external_venues.json')):
        if path.is_symlink() or path.stat().st_size > 4*1024**2:
            continue
        try: value = json.loads(path.read_bytes())
        except (ValueError, OSError): continue
        if not any(k in value for k in ('raw_frame_tapes','normalized_event_tapes','venues')):
            continue
        selected = {k:v for k,v in value.items() if k in {
            'asset','timestamp','timestamp_ms','updated_at_ms','raw_frame_tapes',
            'normalized_event_tapes','venues','disk_pressure','l2','ingress','state','valid',
            'binance_spot_l2','coinbase_spot_l2','bybit_spot_l2'}}
        result.append(dict(path=str(path.relative_to(root)), status_age_seconds=max(0,time.time()-path.stat().st_mtime), **selected))
    return result


def measure(root, seconds=30):
    root=Path(root).resolve(); first=snapshot(root); time.sleep(seconds); last=snapshot(root)
    compression_raw=compression_gzip=0; failures=0
    manifest=root/'lossless_compression_manifest.jsonl'
    if manifest.exists():
        with manifest.open() as stream:
            for line in stream:
                try: row=json.loads(line)
                except ValueError: failures+=1; continue
                if row.get('decompressed_sha256_verified') is True:
                    compression_raw+=int(row.get('source_bytes',0)); compression_gzip+=int(row.get('gzip_bytes',0))
    largest=[]
    for p in root.rglob('*'):
        if p.is_file() and not p.is_symlink():
            try: largest.append((p.stat().st_size,str(p.relative_to(root))))
            except FileNotFoundError: pass
    largest=sorted(largest,reverse=True)[:20]
    pm={}
    for relative in ('research/repricing_book/fillability_ws_status.json','universe/book_selection.json'):
        path=root/relative
        if path.exists():
            try:
                value=json.loads(path.read_bytes())
                if 'markets' in value:
                    value={'contexts':sorted({str(r.get('asset'))+':'+str(r.get('horizon')) for r in value['markets']}),
                           'markets':len(value['markets'])}
                pm[relative]=value
            except (ValueError,OSError): pass
    try: private_ips=subprocess.check_output(['tailscale','ip','-4'],text=True,timeout=5).strip()
    except (OSError,subprocess.SubprocessError): private_ips=None
    return dict(private_ips=private_ips,pm_observers=pm,largest_files=largest,schema='v7_hft_data_health_v1', paper_only=True, authenticated_execution=False,
                real_order_submission=False, root=str(root),
                **{k:v for k,v in last.items() if k!='files'}, **compare(first,last),
                verified_compression_ratio=(compression_raw/compression_gzip if compression_gzip else None),
                compression_manifest_errors=failures, collectors=collectors(root))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=30);a=p.parse_args()
    if not 1<=a.seconds<=60: p.error('measurement must be between 1 and 60 seconds')
    print(json.dumps(measure(a.root,a.seconds),sort_keys=True))
