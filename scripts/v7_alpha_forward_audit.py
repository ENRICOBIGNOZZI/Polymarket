#!/usr/bin/env python3
"""Read-only audit of frozen fair forecasts and nominal lead/lag timing."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from v7_external_rich_train import build_rows
from v7_external_lead_lag_collector import horizon_eligible, observation_rows
from v7_maker_durable_learning import order_examples, exact_execution_cell
from v7_compressed_journal import journal_rows


def stream(path, manifest):
    digest=hashlib.sha256(); size=0
    with path.open('rb') as handle:
        limit=path.stat().st_size
        while handle.tell()<limit:
            raw=handle.readline(limit-handle.tell())
            if not raw.endswith(b'\n'): break
            digest.update(raw); size+=len(raw)
            if raw.strip(): yield json.loads(raw)
    manifest.append({'path':str(path),'prefix_bytes':size,'sha256':digest.hexdigest()})


def audit(run_root, durable_root):
    manifest=[]
    model=json.loads((durable_root/'external_fair/rich_research_model.json').read_text())
    hp=model['hyperparameters']; model_hash=model['model_hash']
    excluded=set(hp['development_market_ids'])
    boundary=int(hp['forward_oos_starts_after_ns'])//1_000_000
    origins={}; finals={}
    for r in stream(durable_root/'external_fair/counterfactuals.jsonl',manifest):
        if r.get('event_type')=='FORECAST' and r.get('research_model_model_hash')==model_hash \
                and r.get('market_id') not in excluded and int(r.get('observed_ms') or 0)>boundary:
            origins[r['forecast_id']]=r
        elif r.get('event_type')=='FORECAST_FINAL':
            finals[r['forecast_id']]=r
    matched=[v for k,o in origins.items() if k in finals for v in (o,finals[k])]
    rows, exclusions=build_rows(matched,time.time_ns()//1_000_000)
    scores={}
    for name,key in [('PM','market_yes'),('structural','external_only_yes'),
                     ('hybrid','hybrid_yes'),('rich','research_model_yes')]:
        by_market=defaultdict(list)
        for r in rows:
            p=origins[r['forecast_id']].get(key)
            if p is None: continue
            p=min(1-1e-9,max(1e-9,float(p))); y=r['actual']
            by_market[r['market_id']].append(((p-y)**2,-y*math.log(p)-(1-y)*math.log(1-p)))
        scores[name]={'contracts':len(by_market),'rows':sum(map(len,by_market.values())),
                      'brier':statistics.fmean(statistics.fmean(x[0] for x in v) for v in by_market.values()) if by_market else None,
                      'log_loss':statistics.fmean(statistics.fmean(x[1] for x in v) for v in by_market.values()) if by_market else None}
    sha=json.loads((run_root/'control/runtime_identity.json').read_text())['runtime_sha']
    horizons=defaultdict(list)
    for r in observation_rows(journal_rows(durable_root/'external_fair/pm_lead_lag.jsonl',manifest)):
        if r.get('model_sha')==sha and r.get('realized_horizon_ms') is not None:
            horizons[int(r['horizon_ms'])].append(float(r['realized_horizon_ms']))
    ledger=list(stream(run_root/'ledger/execution.jsonl',manifest))
    maker=[r for r in ledger if (r.get('metadata') or {}).get('component')=='professional_maker']
    examples=order_examples(maker)
    orders=[r for r in maker if r['event_type']=='ORDER_SUBMITTED']
    final_pnl=defaultdict(lambda:{'trades':0,'net_pnl':0.0})
    for r in ledger:
        if r.get('event_type')=='FINAL':
            key=(r.get('metadata') or {}).get('component') or r.get('strategy','UNKNOWN')
            final_pnl[key]['trades']+=1
            final_pnl[key]['net_pnl']+=float(r.get('final_pnl') or 0)
    return {'timestamp_ms':time.time_ns()//1_000_000,'runtime_sha':sha,'frozen_model_hash':model_hash,
            'forward_boundary_ms':boundary,'scores':scores,'exclusions':exclusions,
            'new_feature_forward_contracts':len({r['market_id'] for r in rows if r['features'].get('return_100ms_bp') is not None}),
            'lead_lag':{h:{'labels':len(v),'median_realized_ms':statistics.median(v),
                          'old_250ms_tolerance_eligible':sum(h<=x<=h+250 for x in v),
                          'new_50ms_tolerance_eligible':sum(horizon_eligible(h,x) for x in v)} for h,v in horizons.items()},
            'maker':{'orders':len(orders),'valid_cells_after_legacy_recovery':sum(exact_execution_cell(o) is not None for o in orders),
                     'feature_examples':sum(r['features'] is not None for r in examples),
                     'outcomes':dict(Counter(r['execution_outcome'] for r in examples))},
            'final_pnl_by_component':dict(final_pnl),'source_prefixes':manifest}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-root',type=Path,required=True)
    parser.add_argument('--durable-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=audit(args.run_root,args.durable_root)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as out: json.dump(result,out,indent=2,sort_keys=True); out.write('\n')
    print(json.dumps({k:v for k,v in result.items() if k!='source_prefixes'},indent=2))


if __name__=='__main__': main()
