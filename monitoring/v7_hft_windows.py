#!/usr/bin/env python3
"""Cold, incremental opportunity preservation before rolling tape retirement.

Compact native records retain their existing schema. Replay windows retain exact
source records once per source (union of overlapping requests), with SHA proofs.
Unknown/truncated formats stay pinned. This module never submits orders or fits.
"""
from __future__ import annotations
import bisect
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import time
import tempfile

HEADER = struct.Struct('<8sIIq41s65s65s33s4x')
RAW = struct.Struct('<QQqqB3xI')
RECORD = struct.Struct('<QqQHHI')
EVENT = struct.Struct('<QQQBBH4xqqq10dbBBBB3x')
SECOND = 1_000_000_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def decoded_sha(path):
    checksum=hashlib.sha256()
    with gzip.open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1<<20),b''):checksum.update(block)
    return checksum.hexdigest()


def wall(row):
    mono = row.get('observed_monotonic_ns') or row.get('decision_monotonic_ns')
    if mono and row.get('close_wall_ns') and row.get('close_monotonic_ns'):
        return mono + row['close_wall_ns'] - row['close_monotonic_ns']
    return row.get('decision_wall_ns') or 0


def merged(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else: result.append([start, end])
    return result


def contains(intervals, stamp):
    i = bisect.bisect_right(intervals, [stamp, float('inf')])-1
    return i >= 0 and intervals[i][0] <= stamp <= intervals[i][1]


def publish(root, kind, payload, suffix='.jsonl.gz'):
    folder = root/kind; folder.mkdir(parents=True, exist_ok=True)
    checksum=hashlib.sha256()
    fd, name=tempfile.mkstemp(prefix='.writing-',dir=folder)
    temp=Path(name)
    try:
        with os.fdopen(fd,'wb') as target:
            with gzip.GzipFile(fileobj=target,mode='wb',compresslevel=6,mtime=0,filename='') as packed:
                if isinstance(payload,bytes):
                    checksum.update(payload);packed.write(payload)
                else:
                    payload.seek(0)
                    for block in iter(lambda:payload.read(1<<20),b''):
                        checksum.update(block);packed.write(block)
            target.flush();os.fsync(target.fileno())
        digest=checksum.hexdigest(); path=folder/(digest+suffix)
        verified=hashlib.sha256()
        with gzip.open(temp,'rb') as stream:
            for block in iter(lambda:stream.read(1<<20),b''):verified.update(block)
        if verified.hexdigest()!=digest:raise ValueError('WINDOW_ROUNDTRIP_FAILED')
        if path.exists():
            verified=hashlib.sha256()
            with gzip.open(path,'rb') as stream:
                for block in iter(lambda:stream.read(1<<20),b''):verified.update(block)
            if verified.hexdigest()!=digest:raise ValueError('IMMUTABLE_WINDOW_CORRUPTED')
        else:
            os.link(temp,path)
            directory=os.open(folder,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        return str(path.relative_to(root)),digest
    finally:temp.unlink(missing_ok=True)


class Windows:
    def __init__(self, run_root, minimum_wall_ns=0):
        self.run_root=Path(run_root).resolve(); self.root=self.run_root/'research/hft_permanent'
        self.root.mkdir(parents=True,exist_ok=True)
        self.minimum_wall_ns=minimum_wall_ns
        self.db=sqlite3.connect(self.root/'index.sqlite',timeout=1)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, offset INTEGER, capture TEXT, last_ns INTEGER);
          CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, capture TEXT, asset TEXT, horizon TEXT,
            market TEXT, decision_ns INTEGER, start_ns INTEGER, end_ns INTEGER, reason INTEGER, accepted INTEGER);
          CREATE INDEX IF NOT EXISTS windows_asset ON requests(asset,start_ns,end_ns);
          CREATE INDEX IF NOT EXISTS windows_capture ON requests(capture);
          CREATE TABLE IF NOT EXISTS watermarks(asset TEXT,horizon TEXT,stamp INTEGER,PRIMARY KEY(asset,horizon));
          CREATE TABLE IF NOT EXISTS preserved(source TEXT PRIMARY KEY,source_sha TEXT, proof TEXT);
        ''')

    def close(self): self.db.close()

    def source_key(self,path):
        return str(path.relative_to(self.run_root)) if path.is_relative_to(self.run_root) else str(path)

    def ingest(self, *, max_rows=10000, archive_max_rows=50000, budget_seconds=45, realtime_lag_seconds=30):
        """Freeze complete append-only prefixes, including active D1 captures.

        Offsets commit only after immutable compact chunks exist. Repeated reads
        after a crash deduplicate by payload hash and opportunity identity.
        """
        if realtime_lag_seconds < 1:
            raise ValueError('REALTIME_LAG_MUST_BE_POSITIVE')
        if archive_max_rows < max_rows:
            raise ValueError('ARCHIVE_ROW_LIMIT_BELOW_ACTIVE_LIMIT')
        counts={'records':0,'decisions':0,'pending_markets':set()}; failures=[]; backlog_sources=[]
        progress={}; incomplete=set(); started=time.monotonic(); scan_complete=True
        # The input is append-only and active captures will normally gain a few
        # records while a cold pass is running. A fresh tail is not a historical
        # gap: the daily cutoff will be far behind it. An older unread record is.
        complete_before_ns=time.time_ns()-int(realtime_lag_seconds*SECOND)
        roots=[self.run_root/'research/native_observations']
        archives=self.run_root.parent/'paper_v7_london_archives'
        if archives.is_dir() and not archives.is_symlink():
            roots.extend(p/'research/native_observations' for p in archives.glob('cutover-*') if p.is_dir() and not p.is_symlink())
        for path in sorted(p for folder in roots for p in folder.rglob('*.jsonl*')):
            if time.monotonic()-started>=budget_seconds:
                scan_complete=False;break
            if path.is_symlink() or path.name.endswith('.closed.json') or not path.name.endswith(('.jsonl','.jsonl.gz')):
                continue
            relative=self.source_key(path).removesuffix('.gz')
            old=self.db.execute('SELECT offset FROM sources WHERE path=?',(relative,)).fetchone()
            if old:
                if path.suffix!='.gz' and path.stat().st_size==old[0]: continue
                closure=Path(str(path).removesuffix('.gz')+'.closed.json')
                if closure.exists() and json.loads(closure.read_bytes()).get('bytes')==old[0]: continue
            offset=old[0] if old else 0; requests=[]; last=0; capture=''; contexts={}
            archived_source=path.is_relative_to(archives)
            row_limit=archive_max_rows if archived_source else max_rows
            # Native compact chunks can be dense during an archive catch-up.
            # Spill after 8 MiB so preserving old opportunity population never
            # turns a cold worker into an unbounded-memory process.
            batch=tempfile.SpooledTemporaryFile(max_size=8<<20)
            opener=gzip.open if path.suffix=='.gz' else open
            try:
                with opener(path,'rb') as stream:
                    stream.seek(offset)
                    exhausted_rows = False
                    for _ in range(row_limit):
                        if time.monotonic()-started >= budget_seconds:
                            scan_complete=False
                            incomplete.update(contexts)
                            break
                        line=stream.readline()
                        if not line or not line.endswith(b'\n'): break
                        row=json.loads(line)
                        if row.get('schema')!='polymarket_v7_native_observation_v1':
                            raise ValueError('UNKNOWN_NATIVE_SCHEMA')
                        if row.get('paper_only') is not True or row.get('execution_authority') is not False:
                            raise ValueError('INVALID_PAPER_AUTHORITY')
                        offset=stream.tell(); counts['records']+=1
                        stamp=wall(row);last=max(last,stamp);capture=row['capture_id']
                        if stamp: contexts[(row['asset'],row['horizon'])]=max(stamp,contexts.get((row['asset'],row['horizon']),0))
                        kind=row.get('kind')
                        if stamp < self.minimum_wall_ns: continue
                        if kind in (2,4,5,6): batch.write(line)
                        if kind==2:
                            decision=row.get('decision_wall_ns') or stamp
                            if not decision: raise ValueError('MISSING_OPPORTUNITY_CLOCK')
                            identity=[row['server_id'],row['run_id'],capture,row.get('signal_version'),
                                      row.get('trigger_monotonic_ns'),row.get('reason'),row.get('accepted'),
                                      # Stable no-signal controls sampled at 30s by the runtime.
                                      decision//(30*SECOND) if not row.get('signal_valid') else None]
                            requests.append((sha(canonical(identity)),capture,row['asset'],row['horizon'],str(row['market_id']),
                                             decision,decision-2*SECOND,decision+5*SECOND,int(row.get('reason') or 0),int(bool(row.get('accepted')))))
                            counts['decisions']+=1;counts['pending_markets'].add(str(row['market_id']))
                    else:
                        exhausted_rows = True
                    # Do not advance the committed offset over the peek: an
                    # interruption must replay that complete record. It is only
                    # used to distinguish a genuine historical backlog from a
                    # normal live tail.
                    if exhausted_rows:
                        next_line=stream.readline()
                        if next_line and next_line.endswith(b'\n'):
                            next_row=json.loads(next_line)
                            next_stamp=wall(next_row)
                            next_context=(next_row.get('asset'),next_row.get('horizon'))
                            if (not next_stamp or next_stamp <= complete_before_ns):
                                scan_complete=False
                                if all(isinstance(v,str) and v for v in next_context):
                                    incomplete.add(next_context)
                                    backlog_sources.append(dict(source=relative,asset=next_context[0],
                                        horizon=next_context[1],next_observation_ns=next_stamp,
                                        lag_seconds=max(0,(time.time_ns()-next_stamp)/1e9) if next_stamp else None))
                                else:
                                    incomplete.update(contexts)
                                    backlog_sources.append(dict(source=relative,asset=None,horizon=None,
                                        next_observation_ns=next_stamp,
                                        lag_seconds=max(0,(time.time_ns()-next_stamp)/1e9) if next_stamp else None))
                if batch.tell():
                    batch.seek(0)
                    publish(self.root,'compact',batch)
                if offset!=(old[0] if old else 0):
                    with self.db:
                        self.db.executemany('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)',requests)
                        self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?)',(relative,offset,capture,last))
                        for context,stamp in contexts.items(): progress[context]=max(stamp,progress.get(context,0))
            except (ValueError,OSError,KeyError) as exc:
                failures.append({'source':relative,'reason':str(exc)})
                incomplete.update(contexts)
            finally:
                batch.close()
        # A newer capture cannot conceal an unread prefix in an older capture.
        if not failures and scan_complete:
            # Include already-indexed unchanged sources after a bounded catch-up
            # pass; absence of new bytes does not erase their proven frontier.
            for asset,horizon,stamp in self.db.execute('''SELECT r.asset,r.horizon,MAX(s.last_ns)
                FROM sources s JOIN (SELECT DISTINCT capture,asset,horizon FROM requests) r
                ON s.capture=r.capture GROUP BY r.asset,r.horizon'''):
                progress[(asset,horizon)]=max(stamp,progress.get((asset,horizon),0))
            with self.db:
                for (asset,horizon),stamp in progress.items():
                    if (asset,horizon) not in incomplete:
                        self.db.execute('INSERT INTO watermarks VALUES (?,?,?) ON CONFLICT(asset,horizon) DO UPDATE SET stamp=MAX(stamp,excluded.stamp)',(asset,horizon,stamp))
        counts['pending_markets']=sorted(counts['pending_markets']);counts['failures']=failures
        counts['scan_complete']=scan_complete and not incomplete
        counts['total_opportunities']=self.db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
        counts['total_markets']=self.db.execute('SELECT COUNT(DISTINCT market) FROM requests').fetchone()[0]
        population={'schema':'v7_hft_population_v1','paper_only':True,
                    'markets':[r[0] for r in self.db.execute('SELECT DISTINCT market FROM requests ORDER BY market')],
                    'unique_opportunities':counts['total_opportunities'], 'updated_ns':time.time_ns(),
                    'scan_complete':counts['scan_complete'],
                    'scan_complete_semantics':'ALL_INDEXED_SOURCES_CAUGHT_UP_TO_BOUNDED_REALTIME_LAG',
                    'complete_before_ns':complete_before_ns,
                    'realtime_lag_seconds':realtime_lag_seconds,
                    'capture_failures':failures,'backlog_contexts':sorted(':'.join(c) for c in incomplete),
                    'backlog_sources':sorted(backlog_sources,key=lambda r:(-(r['lag_seconds'] or 0),r['source'])),
                    'watermarks':{a+':'+h:t for a,h,t in self.db.execute('SELECT * FROM watermarks')}}
        compact=self.root/'compact';compact.mkdir(exist_ok=True)
        temp=compact/'population.tmp';temp.write_bytes(canonical(population));temp.replace(compact/'population.json')
        return counts

    def preserve(self,path):
        """Return verified proof or raise; caller must retain the source on error."""
        path=Path(path); relative=self.source_key(path)
        prior=self.db.execute('SELECT source_sha,proof FROM preserved WHERE source=?',(relative,)).fetchone()
        source_sha=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1<<20),b''): source_sha.update(chunk)
        source_sha=source_sha.hexdigest()
        if prior:
            if prior[0]!=source_sha: raise ValueError('PRESERVED_SOURCE_CHANGED')
            proof=json.loads(prior[1])
            for key in ('window','compact'):
                if proof.get(key):
                    target=self.root/proof[key]
                    if target.is_symlink() or decoded_sha(target)!=proof[key+'_sha256']:
                        raise ValueError('PRESERVED_WINDOW_MISSING_OR_CORRUPTED')
            return proof
        if 'native_observations/' in relative:
            proof=self._native(path,relative)
        elif '/raw/' in relative or '/normalized_events/' in relative:
            proof=self._external(path,relative)
        elif '/book_observations/' in relative:
            proof=self._books(path)
        else: raise ValueError('NO_HFT_PRESERVATION_ADAPTER')
        proof.update(schema='v7_hft_window_preservation_v1',source=relative,source_sha256=source_sha,
                     replayable=True,paper_only=True,authenticated_execution=False,real_order_submission=False)
        with self.db: self.db.execute('INSERT INTO preserved VALUES (?,?,?)',(relative,source_sha,canonical(proof).decode()))
        publish(self.root,'proofs',canonical(proof)+b'\n')
        return proof

    def _books(self,path):
        """Reuse the existing public PM observer, preserving exact event records."""
        marks=list(self.db.execute('SELECT stamp FROM watermarks'))
        if len(marks)!=30: raise ValueError('ALL_CONTEXT_POPULATION_WATERMARKS_REQUIRED')
        watermark=min(r[0] for r in marks)-2*SECOND
        by_market={}
        for market,start,end in self.db.execute('SELECT market,start_ns,end_ns FROM requests ORDER BY market,start_ns'):
            by_market.setdefault(market,[]).append((start,end))
        by_market={m:merged(rows) for m,rows in by_market.items()}
        output=tempfile.SpooledTemporaryFile(max_size=8<<20); first=last=0; count=selected=0
        opener=gzip.open if path.suffix=='.gz' else open
        with opener(path,'rb') as stream:
            for line in stream:
                row=json.loads(line)
                if row.get('schema')!='polymarket_v7_causal_book_observation_v1':
                    raise ValueError('UNKNOWN_PM_BOOK_SCHEMA')
                stamp=int(row.get('receive_wall_ms') or 0)*1_000_000
                if not stamp or not row.get('receive_monotonic_ns'): raise ValueError('MISSING_PM_CLOCK')
                count+=1;first=min(first or stamp,stamp);last=max(last,stamp)
                if contains(by_market.get(str(row['market_id']),[]),stamp):
                    output.write(line);selected+=1
        if last>=watermark: raise ValueError('FUTURE_OPPORTUNITY_POPULATION_NOT_YET_OBSERVED')
        window,wsha=publish(self.root,'windows',output);output.close()
        return dict(window=window,window_sha256=wsha,source_records=count,window_records=selected,
                    first_ns=first,last_ns=last,population_watermark_ns=watermark,
                    coverage='EXACT_PM_OBSERVER_EVENTS; capture gaps remain unavailable')

    def _native(self,path,relative):
        original=relative.removesuffix('.gz')
        closure=self.run_root/(original+'.closed.json')
        if not closure.exists(): raise ValueError('ACTIVE_NATIVE_SOURCE_PINNED')
        closed=json.loads(closure.read_bytes())
        if closed.get('closed') is not True or closed.get('healthy') is not True:
            raise ValueError('UNHEALTHY_NATIVE_CAPTURE_PINNED')
        source=self.db.execute('SELECT offset,capture FROM sources WHERE path=?',(original,)).fetchone()
        if not source: raise ValueError('NATIVE_POPULATION_NOT_INDEXED')
        intervals=merged(self.db.execute('SELECT start_ns,end_ns FROM requests WHERE capture=?',(source[1],)))
        output=tempfile.SpooledTemporaryFile(max_size=8<<20); compact=tempfile.SpooledTemporaryFile(max_size=8<<20)
        total=0; first=last=None; counts=selected=0
        opener=gzip.open if path.suffix=='.gz' else open
        with opener(path,'rb') as stream:
            for line in stream:
                total+=len(line);row=json.loads(line);stamp=wall(row);counts+=1
                if stamp: first=stamp if first is None else min(first,stamp);last=max(last or 0,stamp)
                if row.get('kind') in (2,4,5,6) and stamp>=self.minimum_wall_ns: compact.write(line)
                if contains(intervals,stamp): output.write(line); selected+=1
        if total!=source[0]: raise ValueError('NATIVE_POPULATION_PREFIX_INCOMPLETE')
        window,wsha=publish(self.root,'windows',output)
        compact_path,csha=publish(self.root,'compact_closed',compact)
        output.close();compact.close()
        return dict(window=window,window_sha256=wsha,compact=compact_path,compact_sha256=csha,
                    source_records=counts,window_records=selected,intervals=intervals,
                    first_ns=first,last_ns=last,
                    coverage='ACTUAL_CAPTURE_ONLY; legacy DECISION_WINDOWS have no guaranteed pre-signal PM path')

    def _external(self,path,relative):
        parts=Path(relative).parts; asset=parts[parts.index('assets')+1].upper() if 'assets' in parts else 'BTC'
        marks=list(self.db.execute('SELECT horizon,stamp FROM watermarks WHERE asset=?',(asset,)))
        if {h for h,_ in marks}!={'M5','M15','H1','H4','D1'}:
            raise ValueError('ALL_CONTEXT_POPULATION_WATERMARKS_REQUIRED')
        watermark=min(t for _,t in marks)-2*SECOND
        intervals=merged(self.db.execute('SELECT start_ns,end_ns FROM requests WHERE asset=?',(asset,)))
        output=tempfile.SpooledTemporaryFile(max_size=8<<20);first=last=0;count=selected=0
        opener=gzip.open if path.suffix=='.gz' else open
        with opener(path,'rb') as stream:
            header=stream.read(HEADER.size)
            if len(header)!=HEADER.size: raise ValueError('PARTIAL_TAPE_HEADER')
            magic,version,width,*_=HEADER.unpack(header)
            if version!=3 or magic not in (b'PMV7RAW!',b'PMV7TAPE') or width!=(0 if magic==b'PMV7RAW!' else 544):
                raise ValueError('UNKNOWN_EXTERNAL_TAPE_ABI')
            output.write(header)
            while True:
                record=stream.read(RAW.size if magic==b'PMV7RAW!' else 544)
                if not record: break
                if len(record)!=(RAW.size if magic==b'PMV7RAW!' else 544): raise ValueError('PARTIAL_TAPE_RECORD')
                if magic==b'PMV7RAW!':
                    _,_,mono,stamp,_,size=RAW.unpack(record)
                    if size>2*1024**2: raise ValueError('OVERSIZED_RAW_FRAME')
                    body=stream.read(size)
                    if len(body)!=size: raise ValueError('PARTIAL_RAW_FRAME')
                    record+=body
                else:
                    _,mono,_,kind,_,size=RECORD.unpack(record[:32])
                    if kind!=2 or size!=EVENT.size: raise ValueError('UNKNOWN_NORMALIZED_EVENT')
                    event=EVENT.unpack(record[32:32+size]);stamp=event[8]
                    if event[7]!=mono: raise ValueError('INCONSISTENT_RECEIVE_CLOCK')
                if stamp<=0 or mono<=0: raise ValueError('MISSING_EXTERNAL_CLOCK')
                count+=1;first=min(first or stamp,stamp);last=max(last,stamp)
                if contains(intervals,stamp): output.write(record);selected+=1
        if last>=watermark: raise ValueError('FUTURE_OPPORTUNITY_POPULATION_NOT_YET_OBSERVED')
        window,wsha=publish(self.root,'windows',output,'.bin.gz')
        output.close()
        return dict(window=window,window_sha256=wsha,asset=asset,source_records=count,
                    window_records=selected,first_ns=first,last_ns=last,population_watermark_ns=watermark,
                    coverage='EXACT_EVENT_RECORDS; L2 state requires a preceding venue snapshot')
