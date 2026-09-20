"""Validation-only trading parameters, immutable freeze, then one test evaluation.

Offline PAPER research. Uses recorded predictions; no training, registry writes,
network calls or order submission. A trade means one attempted order per market.
"""
from __future__ import annotations
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
import gzip
import hashlib
import json
import math
from pathlib import Path

from research.backtest.data import SAFETY, digest
from research.economic.causal_replay import cash_fee

LATENCIES = (50,100,250,500)
MAX_BOOK_WAIT_MS = 50
EXECUTION_RESERVE = .005


@dataclass(frozen=True)
class Parameters:
    edge: float = .02
    tte_min: int = 105
    tte_max: int = 120
    signal_age_ms: int = 100
    entry_cap: float = .75
    shares: float = 5.


def grid():
    """Small prespecified grid; no model or feature tuning."""
    base = Parameters()
    values = [base]
    for field, options in dict(edge=(.005,.01,.02,.03,.04), tte_min=(30,60,90,105),
        signal_age_ms=(50,100,250,500),entry_cap=(.65,.70,.75,.80),shares=(5.,10.,20.)).items():
        values += [replace(base, **{field:v}) for v in options]
    # Broad age variant is prespecified, never added after looking at TEST.
    values += [replace(base, edge=e,tte_min=t,signal_age_ms=500)
               for e in (.005,.01,.02,.04) for t in (30,60,90,105)]
    return list(dict.fromkeys(values))


class Tape:
    def __init__(self, books):
        grouped = defaultdict(lambda: defaultdict(list))
        for b in books: grouped[b['token']][b['ts']].append(b)
        self.books={};self.times={}
        for token, timestamps in grouped.items():
            sequence=[]
            for ts, obs in sorted(timestamps.items()):
                b=dict(obs[0])
                # Clock granularity cannot order conflicting snapshots safely.
                if any((x['market'],x['bid'],x['ask']) != (b['market'],b['bid'],b['ask']) or not x['valid'] for x in obs):
                    b['valid']=False
                b['qty']=min((x['qty'] for x in obs if x['qty'] is not None),default=0)
                sequence.append(b)
            self.books[token]=sequence;self.times[token]=[b['ts'] for b in sequence]

    def after(self, token, ts, market):
        times=self.times.get(token,[]); i=bisect_left(times,ts)
        if i==len(times) or times[i]-ts > MAX_BOOK_WAIT_MS:
            return None, 'post_latency_book_unavailable'
        b=self.books[token][i]
        if not b['valid'] or b['market']!=market: return None,'post_latency_book_invalid'
        return b, None


