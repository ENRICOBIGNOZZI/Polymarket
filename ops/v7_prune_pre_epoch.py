#!/usr/bin/env python3
"""One-time user-authorized pre-epoch raw cleanup; never a rolling policy."""
import hashlib
import json
import os
from pathlib import Path
import stat


def candidate(path, active_root, cutoff_ns, open_files):
    if path.is_symlink() or active_root==path or active_root in path.parents:
        return False
    relative=path.relative_to(active_root.parent)
    if not relative.parts[0].startswith('paper_v7_london'):
        return False
    if any(any(word in part.lower() for word in ('model','credential','secret','config','ledger','artifact')) for part in relative.parts):
        return False
    if not set(relative.parts)&{'raw','normalized_events','book_observations','native_observations','tapes'}:
        return False
    if not path.name.endswith(('.bin','.bin.gz','.jsonl','.jsonl.gz')):
        return False
    info=path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_mtime_ns<cutoff_ns and (info.st_dev,info.st_ino) not in open_files


def plan(active_root,cutoff_ns,open_files):
    result=[]
    for folder in active_root.parent.glob('paper_v7_london*'):
        if folder==active_root or folder.is_symlink() or not folder.is_dir():continue
        for path in folder.rglob('*'):
            try:
                if candidate(path,active_root,cutoff_ns,open_files):
                    info=path.lstat()
                    result.append(dict(path=str(path),identity=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns],allocated=info.st_blocks*512))
            except FileNotFoundError:pass
    return sorted(result,key=lambda r:r['path'])


def execute(rows,active_root,cutoff_ns,open_files):
    removed=skipped=logical=allocated=0
    for row in rows:
        path=Path(row['path'])
        try:
            info=path.lstat()
            identity=[info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
            if identity!=row['identity'] or not candidate(path,active_root,cutoff_ns,open_files):
                skipped+=1;continue
            path.unlink();removed+=1;logical+=info.st_size;allocated+=info.st_blocks*512
        except FileNotFoundError:skipped+=1
    return dict(removed_files=removed,removed_logical_bytes=logical,removed_allocated_bytes=allocated,skipped=skipped)
