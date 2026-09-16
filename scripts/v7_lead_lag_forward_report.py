#!/usr/bin/env python3
"""Evaluate the frozen LEAD_LAG_TAKER_V1 PAPER forward test.

This report grants no authority and never promotes execution. Statistical units
are independent market settlements. PnL is canonical hold-to-settlement PnL
inclusive of the recorded taker fee.
"""
from __future__ import annotations
import argparse, json, math, os, random, statistics, time
from pathlib import Path
from typing import Any
from v7_execution_ledger import iter_events

SCHEMA='polymarket_v7_lead_lag_taker_v1_forward_report'
FAMILY='lead_lag_taker_v1'

def load(path: Path) -> dict[str,Any]:
    try: x=json.loads(path.read_text())
    except (OSError,json.JSONDecodeError): return {}
    return x if isinstance(x,dict) else {}

def atomic_json(path: Path, value: dict[str,Any]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f'.tmp.{os.getpid()}')
    tmp.write_text(json.dumps(value,indent=2,sort_keys=True)+'\n'); os.replace(tmp,path)

def percentile(values:list[float],q:float)->float|None:
    if not values:return None
    xs=sorted(values); pos=(len(xs)-1)*q; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    return xs[lo] if lo==hi else xs[lo]+(xs[hi]-xs[lo])*(pos-lo)

def bootstrap_mean_ci(values:list[float],draws:int=10000,seed:int=17)->dict[str,Any]:
    if not values:return {'draws':draws,'mean':None,'ci95':[None,None]}
    rng=random.Random(seed); n=len(values); means=[]
    for _ in range(draws): means.append(statistics.fmean(values[rng.randrange(n)] for _ in range(n)))
    return {'draws':draws,'mean':statistics.fmean(values),'ci95':[percentile(means,.025),percentile(means,.975)]}

def summarize(events:list[Any],manifest:dict[str,Any],status:dict[str,Any],*,draws:int=10000)->dict[str,Any]:
    protocol=str(manifest.get('protocol_hash') or '')
    rows=[]
    for event in events:
        md=event.metadata if isinstance(event.metadata,dict) else {}
        if md.get('model_family')!=FAMILY or md.get('protocol_hash')!=protocol: continue
        rows.append(event)
    orders={str(e.order_id):e for e in rows if e.event_type=='ORDER_SUBMITTED' and e.order_id}
    fills={str(e.fill_id):e for e in rows if e.event_type=='FILL' and e.fill_id}
    finals={str(e.market_id):e for e in rows if e.event_type=='FINAL' and e.market_id}
    terminal=[]
    for market,final in finals.items():
        fill=next((x for x in fills.values() if str(x.market_id)==market),None)
        if fill is None: continue
        shares=float(fill.filled_size or 0.0); pnl=float(final.final_pnl or 0.0)
        if shares<=0: continue
        terminal.append({'market_id':market,'recorded_ts_ms':int(final.recorded_ts_ms),'shares':shares,
                         'entry_price':float(fill.fill_price or 0.0),'fee':float(fill.fee or 0.0),
                         'pnl':pnl,'pnl_per_share':pnl/shares,'won':bool((final.metadata or {}).get('won'))})
    terminal.sort(key=lambda x:(x['recorded_ts_ms'],x['market_id']))
    pnlps=[x['pnl_per_share'] for x in terminal]; pnl=[x['pnl'] for x in terminal]
    block_size=25; blocks=[]
    for i in range(0,len(terminal),block_size):
        b=terminal[i:i+block_size]
        blocks.append({'block':i//block_size+1,'markets':len(b),'complete':len(b)==block_size,
                       'pnl':sum(x['pnl'] for x in b),'pnl_per_share_mean':statistics.fmean(x['pnl_per_share'] for x in b) if b else None})
    complete_blocks=[b for b in blocks if b['complete']]; positive_blocks=sum(b['pnl']>0 for b in complete_blocks)
    boot=bootstrap_mean_ci(pnlps,draws=draws)
    gross_positive=sum(max(0.0,x) for x in pnl)
    largest_positive=max([max(0.0,x) for x in pnl],default=0.0)
    concentration=largest_positive/gross_positive if gross_positive>0 else None
    target=int(manifest.get('target_independent_markets') or 100)
    min_windows=int(manifest.get('minimum_positive_windows_before_canary') or 3)
    ci_lower=boot['ci95'][0]
    gate=(len(terminal)>=target and sum(pnl)>0 and ci_lower is not None and ci_lower>0
          and positive_blocks>=min_windows)
    return {
      'schema':SCHEMA,'generated_ms':time.time_ns()//1_000_000,
      'strategy_id':'LEAD_LAG_TAKER_V1','code_sha':manifest.get('code_sha'),'protocol_hash':protocol,
      'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'real_capital_at_risk':False,
      'automatic_promotion':False,'statistical_unit':'INDEPENDENT_SETTLED_MARKET',
      'target_independent_markets':target,'submitted_orders':len(orders),'fills':len(fills),'settled_markets':len(terminal),
      'wins':sum(x['won'] for x in terminal),'win_rate':sum(x['won'] for x in terminal)/len(terminal) if terminal else None,
      'total_pnl':sum(pnl),'mean_pnl_per_share':statistics.fmean(pnlps) if pnlps else None,
      'median_pnl_per_share':statistics.median(pnlps) if pnlps else None,'bootstrap_mean_pnl_per_share':boot,
      'chronological_25_market_blocks':blocks,'complete_blocks':len(complete_blocks),'positive_complete_blocks':positive_blocks,
      'largest_positive_trade_fraction_of_gross_positive_pnl':concentration,
      'runtime_entries':int(status.get('entries') or 0),'runtime_settled':int(status.get('settled') or 0),
      'arrival_rejections':int(status.get('arrival_rejections') or 0),'skip_reasons':status.get('skip_reasons') or {},
      'research_gate':{'ready_for_manual_canary_review':gate,'minimum_markets_met':len(terminal)>=target,
        'total_pnl_positive':sum(pnl)>0,'bootstrap_ci_lower_positive':ci_lower is not None and ci_lower>0,
        'minimum_positive_complete_blocks':min_windows,'positive_complete_blocks':positive_blocks,
        'note':'Manual research gate only. It cannot enable real order submission.'},
      'terminal_market_rows':terminal,
    }

def main()->int:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('--run-root',type=Path,required=True)
    ap.add_argument('--model-sha',required=True); ap.add_argument('--manifest',type=Path); ap.add_argument('--output',type=Path)
    ap.add_argument('--bootstrap-draws',type=int,default=10000); args=ap.parse_args()
    root=args.run_root.resolve(); manifest=args.manifest or root/'research/lead_lag_taker_v1/forward_manifest.json'
    output=args.output or root/'research/lead_lag_taker_v1/forward_report.json'; mf=load(manifest); st=load(root/'research/lead_lag_taker_v1/status.json')
    if mf.get('code_sha')!=args.model_sha or mf.get('paper_only') is not True or mf.get('automatic_promotion') is not False:
        raise SystemExit('forward manifest identity/safety invalid')
    events=list(iter_events(root/'ledger/execution.jsonl',expected_model_sha=args.model_sha))
    report=summarize(events,mf,st,draws=max(1000,args.bootstrap_draws)); atomic_json(output,report); print(json.dumps(report,indent=2,sort_keys=True)); return 0
if __name__=='__main__': raise SystemExit(main())
