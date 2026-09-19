#!/usr/bin/env python3
"""Read-only, frozen-ledger cross-asset diagnostics. Never submits orders.

Hypothetical limit-price payoffs are not executable counterfactual profits.
Unresolved markets, ambiguous joins and unknown fees remain missing.
"""
from __future__ import annotations
import argparse, bisect, collections, concurrent.futures, gzip, hashlib, json
import math, random, statistics, time, urllib.request
from pathlib import Path

PAPER_REASONS = {1:'FILLED',3:'BOOK_UNAVAILABLE',4:'NOT_MARKETABLE',
    10:'ARRIVAL_CENSORED',11:'VENUE_TERMS_UNKNOWN',12:'PRICE_IMPROVEMENT_UNMODELLED',
    13:'PARTIAL_FILL_UNMODELLED',14:'DEPTH_ACCOUNTING_UNAVAILABLE',15:'PARTIAL_FILL_MODELLED'}

def load_rows(path: Path):
    seen=set()
    for line in path.open():
        r=json.loads(line); key=r.get('record_id')
        if not key or key in seen: raise ValueError('missing/duplicate record identity')
        seen.add(key); yield r

def observation_near(path: Path, target_ms: int, token: str, book_version: int):
    """Bounded byte bisection on monotone owner observation time, not event time."""
    opener=gzip.open if path.name.endswith('.gz') else open
    with opener(path,'rb') as f:
        first=json.loads(f.readline())
        offset=int(first['close_wall_ns'])-int(first['close_monotonic_ns'])
        target=target_ms*1_000_000-offset; radius=10_000_000
        if path.name.endswith('.gz'):
            if first.get('capture_mode')=='FULL': return []  # No unbounded inflate.
            f.seek(0)
        else:
            low,high=0,path.stat().st_size
            while high-low>65536:
                mid=(low+high)//2; f.seek(mid);f.readline();pos=f.tell();raw=f.readline()
                if not raw: high=mid;continue
                try: point=json.loads(raw)
                except json.JSONDecodeError: high=mid;continue
                if int(point.get('observed_monotonic_ns',0))<target-radius: low=f.tell()
                else: high=pos
            f.seek(low)
        hits=[]
        for raw in f:
            try:r=json.loads(raw)
            except json.JSONDecodeError:continue  # Active trailing partial line only.
            stamp=int(r.get('observed_monotonic_ns',0))
            if stamp>target+radius:break
            if stamp<target-radius or r.get('kind')!=2 or not r.get('accepted'):continue
            if str(r.get('token_id'))!=token or int(r.get('book_version',0))!=book_version:continue
            hits.append(r)
        return hits

def public_resolution(market: str, cache: Path):
    p=cache/(market+'.json')
    if p.exists(): return json.loads(p.read_text())
    url='https://gamma-api.polymarket.com/markets/'+market
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Polymarket-research-audit/1.0'})
        with urllib.request.urlopen(req,timeout=5) as f: raw=json.load(f)
        tokens=raw.get('clobTokenIds'); prices=raw.get('outcomePrices'); outcomes=raw.get('outcomes')
        if isinstance(tokens,str):tokens=json.loads(tokens)
        if isinstance(prices,str):prices=json.loads(prices)
        if isinstance(outcomes,str):outcomes=json.loads(outcomes)
        values=[float(x) for x in (prices or [])]
        resolved=(raw.get('closed') is True and isinstance(tokens,list) and len(tokens)==2
            and len(set(tokens))==2 and len(values)==2 and (sorted(values)==[0.0,1.0] or values==[0.5,0.5]))
        result={'market_id':market,'source':url,'observed_at_unix':time.time(),
          'resolved':resolved,'payouts':dict(zip(tokens,values)) if resolved else None,
          'outcomes':dict(zip(tokens,outcomes)) if tokens and outcomes else None,
          'end_date':raw.get('endDate'),'resolution_status':raw.get('umaResolutionStatus')}
        if resolved:p.write_text(json.dumps(result,sort_keys=True))
        return result
    except (OSError,ValueError,TypeError) as exc:
        return {'market_id':market,'resolved':False,'error':type(exc).__name__}

