"""Bounded hot JSONL with verified, immutable gzip segments and legacy reads.

One producer owns the append path. A single compression worker handles closed
segments; it never changes an open producer file or removes unverified bytes.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import gzip
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import time


def sync_directory(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def compress_closed(source):
    source=Path(source);before=source.stat()
    target=source.with_name(source.name+'.gz');temporary=target.with_name(target.name+f'.tmp.{os.getpid()}.{time.time_ns()}')
    digest=hashlib.sha256();size=0
    try:
        with source.open('rb') as stream,temporary.open('xb') as raw:
            with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=6) as output:
                for block in iter(lambda:stream.read(1024*1024),b''):
                    digest.update(block);size+=len(block);output.write(block)
            raw.flush();os.fsync(raw.fileno())
        verified=hashlib.sha256();decoded=0
        with gzip.open(temporary,'rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):
                verified.update(block);decoded+=len(block)
        after=source.stat()
        if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=
                (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                or size!=decoded or verified.digest()!=digest.digest()):
            raise ValueError('closed journal changed or compression failed verification')
        os.chmod(temporary,0o444)
        # A crash after linking is resumable only if the existing file is exact.
        try:os.link(temporary,target)
        except FileExistsError:
            existing=hashlib.sha256()
            with gzip.open(target,'rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):existing.update(block)
            if existing.digest()!=digest.digest():raise ValueError('journal archive collision')
        sync_directory(source.parent)
        receipt={'schema':'polymarket_v7_closed_journal_compression_v1',
                 'source_name':source.name,'source_bytes':size,'source_sha256':digest.hexdigest(),
                 'gzip_name':target.name,'gzip_bytes':target.stat().st_size,
                 'decoded_sha256_verified':True,'timestamp_ns':time.time_ns()}
        proof=target.with_name(target.name+'.manifest.json')
        if not proof.exists():
            proof_temp=temporary.with_name(temporary.name+'.manifest')
            try:
                with proof_temp.open('x') as out:
                    json.dump(receipt,out,sort_keys=True);out.write('\n');out.flush();os.fsync(out.fileno())
                os.link(proof_temp,proof)
            finally:proof_temp.unlink(missing_ok=True)
        else:
            prior=json.loads(proof.read_text())
            if any(prior.get(k)!=receipt[k] for k in ('source_name','source_bytes','source_sha256','gzip_name','decoded_sha256_verified')):
                raise ValueError('journal compression proof collision')
        sync_directory(source.parent)
        for stale in source.parent.glob(source.name+'.gz.tmp.*'):
            match=re.fullmatch(re.escape(source.name)+r'\.gz\.tmp\.(\d+)\.\d+(?:\.manifest)?',stale.name)
            if not match:continue
            try:os.kill(int(match[1]),0)
            except ProcessLookupError:stale.unlink(missing_ok=True)
            except (OSError,OverflowError):pass
        source.unlink();sync_directory(source.parent)
        return receipt
    finally:temporary.unlink(missing_ok=True)


def journal_paths(path):
    """Ordered immutable segments followed by the bounded active tail."""
    path=Path(path);segments={}
    for p in path.parent.glob(path.name+'.segment-*.jsonl*'):
        if p.name.endswith('.jsonl') or p.name.endswith('.jsonl.gz'):
            key=p.name.removesuffix('.gz')
            if key not in segments or p.suffix=='.gz':segments[key]=p
    return [segments[k] for k in sorted(segments)]+([path] if path.exists() else [])


def journal_rows(path,manifest=None):
    path=Path(path)
    if not path.parent.exists():return
    lock_path=path.with_name(path.name+'.rotation.lock')
    while True:
        active=None
        try:lock=lock_path.open('rb')
        except FileNotFoundError:lock=None
        with lock if lock is not None else nullcontext():
            if lock is not None:fcntl.flock(lock,fcntl.LOCK_SH)
            sources=journal_paths(path)
            if path in sources:
                try:active=path.open('rb')
                except FileNotFoundError:
                    if lock is None and lock_path.exists():continue
                    raise
                active_limit=os.fstat(active.fileno()).st_size
        # A producer may have started its first rotation during legacy discovery.
        if lock is None and lock_path.exists():
            if active is not None:active.close()
            continue
        break
    try:
        yield from _snapshot_rows(sources,path,active,active_limit if active else None,manifest)
    finally:
        if active is not None:active.close()


def _snapshot_rows(sources,path,active,active_limit,manifest):
    for source in sources:
        if source==path and active is not None:
            stream=active
        else:
            try:stream=source.open('rb')
            except FileNotFoundError:
                # Compression published its verified replacement after enumeration.
                source=source.with_name(source.name+'.gz');stream=source.open('rb')
        digest=hashlib.sha256();size=0
        with stream:
            limit=active_limit if stream is active else os.fstat(stream.fileno()).st_size
            decoded=gzip.GzipFile(fileobj=stream) if source.suffix=='.gz' else stream
            try:
                while source.suffix=='.gz' or stream.tell()<limit:
                    raw=decoded.readline() if source.suffix=='.gz' else decoded.readline(limit-stream.tell())
                    if not raw or not raw.endswith(b'\n'):break
                    digest.update(raw);size+=len(raw)
                    if raw.strip():yield json.loads(raw)
            finally:
                if decoded is not stream:decoded.close()
        if manifest is not None:manifest.append({'path':str(source),'decoded_prefix_bytes':size,'decoded_sha256':digest.hexdigest()})


class CompressedJournal:
    def __init__(self,path,maximum_hot_bytes=64*1024**2):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock_path=self.path.with_name(self.path.name+'.rotation.lock')
        self.lock_path.touch(exist_ok=True)
        self.maximum_hot_bytes=int(maximum_hot_bytes)
        if self.maximum_hot_bytes<1:raise ValueError('invalid journal segment size')
        self.writer_lock=self.path.with_name(self.path.name+'.writer.lock').open('a')
        try:fcntl.flock(self.writer_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException:self.writer_lock.close();raise
        self.worker=ThreadPoolExecutor(max_workers=1,thread_name_prefix='closed-evidence-compression')
        self.pending=None
        # Old closed tails survive a process crash. Compress before queuing more.
        self.recovery=sorted(self.path.parent.glob(self.path.name+'.segment-*.jsonl'))
        self.maintain()

    def maintain(self):
        if self.pending is not None:
            if not self.pending.done():return
            self.pending.result();self.pending=None
        if self.recovery:
            self.pending=self.worker.submit(compress_closed,self.recovery.pop(0));return
        if self.path.exists() and self.path.stat().st_size>=self.maximum_hot_bytes:
            if self.path.is_symlink() or self.path.stat().st_nlink!=1:
                raise ValueError('journal rotation requires an exclusively owned source inode')
            closed=self.path.with_name(self.path.name+f'.segment-{time.time_ns():020d}.jsonl')
            with self.lock_path.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                os.rename(self.path,closed);sync_directory(closed.parent)
            self.pending=self.worker.submit(compress_closed,closed)

    def append(self,row):
        self.maintain()
        raw=(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
        size=self.path.stat().st_size if self.path.exists() else 0
        if size+len(raw)>2*self.maximum_hot_bytes:
            raise RuntimeError('compression backlog exceeds bounded hot journal; preserved files require recovery')
        with self.path.open('ab') as out:out.write(raw);out.flush();os.fsync(out.fileno())
        self.maintain()

    def close(self):
        try:
            self.worker.shutdown(wait=True)
            if self.pending is not None:self.pending.result()
        finally:self.writer_lock.close()

    def __enter__(self):return self
    def __exit__(self,*unused):self.close()
