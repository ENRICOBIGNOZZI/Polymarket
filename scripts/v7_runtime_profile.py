#!/usr/bin/env python3
"""Validate the single checked-in PAPER runtime profile and emit shell-safe fields."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path

SCHEMA='polymarket_v7_runtime_profile_v1'
KEY=re.compile(r'^[A-Z][A-Z0-9_]+$')
REQUIRED={
 'CONFIG','MAKER_POLICY','EXTERNAL_FAIR_POLICY','LEAD_LAG_TAKER_CONFIG',
 'CRYPTO_EXECUTION_ALPHA_CONFIG','EXTERNAL_SOURCE_REGISTRY','LIVE_MODEL_SCOPE',
 'CRYPTO_SETTLEMENT_ENGINE_POLICY','CRYPTO_SETTLEMENT_MARKET_REGISTRY',
 'CRYPTO_SETTLEMENT_MODEL_REGISTRY','LONDON_BUFFER_RETENTION_CONFIG',
 'CRYPTO_UNIVERSE_CONFIG','RUNTIME_RESOURCE_CONFIG',
}

def resolve(root:Path, profile:Path)->dict[str,str]:
    value=json.loads(profile.read_text())
    if (value.get('schema')!=SCHEMA or value.get('version')!=1 or value.get('paper_only') is not True
        or value.get('execution_mode')!='PAPER_SIMULATED'
        or value.get('authenticated_execution') is not False
        or value.get('real_order_submission') is not False):
        raise ValueError('unsafe runtime profile')
    paths=value.get('paths') if isinstance(value.get('paths'),dict) else {}
    if set(paths)!=REQUIRED: raise ValueError('runtime profile path set mismatch')
    out={}
    for key,rel in paths.items():
        if not KEY.fullmatch(key) or not isinstance(rel,str) or not rel or rel.startswith('/') or '..' in Path(rel).parts:
            raise ValueError(f'unsafe runtime profile path:{key}')
        path=(root/rel).resolve()
        if not path.is_file() or not path.is_relative_to(root): raise ValueError(f'missing runtime profile path:{key}')
        out[key]=rel
    out['PM_V7_EXECUTION_MODE']='PAPER_SIMULATED'
    return out

def main()->int:
    ap=argparse.ArgumentParser();ap.add_argument('--repository-root',type=Path,default=Path('.'));ap.add_argument('--profile',type=Path,required=True);ap.add_argument('--shell',action='store_true');a=ap.parse_args()
    root=a.repository_root.resolve(); profile=a.profile if a.profile.is_absolute() else root/a.profile
    out=resolve(root,profile)
    if a.shell:
        for key in sorted(out): print(f'{key}\t{out[key]}')
    else: print(json.dumps(out,sort_keys=True))
    return 0
if __name__=='__main__':raise SystemExit(main())
