"""Explicit first-fit research harness for the three EXISTING model definitions.

Authorized separately from replay. No changes to production training, features,
model registry or promotion gates. Historical label availability uses the public
resolution timestamp, with retrieval time retained separately (a retrospective
assumption, not proof of an archived public response available then).
"""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import gzip
import json
from pathlib import Path
import time
import urllib.request

from research.backtest.data import digest
from research.backtest.run import split,write_new
from research.learning.dataset import native_example
from research.learning.models import Candidate,FAMILIES
from research.learning.validation import metrics


def timestamp(value):
    if not value:return None
    try:return int(datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()*1e9)
    except ValueError:return None


def settlement(raw, observed_ns):
    parse=lambda x:json.loads(x) if isinstance(x,str) else x
    if raw.get('closed') is not True or raw.get('umaResolutionStatus')!='resolved':return None
    tokens=parse(raw.get('clobTokenIds'));values=list(map(float,parse(raw.get('outcomePrices'))))
    if len(tokens)!=2 or len(set(tokens))!=2 or sorted(values)!=[0.,1.]:return None
    ended=timestamp(raw.get('endDate'));closed=timestamp(raw.get('closedTime'));resolved=timestamp(raw.get('umaEndDate'))
    if not all(x and x<=observed_ns for x in (ended,closed,resolved)) or min(closed,resolved)<ended:return None
    return dict(market=str(raw['id']),tokens=dict(zip(tokens,values)),ts=max(closed,resolved)/1e6,
                information_ns=max(closed,resolved),retrieved_ns=observed_ns,
                availability_assumption='PUBLIC_REPORTED_RESOLUTION_TIME_RETROSPECTIVE_NOT_ARCHIVED_RECEIVE_TIME')


def collect(markets, root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    def fetch(market):
        path=root/(market+'.json')
        if path.exists():record=json.loads(path.read_text())
        else:
            request=urllib.request.Request('https://gamma-api.polymarket.com/markets/'+market,
                headers={'User-Agent':'Polymarket-paper-backtest/1'})
            try:
                with urllib.request.urlopen(request,timeout=20) as f:raw=json.load(f)
                record=dict(observed_ns=time.time_ns(),public_response=raw)
                write_new(path,record)
            except (OSError,ValueError):return market,None
        raw=record['public_response']
        if str(raw.get('id'))!=market:raise ValueError('settlement_market_mismatch')
        return market,settlement(raw,record['observed_ns'])
    with ThreadPoolExecutor(max_workers=6) as pool:return dict(pool.map(fetch,sorted(markets)))


def prepare(data_path, output):
    import numpy as np
    from threadpoolctl import threadpool_limits
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if (output/'models_frozen.json').exists():raise ValueError('models_already_frozen_no_refit')
    with gzip.open(data_path,'rt') as f:data=json.load(f)
    parts,splits=split(data['opportunities']);cut=int(splits['boundaries_ms']['validation']*1e6)
    labels=collect({o['market'] for o in data['opportunities']},output/'public_settlements')
    examples={};excluded=Counter()
    for o in data['opportunities']:
        raw=dict(o['model_input']);raw['model_sha']=raw.get('model_sha') or raw.get('code_sha')
        try:r=native_example(raw)
        except ValueError as e:excluded[str(e)]+=1;continue
        label=labels.get(o['market']);side=next(iter(o['sides'].values()));token=side['settlement_token']
        if label and token in label['tokens'] and label['ts']>=o['end']-1:
            r.update(outcome=label['tokens'][token],label_information_ns=label['information_ns'])
            o['settlement']=dict(tokens={b['token']:label['tokens'][b['settlement_token']] for b in o['sides'].values()},ts=label['ts'])
        examples[o['id']]=r
    # Five-second embargo on resolved labels; whole-market splits already purge overlap.
    train=[examples[o['id']] for o in parts['old_history'] if o['id'] in examples and
           examples[o['id']].get('outcome') in (0,1) and examples[o['id']]['label_information_ns']<cut-5_000_000_000]
    validation=[examples[o['id']] for o in parts['validation'] if o['id'] in examples and
                examples[o['id']].get('outcome') in (0,1) and
                examples[o['id']]['label_information_ns']<int(splits['boundaries_ms']['test']*1e6)-5_000_000_000]
    if len({r['market_id'] for r in train})<10 or len({r['market_id'] for r in validation})<5:
        raise ValueError('insufficient_resolved_train_or_validation_markets')
    # Keep exactly three existing families. No hyperparameter/calibration search.
    models={};scores={};failures={}
    with threadpool_limits(limits=1):
        for family in FAMILIES:
            try:
                model=Candidate(family,ridge=8.,weighting='market').fit(train,cut)
                prediction=model.predict(validation)
                scores[family]=metrics(validation,prediction);models[family]=model
            except (ValueError,RuntimeError) as e:failures[family]=str(e)
    if 'pm' not in scores or len(models)<2:raise ValueError('first_fit_failed:'+str(failures))
    winner=min(scores,key=lambda family:(scores[family]['log_loss'],family))
    frozen=dict(model_families=list(FAMILIES),winning_family=winner,validation_scores=scores,failures=failures,
        selection_rule='MARKET_WEIGHTED_VALIDATION_LOG_LOSS_THEN_TRADING_PARAMETERS_BY_VALIDATION_PNL',
        fixed_ridge=8.,fixed_weighting='market',calibration='raw',fit_wall_ns=time.time_ns(),
        simulated_fit_cutoff_ns=cut,split=splits,train_rows=len(train),validation_rows=len(validation),
        train_markets=len({r['market_id'] for r in train}),
        validation_markets=len({r['market_id'] for r in validation}),
        train_contexts=dict(Counter(r['asset']+':'+r['horizon'] for r in train)),
        label_timing_assumption='Public closedTime/umaEndDate; fetched later. Not archived availability proof.',
        retrospective_research_only=True,promotion_eligible=False,
        parameters={family:model.parameters() for family,model in models.items()})
    # Freeze selection and parameters before inference on the final period.
    write_new(output/'models_frozen.json',frozen)
    winner_hash=digest(frozen['parameters'][winner]);model=models[winner]
    for key in ('validation','test'):
        selected=[o for o in parts[key] if o['id'] in examples]
        if not selected:continue
        predicted=model.predict([examples[o['id']] for o in selected])
        for o,p in zip(selected,predicted):
            # Existing native dataset predicts the selected token's settlement.
            o['p']=float(p) if o['direction']>0 else 1-float(p)
            o['model_hash']=winner_hash
    data.update(model_hash=winner_hash,model_description='Existing '+winner+'; first fit on today old-history partition; frozen before TEST.',
                fitted_model_receipt=digest(frozen),model_fit_summary={k:v for k,v in frozen.items() if k!='parameters'})
    data['exclusions']['feature_adapter_exclusions']=dict(excluded)
    path=output/'predictions.json.gz'
    with gzip.open(path,'wt') as f:json.dump(data,f,separators=(',', ':'),allow_nan=False)
    path.chmod(0o600)
    print(json.dumps({k:v for k,v in frozen.items() if k not in ('parameters','split')},indent=2))
    return path


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    prepare(a.data,a.output)
