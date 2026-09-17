#!/usr/bin/env python3
"""Build a minimal London filesystem from the checked-in runtime manifest."""
from __future__ import annotations
import argparse,ast,json,os,shutil,subprocess,sys
from pathlib import Path
SEARCH_DIRS=('scripts','monitoring','ops')

def local_imports(path:Path)->set[str]:
    if path.suffix!='.py': return set()
    out=set()
    try: tree=ast.parse(path.read_text(encoding='utf-8'))
    except SyntaxError: return out
    for node in ast.walk(tree):
        if isinstance(node,ast.Import): out|={a.name.split('.')[0] for a in node.names if a.name.split('.')[0].startswith(('v7_','exporter_v7'))}
        elif isinstance(node,ast.ImportFrom) and node.module:
            root=node.module.split('.')[0]
            if root.startswith(('v7_','exporter_v7')): out.add(root)
    return out

def resolve_module(root:Path,name:str)->Path|None:
    for d in SEARCH_DIRS:
        p=root/d/(name+'.py')
        if p.is_file(): return p
    return None

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path,default=Path('.'));ap.add_argument('--manifest',type=Path,default=Path('deploy/london/runtime_manifest.json'));ap.add_argument('--dest',type=Path,required=True);a=ap.parse_args();root=a.source.resolve();m=json.loads((root/a.manifest).read_text() if not a.manifest.is_absolute() else a.manifest.read_text())
    if m.get('schema')!='polymarket_v7_london_runtime_files_v1' or m.get('paper_only') is not True: raise SystemExit('runtime manifest invalid')
    queue=[root/x for x in m['python_entrypoints']]; files={root/x for x in m['direct_files']+m['config_files']}; seen=set()
    while queue:
        p=queue.pop()
        if p in seen: continue
        if not p.is_file(): raise SystemExit(f'missing runtime file: {p}')
        seen.add(p);files.add(p)
        for mod in local_imports(p):
            dep=resolve_module(root,mod)
            if dep is None: raise SystemExit(f'unresolved local runtime import {mod} from {p}')
            queue.append(dep)
    rels=sorted(str(p.relative_to(root)) for p in files)
    forbidden=m.get('forbidden_path_fragments') or []
    for rel in rels:
        probe='/'+rel
        if any(f in probe for f in forbidden): raise SystemExit(f'forbidden London runtime file: {rel}')
    if a.dest.exists(): shutil.rmtree(a.dest)
    for rel in rels:
        src=root/rel; dst=a.dest/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
    (a.dest/'deploy/london').mkdir(parents=True,exist_ok=True)
    shutil.copy2(root/'deploy/london/runtime_manifest.json',a.dest/'deploy/london/runtime_manifest.json')
    sha=os.environ.get('PM_V7_MODEL_SHA','').strip()
    if not sha:
        try: sha=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
        except (OSError,subprocess.SubprocessError): sha=''
    if len(sha)!=40 or any(c not in '0123456789abcdef' for c in sha): raise SystemExit('exact staging SHA unavailable')
    (a.dest/'deploy/london/runtime_sha').write_text(sha+'\n')
    print(json.dumps({'schema':'polymarket_v7_london_stage_v1','file_count':len(rels),'files':rels,'runtime_sha':sha},sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
