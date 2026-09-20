"""Small adapter for today's existing native captures. No fitting or new features."""
from __future__ import annotations
from bisect import bisect_left
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path

SAFETY = dict(paper_only=True, authenticated_execution=False, real_order_submission=False)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()


def number(x):
    return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)


def decision(r):
    """Preserve logged probability, token direction and the actual information clock."""
    if r.get('kind')!=2 or r.get('signal_valid') is not True or r.get('confirmed_non_opposing') is not True:
        raise ValueError('not_valid_confirmed_signal')
    if r.get('paper_only') is not True or r.get('execution_authority') is not False:
        raise ValueError('unsafe_authority')
    dm,wall,trigger,received,close,closewall,grid=(r.get(k) for k in (
        'decision_monotonic_ns','decision_wall_ns','trigger_monotonic_ns','receive_monotonic_ns',
        'close_monotonic_ns','close_wall_ns','evaluated_grid_monotonic_ns'))
    if (not all(number(x) and x>0 for x in (dm,wall,trigger,received,close,closewall,grid))
            or max(trigger,received,grid)>dm or close<=dm or
            abs((wall-dm)-(closewall-close))>1_000_000):
        raise ValueError('unproven_causal_clock')
    direction=r.get('direction');bid,ask=r.get('bid_e4'),r.get('ask_e4')
    if direction not in (-1,1) or r.get('book_valid') is not True or not 0<bid<ask<10000:
        raise ValueError('invalid_book_or_direction')
    if dm-received>100_000_000:raise ValueError('stale_decision_book')
    rate,exponent,minimum,tick=(r.get(k) for k in ('fee_rate','fee_exponent','minimum_order_microunits','tick_e4'))
    if (not all(number(x) and x>=0 for x in (rate,exponent,minimum,tick)) or min(minimum,tick)<=0
            or not r.get('paper_terms_sha256') or not r.get('fee_source')):
        raise ValueError('missing_fee_or_order_terms')
    key='|'.join(str(r[k]) for k in ('server_id','run_id','capture_id'))
    token=str(r['token_id']);side='YES' if direction>0 else 'NO'
    p=r.get('probability_forecast');model=r.get('probability_artifact_sha256')
    if p is not None and (not number(p) or not 0<=p<=1 or not model or r.get('probability_input_token_id')!=token):
        raise ValueError('invalid_forecast_identity')
    ext=r.get('external_features') or {}
    if number(ext.get('input_receive_ns')) and ext['input_receive_ns']>dm:raise ValueError('future_external_input')
    return dict(id=digest([key,r['signal_version'],trigger]),market=str(r['market_id']),
        asset=r['asset'],horizon=r['horizon'],ts=wall/1e6,end=closewall/1e6,tte=(close-dm)/1e9,
        p=p,model_hash=model,signal_age_ms=(dm-trigger)/1e6,direction=direction,
        sides={side:dict(token=key+'|'+token,settlement_token=token,ask=ask/10000,bid=bid/10000,
                        minimum=minimum/1e6,tick=tick/10000)},
        fee_rate=rate,fee_exponent=exponent,venue_delay_ms=r.get('paper_venue_delay_ns',0)/1e6,
        external_return_bp=r.get('binance_return_100ms_bp'),strong_signal=True,
        capture_mode=r.get('capture_mode'),settlement=None,model_input=r)


def snapshot(r):
    if r.get('kind') not in (1,3,5,6):return None
    wall,close,observed,received=(r.get(k) for k in ('close_wall_ns','close_monotonic_ns','observed_monotonic_ns','receive_monotonic_ns'))
    if not all(number(x) and x>0 for x in (wall,close,observed,received)):return None
    token='|'.join(str(r[k]) for k in ('server_id','run_id','capture_id','token_id'))
    bid,ask,qty=(r.get(k) for k in ('bid_e4','ask_e4','ask_quantity'))
    valid=(r.get('paper_only') is True and r.get('execution_authority') is False and
           r.get('book_valid') is True and all(number(x) for x in (bid,ask,qty)) and
           0<bid<ask<10000 and qty>=0 and 0<=observed-received<=100_000_000 and r['kind']!=5)
    return dict(market=str(r['market_id']),token=token,ts=(wall-close+observed)/1e6,
                bid=bid/10000 if number(bid) else None,ask=ask/10000 if number(ask) else None,
                qty=qty/1e6 if number(qty) else None,valid=valid,epoch=r.get('connection_epoch'))


def read_prefix(path, length):
    """Freeze the observed file prefix; live writers may append safely."""
    with path.open('rb') as f:
        left=length
        while left:
            line=f.readline(left);left-=len(line)
            if not line:break
            if not line.endswith(b'\n'):break
            yield line


