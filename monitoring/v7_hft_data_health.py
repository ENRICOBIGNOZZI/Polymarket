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
import urllib.request


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


def compression_ratios(root):
    totals=defaultdict(lambda:[0,0]);errors=0
    manifest=root/'lossless_compression_manifest.jsonl'
    if manifest.exists():
        with manifest.open() as stream:
            for line in stream:
                try: row=json.loads(line)
                except ValueError: errors+=1;continue
                if row.get('decompressed_sha256_verified') is True:
                    values=totals[category(row.get('source',''))]
                    values[0]+=int(row.get('source_bytes',0));values[1]+=int(row.get('gzip_bytes',0))
    return {k:v[0]/v[1] for k,v in totals.items() if v[1]},errors


def storage_projection(root, before, after, target=50_000_000_000):
    result=compare(before,after,target=target)
    ratios,errors=compression_ratios(root)
    rate=sum(v/max(1,ratios.get(k,1)) for k,v in result['gb_per_hour'].items() if k!='permanent')
    # Other disk users count against actual headroom even outside this run root.
    available=min(target,after['total_bytes']+max(0,after['filesystem_free_bytes']-8*1024**3))
    available=max(0,available-after['category_bytes'].get('permanent',0))
    result.update(verified_compression_ratios=ratios,compression_manifest_errors=errors,
                  estimated_compressed_gb_per_hour=rate,safe_raw_budget_bytes=available,
                  estimated_compressed_raw_hours=(available/1e9/rate if rate else None))
    return result


def measure(root, seconds=30):
    root=Path(root).resolve(); first=snapshot(root); first_feeds=collectors(root)
    time.sleep(seconds); last=snapshot(root); last_feeds=collectors(root)
    previous={(r.get('asset'),v['venue']):v for r in first_feeds for v in r.get('venues',[])}
    quality=[]
    for row in last_feeds:
        for venue in row.get('venues',[]):
            if not venue.get('enabled'): continue
            old=previous.get((row.get('asset'),venue['venue']),{})
            delta=venue.get('frames_received',0)-old.get('frames_received',0)
            age=(time.time_ns()-venue.get('last_receive_wall_ns',0))/1e9 if venue.get('last_receive_wall_ns') else None
            quality.append(dict(asset=row.get('asset'),venue=venue['venue'],connected=venue.get('connected'),
                                healthy=venue.get('healthy'),events_in_sample=delta if delta>=0 else None,
                                counter_reset=delta<0,last_event_age_seconds=age,
                                stale=age is None or age>5,decode_failures=venue.get('decode_failures'),
                                transport_failures=venue.get('transport_failures')))
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
    public_ip=None;ssh_host_key=None
    try:
        request=urllib.request.Request('http://169.254.169.254/latest/api/token',method='PUT',
            headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})
        with urllib.request.urlopen(request,timeout=2) as response: token=response.read().decode()
        request=urllib.request.Request('http://169.254.169.254/latest/meta-data/public-ipv4',headers={'X-aws-ec2-metadata-token':token})
        with urllib.request.urlopen(request,timeout=2) as response: public_ip=response.read().decode()
        ssh_host_key=Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().strip()
    except (OSError,ValueError): pass
    siblings={}
    for folder in root.parent.iterdir():
        if folder.is_dir() and not folder.is_symlink():
            size=0
            for p in folder.rglob('*'):
                try:
                    if p.is_file() and not p.is_symlink():size+=p.stat().st_size
                except FileNotFoundError:pass
            siblings[folder.name]=size
    native=[]
    for path in sorted(root.glob('native_crypto_settlement_engine_*_M5.log')):
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-512*1024))
            for line in reversed(stream.read().splitlines()):
                try: row=json.loads(line)
                except (ValueError,UnicodeError):continue
                if isinstance(row,dict) and 'decision_compute' in row:
                    native.append({k:row.get(k) for k in ('code_sha','model_sha','asset','horizon','clean_capture',
                        'decision_compute','first_signal_to_decision','repricing_horizons_ms','repricing_origins',
                        'repricing_labels','repricing_censors','repricing_window_overflow',
                        'repricing_evidence_compute_ns','repricing_evidence_max_ns')});break
    preservation={}
    for relative in ('control/london_buffer_retention_status.json','research/hft_permanent/compact/population.json'):
        path=root/relative
        if path.exists():
            try:
                value=json.loads(path.read_bytes())
                if 'lossless_compression' in value:
                    value['lossless_compression']={k:v for k,v in value['lossless_compression'].items() if k not in ('archived','skipped')}
                    value['rolling_retirement']={k:v for k,v in value.get('rolling_retirement',{}).items() if k!='retired'}
                    for section in ('lossless_compression','rolling_retirement','hft_opportunity_preservation'):
                        item=value.get(section) or {}
                        if isinstance(item.get('failures'),list):
                            item['failure_count']=len(item['failures']);item['failures']=item['failures'][:10]
                if 'markets' in value: value['markets_count']=len(value.pop('markets'))
                if isinstance(value.get('capture_failures'),list):
                    value['capture_failure_count']=len(value['capture_failures']);value['capture_failures']=value['capture_failures'][:10]
                preservation[relative]=value
            except (ValueError,OSError): pass
    return dict(sibling_directory_bytes=siblings,public_ip=public_ip,ssh_host_public_key=ssh_host_key,
                preservation=preservation,native_latency=native,feed_quality=quality,
                private_ips=private_ips,pm_observers=pm,largest_files=largest,schema='v7_hft_data_health_v1', paper_only=True, authenticated_execution=False,
                real_order_submission=False, root=str(root),
                **{k:v for k,v in last.items() if k!='files'}, **storage_projection(root,first,last),
                verified_compression_ratio=(compression_raw/compression_gzip if compression_gzip else None),
                collectors=last_feeds)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,required=True)
    p.add_argument('--seconds',type=int,default=30);a=p.parse_args()
    if not 1<=a.seconds<=60: p.error('measurement must be between 1 and 60 seconds')
    print(json.dumps(measure(a.root,a.seconds),sort_keys=True))
