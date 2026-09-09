#!/usr/bin/env python3
"""Lossless shared packs: transparent source reads and deduplicated CAS objects.

Only closed immutable source groups may enter. Every original alias keeps the
same readable bytes. Pack + slice locators make old CAS revisions readable after
removal of redundant gzip representations. No unique observation is discarded.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from v7_evidence_store import EvidenceStore,AUTH,canonical,digest,immutable,fsync_dir

CHUNK=4*1024**2
SCHEMA='polymarket_v7_lossless_shared_pack_v1'
IMPLEMENTATION_BYTES=Path(__file__).read_bytes()
IMPLEMENTATION_SHA256=hashlib.sha256(IMPLEMENTATION_BYTES).hexdigest()


def recompress_pack(pack,aliases,store,*,compressor=None,check_closed=None):
    """Re-encode a verified pack and all surviving aliases without changing bytes.

    Caller holds the store writer lock. An interrupted publication can be
    resumed because each surviving inode is independently checked against SHA.
    """
    compressor=compressor or transparent_copy;check_closed=check_closed or closed
    pack=Path(pack);sha=pack.stem
    paths=list(dict.fromkeys([pack]+[Path(p) for p in aliases if Path(p).exists()]))
    stats={p:p.lstat() for p in paths};groups=defaultdict(list)
    for p,s in stats.items():
        if not stat.S_ISREG(s.st_mode):raise ValueError('nonregular pack alias')
        groups[(s.st_dev,s.st_ino)].append(p)
    for group in groups.values():
        if stats[group[0]].st_nlink!=len(group):raise ValueError('uninspected pack hardlink')
        if file_hash(group[0])!=sha:raise ValueError('pack alias content differs')
    if not all(check_closed(p) for p in paths):raise ValueError('pack alias is open')
    temporary=pack.with_name(pack.name+f'.recompress.{os.getpid()}.{time.time_ns()}')
    before=sum(stats[g[0]].st_blocks*512 for g in groups.values())
    try:
        compressor(pack,temporary)
        if file_hash(temporary)!=sha:raise ValueError('recompression changed readable bytes')
        after=temporary.stat().st_blocks*512
        if after>=before:return {'state':'NO_SAVINGS','pack_sha256':sha}
        os.chmod(temporary,stat.S_IMODE(stats[pack].st_mode)&~0o222)
        with temporary.open('rb') as f:os.fsync(f.fileno())
        proof={'schema':'polymarket_v7_lossless_pack_recompression_v1',**AUTH,
               'pack_sha256':sha,'source_aliases':[str(p) for p in paths],
               'before_allocated_bytes':before,'after_allocated_bytes':after,
               'implementation_sha256':IMPLEMENTATION_SHA256,'created_ns':time.time_ns(),
               'readable_bytes_sha256_verified':True}
        immutable(store.root/'implementation_sources'/(IMPLEMENTATION_SHA256+'.py'),IMPLEMENTATION_BYTES)
        immutable(store.root/'pack_recompressions'/(digest(canonical(proof))+'.json'),canonical(proof))
        if any(stable(p.lstat())!=stable(stats[p]) for p in paths) or not all(check_closed(p) for p in paths):
            raise ValueError('pack alias changed before publication')
        for p in paths:
            link=p.with_name(p.name+f'.recompress-link.{os.getpid()}')
            try:os.link(temporary,link);os.replace(link,p);fsync_dir(p.parent)
            finally:link.unlink(missing_ok=True)
        return {**proof,'reclaimed_bytes':before-after,'state':'RECOMPRESSED'}
    finally:temporary.unlink(missing_ok=True)


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(CHUNK),b''):h.update(b)
    return h.hexdigest()


def stable(stat):return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns)


def closed(path):
    lsof=shutil.which('lsof') or '/usr/sbin/lsof'
    result=subprocess.run([lsof,'-t','--',str(path)],capture_output=True,text=True,timeout=10)
    return result.returncode==1 and not result.stdout.strip() and not result.stderr.strip()


def transparent_copy(source,target):
    if source.stat().st_size<512*1024**2:
        subprocess.run(['/usr/bin/ditto','--hfsCompression','--noclone',str(source),str(target)],check=True,timeout=300,capture_output=True)
        return
    # ditto declines filesystem compression above its large-file threshold.
    # Apple Archive streams the file into kernel-supported LZFSE compression.
    with tempfile.TemporaryDirectory(prefix='afsc-',dir=target.parent) as directory:
        archive=subprocess.Popen(['/usr/bin/aa','archive','-i',str(source),'-a','raw','-t','1'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        extract=None
        try:
            extract=subprocess.Popen(['/usr/bin/aa','extract','-d',directory,'-afsc','lzfse','-afsc-all','-t','1'],
                stdin=archive.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            archive.stdout.close();archive.stdout=None
            _,error=extract.communicate(timeout=300);archive.wait(timeout=30)
            if archive.returncode or extract.returncode:raise ValueError('Apple Archive compression failed: '+error.decode()[:200])
            os.replace(Path(directory)/source.name,target)
        finally:
            for process in (archive,extract):
                if process is not None and process.poll() is None:process.kill();process.wait()


def eligible(path,root_kind,now):
    if path.is_symlink() or not path.is_file() or path.stat().st_size<65536:return False
    if now-path.stat().st_mtime<3600:return False
    # No mutable live JSONL/CSV/log is rewritten under its producer. Old
    # cutovers are immutable; native numbered segments never reopen for append.
    if any(p.startswith('cutover-') for p in path.parts):return True
    return '.segment-' in path.name and path.suffix in {'.bin','.gz'}


def compact_group(paths,store,*,compressor=None,check_closed=closed):
    paths=[Path(p).absolute() for p in paths]
    if not paths:raise ValueError('empty source group')
    stats=[p.lstat() for p in paths];before=stats[0]
    if any(not stat.S_ISREG(s.st_mode) or stable(s)!=stable(before) for s in stats):raise ValueError('source alias identity changed')
    if before.st_nlink!=len(paths):raise ValueError('source aliases outside inspected roots')
    if not all(check_closed(p) for p in paths):raise ValueError('source is open; compression deferred')
    sha=file_hash(paths[0]);pack=store.root/'packs'/sha[:2]/(sha+'.pack');pack.parent.mkdir(parents=True,exist_ok=True)
    if pack.exists():
        if pack.is_symlink() or file_hash(pack)!=sha:raise ValueError('immutable pack collision')
    else:
        temporary=pack.with_name(pack.name+f'.tmp.{os.getpid()}.{time.time_ns()}')
        try:
            if compressor is None:
                transparent_copy(paths[0],temporary)
            else:compressor(paths[0],temporary)
            if file_hash(temporary)!=sha:raise ValueError('transparent compression changed source bytes')
            with temporary.open('rb') as f:os.fsync(f.fileno())
            os.chmod(temporary,stat.S_IMODE(before.st_mode)&~0o222)
            os.link(temporary,pack);fsync_dir(pack.parent)
        finally:temporary.unlink(missing_ok=True)
    # Register slice hashes, including gzip's entire decoded object. Each
    # locator points to a pack owned by the permanent store, not a run pathname.
    encoding='gzip' if paths[0].suffix=='.gz' else 'raw';opener=gzip.open if encoding=='gzip' else open
    refs=[];offset=0;whole=hashlib.sha256()
    with opener(pack,'rb') as f:
        for block in iter(lambda:f.read(CHUNK),b''):
            if encoding=='raw':refs.append((digest(block),offset,len(block)))
            offset+=len(block);whole.update(block)
    if encoding=='gzip':refs.append((whole.hexdigest(),0,offset))
    # Captured append prefixes sometimes end between 4 MiB boundaries. Reuse
    # their recorded offsets as well, validating against the actual sealed bytes.
    if not hasattr(store,'_compaction_heads_by_inode'):
        store._compaction_heads_by_inode=defaultdict(list)
        for (raw,) in store.db.execute('SELECT state FROM sources'):
            value=json.loads(raw);store._compaction_heads_by_inode[tuple(value.get('stat',[])[:2])].append(value)
    heads=store._compaction_heads_by_inode.get((before.st_dev,before.st_ino),[])
    checked=set()
    for source in heads:
        chain=source
        while chain:
            for ref in chain['chunks']:
                key=(ref['sha256'],ref['offset'],ref['bytes'])
                if key not in checked:
                    with opener(pack,'rb') as f:
                        f.seek(ref['offset']);h=hashlib.sha256();remaining=ref['bytes']
                        while remaining:
                            block=f.read(min(CHUNK,remaining))
                            if not block:raise ValueError('sealed pack does not contain old source prefix')
                            h.update(block);remaining-=len(block)
                    if h.hexdigest()!=ref['sha256']:raise ValueError('sealed source differs from captured prefix')
                    refs.append(key);checked.add(key)
            previous=chain.get('previous_revision');chain=store.revision(previous) if previous else None
    manifest={'schema':SCHEMA,**AUTH,'pack_sha256':sha,'pack_path':str(pack.relative_to(store.root)),
      'pack_bytes':pack.stat().st_size,'pack_allocated_bytes':pack.stat().st_blocks*512,
      'source_original_allocated_bytes':before.st_blocks*512,'source_encoding':encoding,
      'source_aliases':[str(p) for p in paths],'source_original_stat':list(stable(before)),
      'decoded_bytes':offset,'created_ns':time.time_ns(),'source_bytes_sha256_verified':True,
      'implementation_sha256':IMPLEMENTATION_SHA256,
      'object_locators':len(set(refs)),'scope':'LOSSLESS_CLOSED_SOURCE_PACK; EXACT_READ_BYTES; NO_SEMANTIC_REDUCTION'}
    immutable(store.root/'implementation_sources'/(IMPLEMENTATION_SHA256+'.py'),IMPLEMENTATION_BYTES)
    payload=canonical(manifest);receipt=store.root/'pack_manifests'/(digest(payload)+'.json');immutable(receipt,payload)
    # Manifest and verified pack exist before any alias or redundant object moves.
    if any(stable(p.stat())!=stable(before) for p in paths) or not all(check_closed(p) for p in paths):
        raise ValueError('source changed or opened before atomic compression publication')
    for p in paths:
        temp=p.with_name(p.name+f'.compact-link.{os.getpid()}')
        try:os.link(pack,temp);os.replace(temp,p);fsync_dir(p.parent)
        finally:temp.unlink(missing_ok=True)
    reclaimed_objects=0
    for object_sha,start,size in set(refs):
        locator=store.root/'objects'/object_sha[:2]/(object_sha+'.locator.json')
        value={'schema':'polymarket_v7_lossless_object_locator_v1','object_sha256':object_sha,
               'object_bytes':size,'pack_sha256':sha,'offset':start,'encoding':encoding,'pack_manifest':str(receipt.relative_to(store.root))}
        if locator.exists():
            # An earlier compatible pack remains the chosen immutable owner.
            existing=json.loads(locator.read_text())
            if existing.get('object_sha256')!=object_sha or existing.get('object_bytes')!=size:raise ValueError('object locator collision')
        else:immutable(locator,canonical(value))
        ref={'sha256':object_sha,'bytes':size,'object':str(Path('objects')/object_sha[:2]/(object_sha+'.gz'))}
        for _ in store.packed_object_bytes(ref):pass
        redundant=store.root/ref['object']
        if redundant.exists():
            for _ in store.object_bytes(ref):pass
            reclaimed_objects+=redundant.stat().st_blocks*512;redundant.unlink();fsync_dir(redundant.parent)
    return {**manifest,'manifest':str(receipt),'reclaimed_object_bytes':reclaimed_objects,
      'estimated_reclaimed_source_bytes':max(0,before.st_blocks*512-pack.stat().st_blocks*512)}


def inventory_groups(roots):
    groups=defaultdict(list);kinds={};now=time.time()
    for kind,root in roots.items():
        root=Path(root).resolve()
        for folder,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not (Path(folder)/d).is_symlink() and d!='permanent_evidence']
            for name in files:
                p=Path(folder)/name
                if p.is_symlink():continue
                try:s=p.stat()
                except FileNotFoundError:continue
                key=(s.st_dev,s.st_ino);groups[key].append(p)
                kinds[p]=kind
    return [paths for paths in groups.values() if paths[0].stat().st_nlink==len(paths)
            and all(eligible(p,kinds[p],now) for p in paths)]


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--root',action='append',required=True,help='kind=path')
    ap.add_argument('--store',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--maximum-groups',type=int,default=20);ap.add_argument('--apply',action='store_true')
    ap.add_argument('--maximum-seconds',type=float)
    ap.add_argument('--nonblocking',action='store_true')
    a=ap.parse_args();started=time.monotonic();groups=inventory_groups(dict(x.split('=',1) for x in a.root));groups.sort(key=lambda ps:ps[0].stat().st_blocks,reverse=True)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with EvidenceStore(a.store) as store,(store.root/'.writer.lock').open('a') as lock,a.output.open('a') as out:
        try:fcntl.flock(lock,fcntl.LOCK_EX|(fcntl.LOCK_NB if a.nonblocking else 0))
        except BlockingIOError:
            print(json.dumps({'state':'DEFERRED_EXISTING_STORE_WRITER'}));return
        for paths in groups[:a.maximum_groups]:
            if a.maximum_seconds is not None and time.monotonic()-started>=a.maximum_seconds:break
            try:
                result=compact_group(paths,store) if a.apply else {'state':'PLANNED','paths':[str(p) for p in paths],'bytes':paths[0].stat().st_size}
            except (OSError,ValueError,subprocess.SubprocessError) as exc:result={'state':'DEFERRED','paths':[str(p) for p in paths],'reason':str(exc)}
            out.write(json.dumps(result)+'\n');out.flush();os.fsync(out.fileno())
            print(json.dumps({k:result[k] for k in ['state','pack_sha256','estimated_reclaimed_source_bytes','reclaimed_object_bytes','reason'] if k in result}),flush=True)

if __name__=='__main__':main()