def build(root, start_ms, end_ms, output, *, delete_old=False):
    root=Path(root);output=Path(output);origins={};books={};counts=Counter();contexts=Counter();sources=[]
    for path in sorted((root/'research/native_observations').rglob('*.jsonl')):
        if path.is_symlink():continue
        before=path.stat();size=before.st_size;sha=hashlib.sha256();local=[];tape=defaultdict(list);latest=0;clock_complete=True
        for line in read_prefix(path,size):
            sha.update(line)
            try:r=json.loads(line)
            except ValueError:counts['invalid_json']+=1;clock_complete=False;continue
            if r.get('schema')!='polymarket_v7_native_observation_v1':continue
            wall=r.get('decision_wall_ns') if r.get('kind')==2 else None
            if not wall:
                wall=(r.get('close_wall_ns') or 0)-(r.get('close_monotonic_ns') or 0)+(r.get('observed_monotonic_ns') or 0)
            if wall<=0:clock_complete=False
            latest=max(latest,wall/1e6)
            if not start_ms<=wall/1e6<=end_ms:continue
            counts['native_rows']+=1;contexts[r['asset']+':'+r['horizon']]+=1
            if r.get('kind')==2:
                counts['decisions']+=1;counts['reason_'+str(r.get('reason'))]+=1
                if r.get('probability_forecast') is not None:counts['decisions_with_forecast']+=1
                try:o=decision(r)
                except ValueError as e:counts[str(e)]+=1;continue
                if o['id'] not in origins:
                    origins[o['id']]=o;local.append(o)
            b=snapshot(r)
            if b:tape[b['token']].append(b)
        closed=path.with_name(path.name+'.closed.json')
        if delete_old and clock_complete and 0<latest<start_ms and closed.is_file():
            receipt=json.loads(closed.read_text());after=path.stat()
            if (receipt.get('closed') is True and receipt.get('healthy') is True and
                    (before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns)):
                path.unlink();closed.unlink();counts['old_capture_files_deleted']+=1;counts['old_capture_bytes_deleted']+=size
        if not local:continue
        sources.append(dict(path=str(path),prefix_bytes=size,sha256=sha.hexdigest()))
        # Keep only actual post-target observations. No interpolation/predecision fills.
        for token,seq in tape.items():
            seq.sort(key=lambda b:b['ts']);times=[b['ts'] for b in seq]
            for o in local:
                if not any(b['token']==token for b in o['sides'].values()):continue
                for delay in (50,100,250,500,1000,2000):
                    # Execution adds the recorded venue delay; lead-lag does not.
                    for extra in (0,o['venue_delay_ms']):
                        target=o['ts']+delay+extra;i=bisect_left(times,target)
                        if i<len(seq) and times[i]-target<=50:
                            b=seq[i];books[digest(b)]=b
    obs=sorted(origins.values(),key=lambda o:(o['ts'],o['id']))
    # Reuse public settlement labels if already collected; no unresolved-as-zero.
    label_root=root/'research/public_settlements'
    if label_root.exists():
        for path in label_root.rglob('*.json'):
            try:v=json.loads(path.read_text())
            except (ValueError,OSError):continue
            if v.get('schema')!='v7_public_settlement_evidence_v1' or v.get('closed') is not True or v.get('resolution_status')!='resolved':continue
            labels=v.get('token_outcomes') or {}
            for o in obs:
                if str(v.get('market_id'))==o['market'] and all(b['settlement_token'] in labels for b in o['sides'].values()):
                    o['settlement']=dict(tokens={b['token']:labels[b['settlement_token']] for b in o['sides'].values()},ts=v['information_ns']/1e6)
    hashes=sorted({o['model_hash'] for o in obs if o['model_hash']})
    if len(hashes)>1:raise ValueError('multiple_models_require_explicit_frozen_model_selection')
    result=dict(schema='simple_paper_backtest_data_v1',**SAFETY,model_hash=hashes[0] if hashes else None,
        model_description='Existing recorded native settlement model; unavailable where forecast is null.',
        start_ms=start_ms,end_ms=end_ms,sources=sources,exclusions=dict(counts),context_rows=dict(contexts),
        opportunities=obs,books=sorted(books.values(),key=lambda b:(b['ts'],b['token'])))
    output.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(output,'wt') as f:json.dump(result,f,separators=(',', ':'),allow_nan=False)
    output.chmod(0o600)
    return dict(opportunities=len(obs),markets=len({o['market'] for o in obs}),books=len(books),
                model_hash=result['model_hash'],counts=dict(counts),context_rows=dict(contexts))