def split(rows):
    """Whole markets, chronological 50/25/25; boundary-spanning rows purged."""
    starts={}
    for r in rows: starts[r['market']]=min(starts.get(r['market'],r['ts']),r['ts'])
    markets=sorted(starts,key=lambda m:(starts[m],m)); n=len(markets)
    if n<4: raise ValueError('at_least_four_markets_required')
    a=max(1,n//2); b=max(a+1,3*n//4)
    groups=dict(old_history=markets[:a],validation=markets[a:b],test=markets[b:])
    boundaries={k:min(starts[m] for m in v) for k,v in groups.items()}
    out={};purged=0
    for key,ids in groups.items():
        upper=boundaries['validation'] if key=='old_history' else boundaries['test'] if key=='validation' else math.inf
        out[key]=[r for r in rows if r['market'] in ids and r['end'] <= upper]
        purged+=sum(r['market'] in ids and r['end']>upper for r in rows)
    return out, dict(markets=groups,boundaries_ms=boundaries,purged_opportunities=purged)


def per_share_fee(o, price):
    return cash_fee(1_000_000,round(price*10000),o['fee_rate'],o['fee_exponent'])


def replay(rows, tape, params, latency):
    if latency<0: raise ValueError('negative_latency')
    out=[];used=set();reserve=0.
    for o in sorted(rows,key=lambda x:(x['ts'],x['id'])):
        if (o.get('p') is None or o['market'] in used or not params.tte_min<=o['tte']<=params.tte_max
                or not 0<=o['signal_age_ms']<=params.signal_age_ms): continue
        choices=[]
        for side,b in o['sides'].items():
            p=o['p'] if side=='YES' else 1-o['p'];ask=b['ask']
            edge=p-ask-per_share_fee(o,ask)-EXECUTION_RESERVE
            if edge>=params.edge and ask<=params.entry_cap:
                choices.append((edge,side,p,b))
        if not choices: continue
        _,side,p,b=max(choices,key=lambda v:(v[0],v[1]))
        # Reuse the prepared forward protocol's two-tick bounded chase rule.
        tick=b.get('tick',.01);limit=b['ask']
        for step in (1,2):
            candidate=round(b['ask']+step*tick,4)
            if candidate>params.entry_cap or p-candidate-per_share_fee(o,candidate)-EXECUTION_RESERVE<params.edge:break
            limit=candidate
        qty=math.floor(min(params.shares,20.,(3.75-.00001)/(limit+per_share_fee(o,limit)+EXECUTION_RESERVE))*1e6)/1e6
        if qty < b['minimum'] or reserve+3.75>1000: continue
        used.add(o['market']);reserve+=3.75  # no reuse of unseen/censored capital
        r=dict(id=o['id'],market=o['market'],asset=o['asset'],horizon=o['horizon'],tte=o['tte'],
               tte_bucket=('105–120' if o['tte']>=105 else '90–105' if o['tte']>=90 else '60–90' if o['tte']>=60 else '30–60'),
               ts=o['ts'],side=side,decision_ask=b['ask'],limit=limit,requested=qty,
               filled=0.,fees=0.,turnover=0.,pnl=None,deterioration=None,status='UNAVAILABLE')
        arrival,why=tape.after(b['token'],o['ts']+latency,o['market'])
        if why: r['reason']=why;out.append(r);continue
        if arrival.get('epoch') != (o.get('model_input') or {}).get('connection_epoch'):
            r['reason']='capture_epoch_changed';out.append(r);continue
        if arrival['ts']>=o['end']:
            r['reason']='market_expired';out.append(r);continue
        r['execution_ts']=arrival['ts'];r['effective_latency_ms']=arrival['ts']-o['ts']
        r['deterioration']=arrival['ask']-b['ask']
        filled=math.floor(min(qty,arrival['qty'])*1e6)/1e6 if arrival['ask']<=limit+1e-9 else 0.
        if not filled:
            r.update(status='NO_FILL',pnl=0.,pnl_ts=arrival['ts']);out.append(r);continue
        fee=cash_fee(round(filled*1e6),round(arrival['ask']*10000),o['fee_rate'],o['fee_exponent'])
        r.update(filled=filled,fees=fee,turnover=filled*arrival['ask'],
                 status='PARTIAL_FILL' if filled<qty-1e-6 else 'FILLED')
        label=o.get('settlement')
        if label:
            r.update(pnl=filled*label['tokens'][b['token']]-r['turnover']-fee,pnl_ts=label['ts'])
        else: r['reason']='settlement_unavailable'
        out.append(r)
    return out


def metrics(trades, opportunities):
    known=[r for r in trades if r['pnl'] is not None];fills=[r for r in trades if r['filled']>0]
    settled=[r for r in fills if r['pnl'] is not None]; observed=[r for r in trades if r['status']!='UNAVAILABLE']
    pnl=sum(r['pnl'] for r in known); curve=[];equity=peak=drawdown=0.
    # Settlement-realized PnL; not intramarket mark-to-market drawdown.
    events=defaultdict(float)
    for r in known:events[r['pnl_ts']]+=r['pnl']
    for ts,value in sorted(events.items()):
        equity+=value;peak=max(peak,equity);drawdown=max(drawdown,peak-equity);curve.append([ts,equity])
    deterioration=[r['deterioration'] for r in observed]
    abs_total=sum(abs(r['pnl']) for r in settled)
    return dict(opportunities=opportunities,trades=len(trades),fills=len(fills),settled_fills=len(settled),
        partial_fills=sum(r['status']=='PARTIAL_FILL' for r in trades),unavailable=len(trades)-len(observed),
        pending_settlements=len(fills)-len(settled),fill_rate=len(fills)/len(trades) if trades else None,
        observed_fill_rate=len(fills)/len(observed) if observed else None,
        net_pnl=pnl if len(known)==len(trades) else None,observed_net_pnl=pnl if known else None,
        pnl_per_trade=pnl/len(known) if known else None,pnl_per_settled_fill=pnl/len(settled) if settled else None,
        fees=sum(r['fees'] for r in fills),turnover=sum(r['turnover'] for r in fills),
        max_drawdown=drawdown if known else None,cumulative_pnl=curve,
        average_execution_deterioration=sum(deterioration)/len(deterioration) if deterioration else None,
        average_effective_latency_ms=sum(r['effective_latency_ms'] for r in observed)/len(observed) if observed else None,
        largest_market_abs_pnl_share=max((abs(r['pnl']) for r in settled),default=0)/abs_total if abs_total else None,
        traded_markets=len({r['market'] for r in fills}),
        reasons=dict(Counter(r.get('reason') for r in trades if r.get('reason'))))


def evaluate(rows,tape,params,latency):
    value=metrics(replay(rows,tape,params,latency),len(rows))
    missing=sum(r.get('p') is None for r in rows)
    value['opportunities_without_forecast']=missing
    if missing:
        value['net_pnl']=None
        if missing==len(rows):
            value.update(observed_net_pnl=None,pnl_per_trade=None,max_drawdown=None,
                         status='MODEL_FORECASTS_UNAVAILABLE')
    else:value['status']='EVALUATED'
    return value


def breakdown(trades, opportunities, key):
    return {v:metrics([r for r in trades if r[key]==v],sum(r.get(key)==v for r in opportunities))
            for v in sorted({r[key] for r in trades}|{r[key] for r in opportunities if key in r})}


def lead_lag(rows,tape):
    out=[]
    for asset in sorted({r['asset'] for r in rows}):
        candidates=[r for r in rows if r['asset']==asset and r.get('external_return_bp') is not None and r.get('strong_signal',False)]
        # At most one strong signal per market/second, chosen before future movement.
        seen=set();signals=[]
        for r in sorted(candidates,key=lambda r:r['ts']):
            key=r['market'],int(r['ts']//1000)
            if key not in seen:seen.add(key);signals.append(r)
        for horizon in (100,250,500,1000,2000):
            values=[];markets=set();delays=[]
            for r in signals:
                b,why=tape.after(next(iter(r['sides'].values()))['token'],r['ts']+horizon,r['market'])
                if why or b['ts']>=r['end'] or b.get('epoch') != (r.get('model_input') or {}).get('connection_epoch'):continue
                side=next(iter(r['sides'].values()));before=(side['ask']+side['bid'])/2
                values.append(((b['ask']+b['bid'])/2-before))
                markets.add(r['market']);delays.append(b['ts']-r['ts'])
            out.append(dict(asset=asset,horizon_ms=horizon,strong_signals=len(signals),samples=len(values),
                markets=len(markets),mean_directional_pm_move=sum(values)/len(values) if values else None,
                mean_actual_horizon_ms=sum(delays)/len(delays) if delays else None))
    return out


def write_new(path, value):
    with Path(path).open('x') as f:json.dump(value,f,indent=2,allow_nan=False)
    Path(path).chmod(0o600)


def code_hash():
    return digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))})


