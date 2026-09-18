#!/usr/bin/env python3
"""Build the explicit, research-free London runtime filesystem."""
from __future__ import annotations
import argparse,ast,json,re,shutil,subprocess
from pathlib import Path

SEARCH_DIRS=("scripts","monitoring","ops")
SHA40=re.compile(r"^[0-9a-f]{40}$")

def module_imports(path:Path)->set[str]:
    if path.suffix!='.py': return set()
    try: tree=ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError,UnicodeDecodeError): return set()
    names=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Import): names.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node,ast.ImportFrom) and node.module: names.add(node.module.split('.')[0])
    return names

def resolve_local(root:Path,name:str)->Path|None:
    for directory in SEARCH_DIRS:
        p=root/directory/(name+'.py')
        if p.is_file(): return p
    return None

def local_closure(root:Path,seeds:set[Path])->set[Path]:
    pending=[p for p in seeds if p.suffix=='.py']; seen=set(seeds)
    while pending:
        source=pending.pop()
        for name in module_imports(source):
            dep=resolve_local(root,name)
            if dep is not None and dep not in seen:
                seen.add(dep); pending.append(dep)
    return seen

def copy_one(root:Path,out:Path,path:Path)->None:
    rel=path.relative_to(root); dst=out/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(path,dst)

def git_sha(root:Path,explicit:str|None)->str:
    if explicit:
        value=explicit.strip()
    else:
        try: value=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
        except (OSError,subprocess.SubprocessError): value=''
    if not SHA40.fullmatch(value): raise SystemExit('exact London runtime SHA unavailable')
    return value

def verify_git_tree(root:Path,sha:str,files:set[Path])->None:
    try:
        head=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
        dirty=subprocess.check_output(['git','-C',str(root),'status','--porcelain','--untracked-files=normal'],text=True,stderr=subprocess.DEVNULL)
    except (OSError,subprocess.SubprocessError) as exc:
        raise SystemExit('London runtime source git verification unavailable') from exc
    if head != sha: raise SystemExit('London runtime source HEAD does not match declared SHA')
    if dirty.strip(): raise SystemExit('London runtime source checkout is dirty')
    for path in sorted(files):
        rel=str(path.relative_to(root))
        try:
            blob=subprocess.check_output(['git','-C',str(root),'show',f'{sha}:{rel}'],stderr=subprocess.DEVNULL)
        except (OSError,subprocess.SubprocessError) as exc:
            raise SystemExit(f'London runtime source is not tracked at declared SHA:{rel}') from exc
        if blob != path.read_bytes():
            raise SystemExit(f'London runtime source differs from declared SHA:{rel}')

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--repository-root',type=Path,default=Path('.'))
    ap.add_argument('--manifest',type=Path,default=Path('deploy/london/runtime_manifest.json'))
    ap.add_argument('--build-dir',type=Path,default=Path('build'))
    ap.add_argument('--expected-sha')
    ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args(); root=a.repository_root.resolve(); mp=a.manifest if a.manifest.is_absolute() else root/a.manifest
    manifest=json.loads(mp.read_text()); out=a.output.resolve(); sha=git_sha(root,a.expected_sha)
    if (manifest.get('schema')!='polymarket_v7_london_runtime_bundle_v1' or manifest.get('paper_only') is not True
        or manifest.get('authenticated_execution') is not False or manifest.get('real_order_submission') is not False):
        raise SystemExit('invalid London runtime manifest')
    seeds={root/x for x in manifest['python_entrypoints']+manifest['support_files']}
    seeds.add(mp)
    files=local_closure(root,seeds)
    for p in files:
        if not p.is_file(): raise SystemExit(f'missing London runtime source:{p.relative_to(root)}')
    verify_git_tree(root,sha,files)
    if out.exists(): shutil.rmtree(out)
    out.mkdir(parents=True)
    for p in sorted(files): copy_one(root,out,p)
    bindir=out/'build'; bindir.mkdir(parents=True,exist_ok=True)
    build=a.build_dir if a.build_dir.is_absolute() else root/a.build_dir
    for name in manifest['binaries']:
        src=build/name
        if not src.is_file(): raise SystemExit(f'missing London runtime binary:{name}')
        shutil.copy2(src,bindir/name)
    identity=out/'deploy/london/runtime_sha'; identity.parent.mkdir(parents=True,exist_ok=True); identity.write_text(sha+'\n')
    rels=sorted(str(p.relative_to(out)) for p in out.rglob('*') if p.is_file())
    forbidden=list(manifest['forbidden_path_fragments'])
    bad=[rel for rel in rels if any(f in ('/'+rel) or f in rel for f in forbidden)]
    if bad: raise SystemExit('forbidden London files:'+','.join(bad))
    receipt={'schema':'polymarket_v7_london_runtime_bundle_receipt_v1','paper_only':True,'authenticated_execution':False,
             'real_order_submission':False,'runtime_sha':sha,'files':len(rels),'binaries':manifest['binaries'],
             'training_files':0,'research_tree_present':False,'source_tree_verified':True}
    (out/'runtime_bundle_receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
    print(json.dumps(receipt,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
