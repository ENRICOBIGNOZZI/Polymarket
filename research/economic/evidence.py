"""Freeze/verify/restore research files without deleting or rewriting live data."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterable


def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1<<20),b''): h.update(block)
    return h.hexdigest()


def freeze(source: Path, destination: Path, relatives: Iterable[str], *, identity: dict) -> dict:
    """Copy closed files to a new directory and verify them twice.

    A concurrent source change invalidates the export. Caller must select closed
    segments; no automatic cleanup or retention authorization is performed.
    """
    source=source.resolve(); destination=destination.resolve()
    if destination.exists(): raise ValueError('destination must be new')
    rows=[]; destination.mkdir(parents=True)
    try:
        for name in relatives:
            rel=Path(name)
            if rel.is_absolute() or '..' in rel.parts: raise ValueError('unsafe relative path')
            original=source/rel
            if original.is_symlink() or not original.is_file() or not original.resolve().is_relative_to(source):
                raise ValueError('source not an ordinary contained file')
            before=original.stat(); original_hash=digest(original)
            target=destination/rel; target.parent.mkdir(parents=True,exist_ok=True)
            with original.open('rb') as src,target.open('xb') as dst:
                shutil.copyfileobj(src,dst,1<<20);dst.flush();os.fsync(dst.fileno())
            after=original.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                raise ValueError('source changed during freeze')
            if digest(target)!=original_hash or digest(original)!=original_hash:
                raise ValueError('checksum mismatch')
            rows.append({'path':rel.as_posix(),'bytes':before.st_size,'sha256':original_hash})
        manifest={'schema':'polymarket_economic_dataset_v1','identity':identity,'files':rows,
            'source_delete_authorized':False,'verified_copy':True}
        encoded=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()
        manifest['dataset_sha256']=hashlib.sha256(encoded).hexdigest()
        (destination/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        verify(destination)
        return manifest
    except Exception:
        # Preserve partial copies for diagnosis, explicitly never call them verified.
        (destination/'INCOMPLETE').write_text('Freeze failed; no source files were deleted.\n')
        raise


def verify(root: Path) -> dict:
    root=root.resolve()
    if (root/'INCOMPLETE').exists(): raise ValueError('incomplete export')
    manifest=json.loads((root/'manifest.json').read_text())
    if manifest.get('schema')!='polymarket_economic_dataset_v1': raise ValueError('manifest schema')
    signed={k:v for k,v in manifest.items() if k!='dataset_sha256'}
    if hashlib.sha256(json.dumps(signed,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=manifest.get('dataset_sha256'):
        raise ValueError('manifest identity mismatch')
    for row in manifest['files']:
        rel=Path(row['path']);p=root/rel
        if rel.is_absolute() or '..' in rel.parts or p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError('invalid manifest path')
        if p.stat().st_size!=row['bytes'] or digest(p)!=row['sha256']: raise ValueError('file integrity mismatch')
    return manifest


def restore_test(root: Path) -> dict:
    manifest=verify(root)
    with tempfile.TemporaryDirectory(prefix='pm-restore-') as temp:
        copy=Path(temp)/'copy'
        restored=freeze(root,copy,[r['path'] for r in manifest['files']],identity=manifest['identity'])
        if restored['dataset_sha256']!=manifest['dataset_sha256']: raise ValueError('restore differs')
    return {'schema':'polymarket_restore_receipt_v1','dataset_sha256':manifest['dataset_sha256'],
        'passed':True,'source_delete_authorized':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('mode',choices=['verify','restore']);p.add_argument('root',type=Path)
    a=p.parse_args();print(json.dumps(verify(a.root) if a.mode=='verify' else restore_test(a.root),indent=2))
