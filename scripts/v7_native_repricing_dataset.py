#!/usr/bin/env python3
"""Build causal native PM repricing labels from bounded decision windows.

No execution authority. Only complete two-token pairs with continuous native
capture become labels. Missing/gapped horizons remain censored, never zero.
"""
from __future__ import annotations
import argparse,gzip,json,math
from pathlib import Path
from typing import Any,Iterable

SCHEMA='polymarket_v7_native_repricing_label_v1'
ORIGIN_REASONS={1,15,16,17}  # accepted or probability/edge/size gated after causal signal checks
HORIZONS={100,250,500,1000}

def _lines(path:Path)->Iterable[dict[str,Any]]:
    op=gzip.open if path.name.endswith('.gz') else open
    with op(path,'rt',encoding='utf-8') as f:
        for line in f:
            if not line.endswith('\n'): continue
            value=json.loads(line)
            if isinstance(value,dict): yield value

def _probability(row:dict[str,Any])->float|None:
    if row.get('repricing_pair_valid') is not True:return None
    try:
        yb,ya,nb,na=(int(row[k]) for k in ('yes_bid_e4','yes_ask_e4','no_bid_e4','no_ask_e4'))
        tick=int(row.get('tick_e4') or 0)
    except (KeyError,TypeError,ValueError,OverflowError):return None
    if not (0<yb<ya<10000 and 0<nb<na<10000 and 0<tick<10000):return None
    yes=(yb+ya)/20000.0; no=(nb+na)/20000.0
    if abs(yes+no-1.0)>2*tick/10000.0+1e-9:return None
    p=(yes+1.0-no)/2.0
    return p if 0<p<1 else None

def _logit(p:float)->float:return math.log(p/(1-p))

def build(paths:list[Path])->tuple[list[dict[str,Any]],dict[str,Any]]:
    origins:{}={}; labels:{}={}; conflicts=0; invalid=0
    for path in paths:
        for row in _lines(path):
            if row.get('schema')!='polymarket_v7_native_observation_v1' or row.get('paper_only') is not True or row.get('execution_authority') is not False:
                invalid+=1;continue
            run=str(row.get('run_id') or '');market=str(row.get('market_id') or '')
            try: origin_version=int(row.get('repricing_origin_signal_version') or 0);kind=int(row.get('kind') or 0)
            except (TypeError,ValueError,OverflowError):invalid+=1;continue
            if not run or not market or origin_version<=0:continue
            key=(run,market,origin_version)
            if kind==2 and int(row.get('reason') or 0) in ORIGIN_REASONS:
                if key in origins and origins[key]!=row:conflicts+=1;continue
                origins[key]=row
            elif kind==6:
                try:h=int(row.get('repricing_horizon_ms') or 0)
                except (TypeError,ValueError,OverflowError):invalid+=1;continue
                if h not in HORIZONS:invalid+=1;continue
                lk=(*key,h)
                if lk in labels and labels[lk]!=row:conflicts+=1;continue
                labels[lk]=row
    if conflicts:raise ValueError(f'conflicting repricing identities:{conflicts}')
    out=[];censored=0
    for key,origin in sorted(origins.items(),key=lambda kv:(int(kv[1].get('decision_wall_ns') or 0),kv[0])):
        p0=_probability(origin)
        if p0 is None:censored+=len(HORIZONS);continue
        try:decision_ns=int(origin['decision_monotonic_ns'])
        except (KeyError,TypeError,ValueError):censored+=len(HORIZONS);continue
        for h in sorted(HORIZONS):
            label=labels.get((*key,h));p1=_probability(label) if label else None
            if label is None or p1 is None:
                censored+=1;continue
            try:observed_ns=int(label['observed_monotonic_ns'])
            except (KeyError,TypeError,ValueError):censored+=1;continue
            if observed_ns < decision_ns+h*1_000_000:
                censored+=1;continue
            external=origin.get('external_features') if isinstance(origin.get('external_features'),dict) else {}
            out.append({
                'schema':SCHEMA,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
                'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','model_sha':origin.get('code_sha'),
                'run_id':key[0],'market_id':key[1],'origin_signal_version':key[2],
                'asset':origin.get('asset'),'horizon':origin.get('horizon'),'repricing_horizon_ms':h,
                'decision_wall_ns':origin.get('decision_wall_ns'),'label_observed_monotonic_ns':observed_ns,
                'origin_pm_yes':p0,'label_pm_yes':p1,'delta_probability':p1-p0,
                'delta_logit':_logit(p1)-_logit(p0),
                'binance_return_100ms_bp':origin.get('binance_return_100ms_bp'),
                'confirmation_return_100ms_bp':origin.get('confirmation_return_100ms_bp'),
                'confirmation_venue':origin.get('confirmation_venue'),'signal_age_ns':origin.get('signal_age_ns'),
                'tte_ns':origin.get('tte_ns'),'external_features':external,
                'origin_pair':{k:origin.get(k) for k in ('yes_bid_e4','yes_ask_e4','no_bid_e4','no_ask_e4')},
                'label_pair':{k:label.get(k) for k in ('yes_bid_e4','yes_ask_e4','no_bid_e4','no_ask_e4')},
                'target_semantics':'CAUSAL_PM_BOOK_ASOF_NOMINAL_HORIZON_RECEIVE_TIME',
            })
    summary={'schema':'polymarket_v7_native_repricing_dataset_summary_v1','origins':len(origins),
        'labels':len(out),'censored_horizons':censored,'invalid_records':invalid,'conflicts':conflicts,
        'horizons_ms':sorted(HORIZONS),'paper_only':True}
    return out,summary

def main()->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,action='append',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--summary',type=Path,required=True);a=p.parse_args()
    rows,summary=build(a.input);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(''.join(json.dumps(r,separators=(',',':'),allow_nan=False)+'\n' for r in rows))
    a.summary.write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