def load(path):
    with gzip.open(path,'rt') as f:data=json.load(f)
    if any(data.get(k) is not v for k,v in SAFETY.items()):raise ValueError('unsafe_data')
    return data,hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(data_path,output):
    data,sha=load(data_path); parts,splits=split(data['opportunities']);tape=Tape(data['books'])
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if (output/'freeze.json').exists():raise ValueError('validation_already_frozen_use_existing_result')
    validation_cut=splits['boundaries_ms']['test']
    validation_rows=[dict(o,settlement=o.get('settlement') if o.get('settlement') and o['settlement']['ts']<validation_cut else None) for o in parts['validation']]
    results=[]
    for p in grid():
        m=evaluate(validation_rows,tape,p,100)
        # Five distinct filled markets and full accounting coverage; no one-trade winner.
        eligible=m['settled_fills']>=5 and m['net_pnl'] is not None
        results.append(dict(parameters=asdict(p),metrics=m,selection_eligible=eligible,baseline=p==Parameters()))
    ranked=sorted(results,key=lambda r:(r['selection_eligible'],r['metrics']['net_pnl'] if r['metrics']['net_pnl'] is not None else -math.inf,
        r['metrics']['settled_fills']),reverse=True)
    eligible=[r for r in ranked if r['selection_eligible']]
    winner=eligible[0] if eligible else next(r for r in results if r['baseline'])
    frozen=dict(schema='simple_paper_backtest_freeze_v1',**SAFETY,data_sha256=sha,code_sha256=code_hash(),
        model_hash=data['model_hash'],model_description=data['model_description'],model_fit_summary=data.get('model_fit_summary'),splits=splits,
        baseline=asdict(Parameters()),selected=winner['parameters'],
        selection_status='VALIDATION_SELECTED' if eligible else 'INSUFFICIENT_VALIDATION_SUPPORT_BASELINE_FROZEN',
        selection_minimum_markets=5,primary_latency_ms=100,latencies_ms=list(LATENCIES),
        maximum_book_wait_ms=MAX_BOOK_WAIT_MS,validation_configurations=ranked,
        baseline_note='Prepared forward protocol: .02 EV, .005 execution reserve, two-tick chase; 105–120s, 100ms comparator, .75 entry, 5 shares. Point-probability fixed-size research, no native robust-bound/allocator parity claim.',
        london_latency_ms=None)
    write_new(output/'freeze.json',frozen)
    return {k:v for k,v in frozen.items() if k not in ('validation_configurations','splits')}


