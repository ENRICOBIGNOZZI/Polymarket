#!/usr/bin/env python3
"""Permanent byte-exact evidence beneath replaceable V7 models and reports.

Source revisions and compressed objects are immutable. SQLite is only a rebuildable
cursor/index. Capture never renames, truncates or deletes the producer's source.
An append revision references its predecessor; overwrites/restarts start a new
revision chain and leave all previous chains recoverable. No trading authority.
"""
from __future__ import annotations
import contextlib
import gzip
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

AUTH = {'paper_only': True, 'authenticated_execution': False,
        'real_order_submission': False, 'execution_authority': 'ZERO_AUTHORITY_RESEARCH_ONLY'}
SCHEMA = 'polymarket_v7_permanent_source_revision_v1'
CHUNK_BYTES = 4 * 1024 * 1024
IMPLEMENTATION_BYTES = Path(__file__).read_bytes()
IMPLEMENTATION_SHA256 = hashlib.sha256(IMPLEMENTATION_BYTES).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def immutable(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('symlink evidence destination')
    if path.exists():
        if path.read_bytes() != payload: raise ValueError('immutable evidence collision')
        return
    temp = path.with_name(path.name + f'.tmp.{os.getpid()}.{time.time_ns()}')
    try:
        with temp.open('xb') as f:
            f.write(payload); f.flush(); os.fsync(f.fileno())
        try: os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != payload: raise ValueError('immutable evidence collision')
        fsync_dir(path.parent)
    finally: temp.unlink(missing_ok=True)


def stat_identity(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


class EvidenceStore:
    def __init__(self, root, chunk_bytes=CHUNK_BYTES):
        self.root = Path(root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.chunk_bytes = int(chunk_bytes)
        self.implementation_sha256=IMPLEMENTATION_SHA256
        if self.chunk_bytes < 1: raise ValueError('invalid chunk size')
        self.db = sqlite3.connect(self.root/'index.sqlite', timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS sources (source_id TEXT PRIMARY KEY, revision TEXT NOT NULL, state TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS revisions (revision TEXT PRIMARY KEY, source_id TEXT NOT NULL, captured_ns INTEGER NOT NULL, partition TEXT NOT NULL)')
        self.db.commit()

    def close(self): self.db.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def object(self, payload):
        sha = digest(payload); relative = Path('objects')/sha[:2]/(sha+'.gz')
        target = self.root/relative
        locator=self.root/'objects'/sha[:2]/(sha+'.locator.json')
        if locator.exists():
            ref={'sha256':sha,'bytes':len(payload),'compressed_bytes':0,'object':str(relative),'shared_immutable_pack':True}
            if self.read_object(ref)!=payload:raise ValueError('shared pack object mismatch')
            return ref
        if target.exists():
            if target.is_symlink() or digest(gzip.decompress(target.read_bytes())) != sha:
                raise ValueError('corrupt permanent evidence object')
        else: immutable(target, gzip.compress(payload, compresslevel=6, mtime=0))
        return {'sha256': sha, 'bytes': len(payload), 'compressed_bytes': target.stat().st_size, 'object': str(relative)}

    def object_bytes(self, ref):
        path = self.root/ref['object']
        if path.is_symlink() or not path.resolve().is_relative_to(self.root): raise ValueError('unsafe evidence object')
        if not path.exists():
            yield from self.packed_object_bytes(ref);return
        checksum=hashlib.sha256();size=0
        try:stream=gzip.open(path,'rb')
        except FileNotFoundError:
            yield from self.packed_object_bytes(ref);return
        with stream:
            while True:
                payload=stream.read(self.chunk_bytes)
                if not payload:break
                checksum.update(payload);size+=len(payload);yield payload
        if checksum.hexdigest()!=ref['sha256'] or size!=ref['bytes']:
            raise ValueError('permanent evidence hash mismatch')

    def packed_object_bytes(self,ref):
        locator=self.root/'objects'/ref['sha256'][:2]/(ref['sha256']+'.locator.json')
        if locator.is_symlink():raise ValueError('unsafe packed object locator')
        value=json.loads(locator.read_text())
        if (value.get('schema')!='polymarket_v7_lossless_object_locator_v1'
                or value.get('object_sha256')!=ref['sha256'] or value.get('object_bytes')!=ref['bytes']
                or value.get('encoding') not in {'raw','gzip'}):raise ValueError('packed object contract mismatch')
        pack=self.root/'packs'/value['pack_sha256'][:2]/(value['pack_sha256']+'.pack')
        if pack.is_symlink() or not pack.resolve().is_relative_to(self.root/'packs'):raise ValueError('unsafe immutable pack')
        opener=gzip.open if value['encoding']=='gzip' else open
        checksum=hashlib.sha256();remaining=ref['bytes']
        with opener(pack,'rb') as stream:
            stream.seek(value['offset'])
            while remaining:
                payload=stream.read(min(self.chunk_bytes,remaining))
                if not payload:raise ValueError('packed object truncated')
                checksum.update(payload);remaining-=len(payload);yield payload
        if checksum.hexdigest()!=ref['sha256']:raise ValueError('packed object checksum mismatch')

    def read_object(self, ref):
        return b''.join(self.object_bytes(ref))

    def closed_gzip_object(self, path):
        # Copy the already-compressed representation; no extra compression pass
        # or unbounded decompression buffer. No hardlink to a producer-owned inode.
        checksum=hashlib.sha256();size=0;tail=b''
        with gzip.open(path,'rb') as stream:
            while True:
                payload=stream.read(self.chunk_bytes)
                if not payload:break
                checksum.update(payload);size+=len(payload);tail=(tail+payload)[-4096:]
        sha=checksum.hexdigest();relative=Path('objects')/sha[:2]/(sha+'.gz');target=self.root/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if (self.root/'objects'/sha[:2]/(sha+'.locator.json')).exists():
            ref={'sha256':sha,'bytes':size,'compressed_bytes':0,'object':str(relative),'offset':0,'shared_immutable_pack':True}
            for _ in self.object_bytes(ref):pass
            return ref,tail
        if not target.exists():
            temp=target.with_name(target.name+f'.tmp.{os.getpid()}.{time.time_ns()}')
            try:
                with path.open('rb') as source,temp.open('xb') as dest:
                    while True:
                        payload=source.read(self.chunk_bytes)
                        if not payload:break
                        dest.write(payload)
                    dest.flush();os.fsync(dest.fileno())
                os.link(temp,target);fsync_dir(target.parent)
            except FileExistsError:pass
            finally:temp.unlink(missing_ok=True)
        ref={'sha256':sha,'bytes':size,'compressed_bytes':target.stat().st_size,'object':str(relative),'offset':0}
        # Source mutation/copy corruption cannot publish an accepted revision.
        for _ in self.object_bytes(ref):pass
        return ref,tail

    def revision(self, sha):
        path = self.root/'revisions'/sha[:2]/(sha+'.json')
        if path.is_symlink(): raise ValueError('unsafe evidence revision')
        payload = path.read_bytes()
        if digest(payload) != sha: raise ValueError('revision checksum mismatch')
        value = json.loads(payload)
        if value.get('schema') != SCHEMA or any(value.get(k) != v for k,v in AUTH.items()):
            raise ValueError('unsafe evidence revision contract')
        return value

    def latest(self, source_id):
        row = self.db.execute('SELECT revision,state FROM sources WHERE source_id=?',(source_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else (None, None)

    def capture(self, path, *, partition, relative, contract, append=False, maximum_bytes=None):
        # Source cursor and immutable revision publication have one writer.
        # Readers need no lock; SQLite remains a disposable derived index.
        with (self.root/'.writer.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:return self._capture(path,partition=partition,relative=relative,contract=contract,
                                     append=append,maximum_bytes=maximum_bytes)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def _capture(self, path, *, partition, relative, contract, append=False, maximum_bytes=None):
        """Capture a stable observed byte prefix; an optional budget is resumable.

        gzip files are closed source representations: their decompressed bytes
        are captured as a whole stream. Append files keep bounded chunks, even
        when the captured prefix ends inside a record; record readers wait for
        the next revision to complete that record. File publication time is not
        relabeled as the original exchange/receive/decision timestamp.
        """
        path = Path(path)
        if path.is_symlink() or not path.is_file(): raise ValueError('source must be a regular non-symlink file')
        rel = Path(relative)
        if rel.is_absolute() or '..' in rel.parts: raise ValueError('unsafe logical source path')
        source_id = digest(canonical({'partition':partition,'path':str(rel)}))
        previous_sha, previous = self.latest(source_id)
        before = path.stat(); identity = stat_identity(before)
        if (previous and previous['stat'] == identity and previous.get('capture_complete', True)
                and previous.get('contract')==contract):
            self.revision(previous_sha)  # index is not authority
            return {'state':'UNCHANGED','source_id':source_id,'revision':previous_sha}
        compressed = path.name.endswith('.gz')
        can_append = bool(append and not compressed and previous
                          and previous['stat'][:2] == identity[:2]
                          and before.st_size >= previous['source_bytes'])
        rebound_prefix = False
        # Check a previously captured tail before trusting append semantics.
        # Mutable snapshots must use append=False and get a new complete chain.
        offset = previous['source_bytes'] if can_append else 0
        with path.open('rb') as f:
            opened = os.fstat(f.fileno())
            if stat_identity(opened) != identity: raise ValueError('source changed before capture')
            if (append and not compressed and previous and not can_append
                    and before.st_size >= previous['source_bytes']):
                # Verified compression or a cutover copy can replace an inode.
                # Reuse the old chain only after comparing every prior byte,
                # never merely because size, timestamps or the last record match.
                matches = True
                for payload in self.bytes(previous_sha):
                    if f.read(len(payload)) != payload:
                        matches = False
                        break
                if matches:
                    can_append = rebound_prefix = True
                    offset = previous['source_bytes']
            if can_append and previous.get('tail_bytes',0):
                f.seek(offset-previous['tail_bytes'])
                if digest(f.read(previous['tail_bytes'])) != previous['tail_sha256']:
                    can_append=False;offset=0
            f.seek(offset)
            target_bytes=before.st_size
            if maximum_bytes is not None and not compressed: target_bytes=min(target_bytes,offset+int(maximum_bytes))
            chunks=[];total=offset;tail=b''
            if compressed:
                ref,tail=self.closed_gzip_object(path);chunks.append(ref);total=ref['bytes']
            else:
                while total<target_bytes:
                    payload=f.read(min(self.chunk_bytes,target_bytes-total))
                    if not payload:break
                    ref=self.object(payload);ref['offset']=total;chunks.append(ref);total+=len(payload)
                    tail=(tail+payload)[-4096:]
            after=os.fstat(f.fileno())
            if after.st_ino!=before.st_ino or after.st_size<before.st_size:
                raise ValueError('source truncated during capture; no revision published')
            if compressed or not append:
                if stat_identity(after)!=identity: raise ValueError('source snapshot changed during capture')
            if not tail and offset:
                f.seek(max(0,offset-4096));tail=f.read(min(offset,4096))
        if not compressed and total!=target_bytes: raise ValueError('source short read')
        complete=compressed or total==before.st_size
        value={'schema':SCHEMA,**AUTH,'source_id':source_id,'partition':partition,
               'relative_path':str(rel),'original_path_at_capture':str(path.absolute()),
               'captured_ns':time.time_ns(),'capture_implementation_sha256':self.implementation_sha256,'source_bytes':total,'source_physical_bytes':before.st_size,
               'capture_complete':complete,'source_compression':'gzip' if compressed else 'none',
               'previous_prefix_rebound_after_full_byte_verification':rebound_prefix,
               'stat':identity,'previous_revision':previous_sha if can_append else None,
               'supersedes_without_destroying':previous_sha if previous_sha and not can_append else None,
               'chunks':chunks,'contract':contract,'tail_bytes':len(tail),'tail_sha256':digest(tail)}
        encoded=canonical(value);revision=digest(encoded)
        immutable(self.root/'implementation_sources'/(self.implementation_sha256+'.py.gz'),gzip.compress(IMPLEMENTATION_BYTES,mtime=0))
        immutable(self.root/'revisions'/revision[:2]/(revision+'.json'),encoded)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?)',(revision,source_id,value['captured_ns'],partition))
            self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)',(source_id,revision,canonical(value).decode()))
        return {'state':'CAPTURED','source_id':source_id,'revision':revision,'new_bytes':total-offset,
                'source_bytes':total,'capture_complete':complete,'objects':len(chunks)}

    def bytes(self, revision):
        chain=[];seen=set();sha=revision
        while sha:
            if sha in seen: raise ValueError('revision cycle')
            seen.add(sha);value=self.revision(sha);chain.append(value);sha=value['previous_revision']
        offset=0
        for value in reversed(chain):
            for ref in value['chunks']:
                if ref['offset']!=offset: raise ValueError('source chunk gap or overlap')
                for payload in self.object_bytes(ref):
                    offset+=len(payload);yield payload
            if offset!=value['source_bytes']: raise ValueError('source length mismatch')

    def json_rows(self, revision):
        pending=b''
        for payload in self.bytes(revision):
            pending+=payload
            while b'\n' in pending:
                line,pending=pending.split(b'\n',1)
                if line.strip():yield json.loads(line)
        # Incomplete tails are preserved byte-for-byte but are not observations.

    def heads(self):
        return [{'source_id':r[0],'revision':r[1],**json.loads(r[2])} for r in self.db.execute('SELECT source_id,revision,state FROM sources ORDER BY source_id')]

    def rebuild_index(self):
        """Recover without original paths, source writer, model or mutable index."""
        values=[]
        for path in sorted((self.root/'revisions').glob('*/*.json')):
            sha=path.stem;value=self.revision(sha)
            # Every referenced object must still be readable before indexing.
            for ref in value['chunks']:
                for _ in self.object_bytes(ref):pass
            values.append((value['captured_ns'],sha,value))
        with self.db:
            self.db.execute('DELETE FROM sources');self.db.execute('DELETE FROM revisions')
            for _,sha,value in sorted(values):
                self.db.execute('INSERT INTO revisions VALUES (?,?,?,?)',(sha,value['source_id'],value['captured_ns'],value['partition']))
                self.db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)',(value['source_id'],sha,canonical(value).decode()))
        return {'revisions':len(values),'sources':len(self.heads())}