def summarize(rows):
    settled=[r for r in rows if r.get('realized_pnl') is not None]
    ps=[r['realized_pnl'] for r in settled]
    wins=sum(p>0 for p in ps); fees=sum(r.get('paid_fees',0) for r in settled)
    return {'submitted':len(rows),'fills':sum(r['filled'] for r in rows),'settled':len(ps),
      'pnl':sum(ps),'fees':fees,'gross_pnl':sum(ps)+fees,
      'win_rate':wins/len(ps) if ps else None,
      'mean_entry':statistics.mean(r['price'] for r in settled) if settled else None,
      'join_count':sum(r.get('feature_join')=='UNIQUE' for r in rows),
      'state_reasons':dict(collections.Counter(r['reason'] for r in rows)),
      'median_signal_age_ms':statistics.median([r['signal_age_ms'] for r in rows if r.get('signal_age_ms') is not None]) if any(r.get('signal_age_ms') is not None for r in rows) else None,
      'median_shock_bp':statistics.median([abs(r['binance_return_bp']) for r in rows if r.get('binance_return_bp') is not None]) if any(r.get('binance_return_bp') is not None for r in rows) else None}

def build(snapshot: Path, root: Path, output: Path, fetch_public: bool):
    output.mkdir(parents=True,exist_ok=True);cache=output/'resolutions';cache.mkdir(exist_ok=True)
    events=list(load_rows(snapshot));subs={}; fills=collections.defaultdict(list);states={};finals={};known={}
    for e in events:
        md=e.get('metadata') or {};kind=e['event_type'];oid=e.get('order_id')
        if kind=='ORDER_SUBMITTED':subs[oid]=e
        elif kind=='ORDER_STATE':states[oid]=e
        elif kind=='FILL':fills[oid].append(e)
        elif kind=='FINAL':
            ids=md.get('included_order_ids') or [oid]
            if len(ids)!=1:raise ValueError('multi-order terminal requires cashflow allocation')
            if ids[0] in finals:raise ValueError('duplicate terminal allocation')
            finals[ids[0]]=e
            payouts=md.get('settlement_payouts')
            if payouts:known[str(e['market_id'])]={'resolved':True,'payouts':payouts,'source':'canonical_FINAL'}
    missing=sorted({str(e['market_id']) for e in subs.values()}-known.keys())
    if fetch_public:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for market,res in zip(missing,pool.map(lambda m:public_resolution(m,cache),missing)):known[market]=res
    file_index=collections.defaultdict(list)
    for p in (root/'research/native_observations').glob('*/*'):
        if p.name.endswith(('.jsonl','.jsonl.gz')):
            file_index[(p.parent.name,p.name.split('-')[0])].append(p)
    rows=[]; feature_fields=['binance_return_100ms_bp','coinbase_return_100ms_bp','confirmation_return_100ms_bp','confirmation_venue','signal_age_ns','tte_ns','bid_e4','ask_e4','bid_quantity','ask_quantity','direction','fee_rate','fee_exponent','close_wall_ns']
    for oid,s in subs.items():
        md=s.get('metadata') or {};state=states.get(oid,{})
        reason=int((state.get('metadata') or {}).get('paper_execution_reason') or 0)
        f=fills.get(oid,[]);fin=finals.get(oid);market=str(s['market_id']);token=str(s['token_id'])
        price=float(s['limit_price']);q=float(s['intended_size']);hits=[]
        book=int((s.get('book_snapshot_id') or '0').split(':')[-1])
        for p in file_index.get((md.get('run_id'),market),[]):
            hits.extend(observation_near(p,int(s['decision_ts_ms']),token,book))
        r={'order_id':oid,'market_id':market,'asset':md.get('asset'),'horizon':md.get('horizon'),
           'run_id':md.get('run_id'),'decision_ts_ms':s['decision_ts_ms'],'price':price,'size':q,
           'filled':bool(f),'paid_fees':sum(float(x.get('fee') or 0) for x in f),
           'realized_pnl':float(fin['final_pnl']) if fin else None,
           'reason':PAPER_REASONS.get(reason,str(reason)),'censored':bool((state.get('metadata') or {}).get('execution_observation_censored')),
           'feature_join':'UNIQUE' if len(hits)==1 else 'MISSING' if not hits else 'AMBIGUOUS',
           'hypothetical_payoff_at_limit':None,'hypothetical_net_at_limit':None,
           'counterfactual_execution_verified':False,'signal_age_ms':None,'binance_return_bp':None}
        if len(hits)==1:
            h=hits[0];r['features']={k:h.get(k) for k in feature_fields}
            r['signal_age_ms']=h['signal_age_ns']/1e6;r['binance_return_bp']=h['binance_return_100ms_bp']
            # Post-switch observations may describe a chosen token different
            # from the forecast-input token. Do not silently train the legacy
            # feature recipe on a mismatched target/book pair.
            if h.get('probability_forecast') is not None and h.get('probability_input_token_id') != token:
                r['feature_join']='MODEL_INPUT_SIDE_MISMATCH_REQUIRES_EXPLICIT_ADAPTER'
            r['tte_seconds']=h['tte_ns']/1e9;r['close_ts_ms']=h['close_wall_ns']//1_000_000
            r['spread']= (h['ask_e4']-h['bid_e4'])/10000
        label=known.get(market,{})
        if label.get('resolved') and token in (label.get('payouts') or {}):
            y=float(label['payouts'][token]);r['selected_outcome']=y
            r['hypothetical_payoff_at_limit']=q*(y-price)
            # A numeric fee field is not evidence of authoritative venue terms.
            # In particular reason 11 explicitly says those terms were unknown.
            if len(hits)==1 and reason != 11:
                rate=hits[0].get('fee_rate'); exponent=hits[0].get('fee_exponent')
                if isinstance(rate,(int,float)) and isinstance(exponent,(int,float)) and math.isfinite(rate) and math.isfinite(exponent) and 0<=rate<=1 and exponent>0:
                    fee=q*rate*(price*(1-price))**exponent
                    r['hypothetical_net_at_limit']=q*(y-price)-fee
        else:r['selected_outcome']=None
        rows.append(r)
    byasset={a:summarize([r for r in rows if r['asset']==a]) for a in ['BTC','ETH','SOL','XRP','DOGE','BNB']}
    bycontext={a+':'+h:summarize([r for r in rows if r['asset']==a and r['horizon']==h]) for a,h in sorted({(r['asset'],r['horizon']) for r in rows})}
    groups=collections.defaultdict(list)
    for r in rows:
        if r['hypothetical_net_at_limit'] is not None:groups[r['reason']].append(r)
    counterfactual={k:{'n':len(v),'markets':len({r['market_id'] for r in v}),
        'hypothetical_net_sum':sum(r['hypothetical_net_at_limit'] for r in v),
        'mean_selected_payoff':statistics.mean(r['selected_outcome'] for r in v),
        'mean_price':statistics.mean(r['price'] for r in v)} for k,v in groups.items()}
    report={'schema':'v7_crypto_cross_asset_audit_v1','snapshot_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest(),
      'generated_unix':time.time(),'model_sha':next(iter(subs.values()))['model_sha'],
      'records':len(events),'overall':summarize(rows),'by_asset':byasset,'by_context':bycontext,
      'labelled_orders':sum(r['selected_outcome'] is not None for r in rows),'unique_markets':len({r['market_id'] for r in rows}),
      'nonfill_payoff_diagnostic':counterfactual,
      'nonfill_warning':'Not attainable profits; ignores fill eligibility and mutually exclusive retry/capital paths.',
      'feature_join_contract':'UNIQUE_RUN_MARKET_TOKEN_BOOK_OWNER_TIME_WITHIN_10MS',
      'no_assets_disabled':True,'paper_only':True,'production_modified':False}
    (output/'orders.json').write_text(json.dumps(rows,separators=(',',':'),allow_nan=False))
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
    return report

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--snapshot',type=Path,required=True);ap.add_argument('--run-root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--fetch-public',action='store_true');a=ap.parse_args()
    print(json.dumps(build(a.snapshot,a.run_root,a.output,a.fetch_public),sort_keys=True))
