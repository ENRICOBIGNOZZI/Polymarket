#!/usr/bin/env python3
"""Build a minimal London runtime tree from explicit entrypoints plus local import closure."""
from __future__ import annotations
import argparse,ast,json,os,shutil
from pathlib import Path

def local_imports(path:Path, scripts:Path)->set[Path]:
    try: tree=ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError,UnicodeDecodeError): return set()
    names=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Import): names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node,ast.ImportFrom) and node.module: names.add(node.module.split('.')[0])
    out=set()
    for name in names:
        candidate=scripts/(name+'.py')
        if candidate.is_file(): out.add(candidate)
    return out

def closure(root:Path, entries:list[str])->set[Path]:
    scripts=root/'scripts'; pending=[]; seen=set()
    for rel in entries:
        p=root/rel
        if p.suffix=='.py': pending.append(p)
        seen.add(p)
    while pending:
        p=pending.pop()
        for q in local_imports(p,scripts):
            if q not in seen: seen.add(q); pending.append(q)
    return seen

def copy(root:Path,out:Path,path:Path)->None:
    rel=path.relative_to(root); dst=out/rel; dst.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(path,dst)

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--repository-root',type=Path,default=Path('.')); ap.add_argument('--manifest',type=Path,default=Path('deploy/london/runtime_manifest.json')); ap.add_argument('--build-dir',type=Path,default=Path('build')); ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args(); root=a.repository_root.resolve(); manifest=json.loads((root/a.manifest).read_text()) if not a.manifest.is_absolute() else json.loads(a.manifest.read_text()); out=a.output.resolve()
    if manifest.get('schema')!='polymarket_v7_london_runtime_bundle_v1' or manifest.get('paper_only') is not True or manifest.get('authenticated_execution') is not False or manifest.get('real_order_submission') is not False: raise SystemExit('invalid runtime bundle manifest')
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    files=closure(root,list(manifest['python_entrypoints']))
    files.update(root/x for x in manifest['support_files'])
    files.add(root/'deploy/london/runtime_manifest.json')
    for p in sorted(files):
        if not p.is_file(): raise SystemExit(f'missing runtime file:{p.relative_to(root)}')
        copy(root,out,p)
    bindir=out/'build'; bindir.mkdir(parents=True,exist_ok=True)
    for name in manifest['binaries']:
        src=(root/a.build_dir/name).resolve() if not a.build_dir.is_absolute() else a.build_dir/name
        if not src.is_file(): raise SystemExit(f'missing runtime binary:{name}')
        shutil.copy2(src,bindir/name)
    # Enforce final-tree boundary, not just source intent.
    rels=[str(p.relative_to(out)) for p in out.rglob('*') if p.is_file()]
    forbidden=list(manifest['forbidden_path_fragments'])
    bad=[r for r in rels if any(f in ('/'+r) or f in r for f in forbidden)]
    if bad: raise SystemExit('forbidden London files:'+','.join(sorted(bad)))
    receipt={'schema':'polymarket_v7_london_runtime_bundle_receipt_v1','paper_only':True,'files':len(rels),'binaries':manifest['binaries'],'training_files':0,'research_tree_present':False}
    (out/'runtime_bundle_receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    print(json.dumps(receipt,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