def final_test(data_path,output):
    output=Path(output);frozen=json.loads((output/'freeze.json').read_text());data,sha=load(data_path)
    if frozen['data_sha256']!=sha or frozen['code_sha256']!=code_hash():raise ValueError('frozen_data_or_code_changed')
    if (output/'test.json').exists():return {'status':'EXISTING_FINAL_TEST_REUSED','report':str(output/'test.json')}
    parts,splits=split(data['opportunities'])
    if frozen['splits']!=splits:raise ValueError('split_changed')
    # An interrupted evaluation cannot silently become a second untouched test.
    write_new(output/'test_started.json',dict(freeze_sha256=digest(frozen),**SAFETY))
    tape=Tape(data['books']);p=Parameters(**frozen['selected']);sweep={};trades=None
    for latency in frozen['latencies_ms']:
        t=replay(parts['test'],tape,p,latency);sweep[str(latency)]=evaluate(parts['test'],tape,p,latency)
        if latency==frozen['primary_latency_ms']:trades=t
    result=dict(schema='simple_paper_backtest_result_v1',**SAFETY,freeze_sha256=digest(frozen),
        selection_status=frozen['selection_status'],parameters=frozen['selected'],
        final_test=sweep[str(frozen['primary_latency_ms'])],latency=sweep,trades=trades,
        by_asset=breakdown(trades,parts['test'],'asset'),by_horizon=breakdown(trades,parts['test'],'horizon'),
        by_tte=breakdown(trades,[dict(r,tte_bucket=('105–120' if r['tte']>=105 else '90–105' if r['tte']>=90 else '60–90' if r['tte']>=60 else '30–60')) for r in parts['test']],'tte_bucket'),
        lead_lag=lead_lag(parts['test'],tape),data_coverage=dict(opportunities=len(data['opportunities']),
        markets=len({r['market'] for r in data['opportunities']}),book_rows=len(data['books']),
        exclusions=data['exclusions'],context_rows=data.get('context_rows',{}),start_ms=data.get('start_ms'),end_ms=data.get('end_ms'),split_opportunities={k:len(v) for k,v in parts.items()}))
    write_new(output/'test.json',result)
    return result['final_test']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('phase',choices=('validate','test','report'))
    parser.add_argument('--data',required=True);parser.add_argument('--output',required=True);a=parser.parse_args()
    if a.phase=='report':
        from research.backtest.report import render
        root=Path(a.output);render(root,json.loads((root/'freeze.json').read_text()),json.loads((root/'test.json').read_text()))
    else:print(json.dumps((validate if a.phase=='validate' else final_test)(a.data,a.output),indent=2))
