#!/usr/bin/env python3
"""Run reproducible diagnostics on a verified frozen dataset; never trade.

Usage: python research/economic/run.py --dataset DIR --output NEW_REPORT.json
The frozen live ledger alone is insufficient for signal/latency causal claims.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from statistics import mean
from typing import Any

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));sys.path.insert(0,str(HERE.parents[1]/'scripts'))
from evidence import verify
from inference import paired_block_report
from causal_replay import Book,BookTape,Order,CausalReplay,EvidenceError
from v7_native_settlement_projection import iter_position_economics


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def read_native(root: Path, manifest: dict) -> tuple[BookTape,list[dict],dict]:
    books=[];points=[];watermarks={};gaps=defaultdict(list);errors=[];last={};epochs={}
    closed={}
    for file in manifest['files']:
        if file['path'].endswith('.closed.json'):
            record=json.loads((root/file['path']).read_text())
            if record.get('schema')=='polymarket_v7_native_capture_closed_v1':
                key=(record['server_id'],record['run_id'],record['capture_id'])
                closed[key]=record
    for file in manifest['files']:
        if not file['path'].endswith('.jsonl') or 'native_observations' not in file['path']:continue
        with (root/file['path']).open() as handle:
            for line in handle:
                row=json.loads(line)
                if (row.get('schema')!='polymarket_v7_native_observation_v1'
                        or row.get('paper_only') is not True or row.get('execution_authority') is not False):
                    raise EvidenceError('native observation contract')
                key=(row['server_id'],row['run_id'],row['capture_id'])
                capture='|'.join(key);row['_capture']=capture
                seq=int(row['sequence']);available=int(row['observed_monotonic_ns'])
                if key in last and seq!=last[key][0]+1:
                    errors.append('capture_sequence_gap:'+capture)
                    gaps[capture].append((last[key][1],available))
                if key in last and available<last[key][1]:raise EvidenceError('observation clock reversed')
                if key in epochs and epochs[key]!=row.get('connection_epoch'):
                    gaps[capture].append((last[key][1],available))
                if row.get('kind') == 5:
                    gaps[capture].append((last.get(key, (0, available))[1], available))
                epochs[key]=row.get('connection_epoch');last[key]=(seq,available)
                if key in closed and closed[key].get('healthy') is True:
                    watermarks[capture]=int(closed[key]['watermark_monotonic_ns'])
                valid=bool(row.get('book_valid'))
                bids=tuple((int(p),int(q)) for p,q in row.get('bids',[]))
                asks=tuple((int(p),int(q)) for p,q in row.get('asks',[]))
                if not bids and int(row.get('bid_e4',0))>0:bids=((int(row['bid_e4']),int(row['bid_quantity'])),)
                if not asks and int(row.get('ask_e4',0))>0:asks=((int(row['ask_e4']),int(row['ask_quantity'])),)
                if row['kind'] in (1,3,5) and row.get('token_id') and row.get('tick_e4',0)>0:
                    books.append(Book(str(row['market_id']),row['token_id'],capture,available,int(row['book_version']),bids,asks,int(row['tick_e4']),valid,int(row['receive_monotonic_ns'])))
                if row['kind']==2:points.append(row)
    for key,(seq,_) in last.items():
        if key not in closed:errors.append('capture_not_closed:'+'|'.join(key))
        elif closed[key].get('last_sequence')!=seq:errors.append('capture_count_mismatch:'+'|'.join(key))
    return BookTape.from_books(books,watermarks,dict(gaps)),points,{'books':len(books),'decisions':len(points),'closed_captures':len(closed),'errors':errors}


def arrival_diagnostics(tape:BookTape,points:list[dict],protocol:dict) -> dict:
    """Replay accepted candidate decisions; not a counterfactual profit estimate."""
    accepted=sorted([p for p in points if p.get('accepted') is True],key=lambda r:r['decision_monotonic_ns'])
    if not accepted:return {'state':'INCONCLUSIVE','reason':'NO_ACCEPTED_NATIVE_DECISIONS','scenarios':[]}
    scenarios=[]
    for delay in protocol['delay_ms']:
        for quantity in protocol['quantity_shares']:
            results=[];contexts={}
            for idx,row in enumerate(accepted):
                key=(row['_capture'],row['fee_rate'],row['fee_exponent'])
                # Monotonic clocks are comparable only inside a single capture.
                sim=contexts.setdefault(key,CausalReplay(tape,rate=row['fee_rate'],exponent=row['fee_exponent'],
                    max_book_age_ns=int(protocol['maximum_book_age_ms']*1e6)))
                minimum = int(row.get('minimum_order_microunits') or 0)
                if minimum <= 0:
                    results.append({'status':'CENSORED','average_price':None,'reason':'minimum_order_metadata_missing'})
                    continue
                mandatory = row.get('paper_venue_delay_ns')
                if type(mandatory) is not int or mandatory < 0 or not row.get('paper_terms_sha256'):
                    results.append({'status':'CENSORED','average_price':None,'reason':'mandatory_delay_metadata_unknown'})
                    continue
                order=Order(str(idx),str(row['market_id']),row['token_id'],row['_capture'],
                    int(row['decision_monotonic_ns']),'BUY',int(row['ask_e4']),int(quantity*1e6),minimum,int(delay*1e6)+mandatory,True)
                result=sim.execute(order);results.append(asdict(result))
            scenarios.append({'delay_ms':delay,'delay_semantics':'ASSUMED_TRANSPORT_PLUS_MARKET_MANDATORY','quantity_shares':quantity,'counts':dict(Counter(r['status'] for r in results)),
                'mean_filled_price':mean([r['average_price'] for r in results if r['average_price'] is not None]) if any(r['average_price'] is not None for r in results) else None,
                'execution_only_no_profit_claim':True,'orders':results})
    return {'state':'EXECUTION_DIAGNOSTICS_ONLY','accepted_candidates':len(accepted),'scenarios':scenarios,
        'limits':'Historical accepted candidates only; alternative selector/window requires full policy replay and venue minimum metadata. The effective minimum is read from each native observation; missing minimum metadata is censored.'}


def analyze(root:Path,protocol:dict) -> dict:
    manifest=verify(root);ledger=[];errors=[]
    for file in manifest['files']:
        if file['path'].endswith('ledger/execution.jsonl'):
            ledger.extend(json.loads(x) for x in (root/file['path']).read_text().splitlines() if x.strip())
    views=list(iter_position_economics(ledger,errors))
    finals=[r for r in ledger if r.get('event_type')=='FINAL']
    final_views=[r for r in views if r.get('event_type')=='FINAL']
    fill_positions={r.get('position_id') for r in ledger if r.get('event_type')=='FILL'}
    closed_positions={r.get('position_id') for r in final_views}
    observed_pnl=sum(float(r['final_pnl']) for r in finals) if finals else None
    canonical_by_block=defaultdict(float)
    for row in finals:
        block=str(int(row['recorded_ts_ms'])//(protocol['time_block_seconds']*1000))
        canonical_by_block[block]+=float(row['final_pnl'])
    tests={k:{'name':v['name'],'state':'INCONCLUSIVE','missing_required':v['requires']} for k,v in protocol['tests'].items()}
    native={'books':0,'decisions':0,'errors':[]};arrival={'state':'INCONCLUSIVE','reason':'NATIVE_TAPES_NOT_IN_DATASET'}
    if any('native_observations' in r['path'] for r in manifest['files']):
        tape,points,native=read_native(root,manifest)
        arrival=arrival_diagnostics(tape,points,protocol)
        tests['T3']['execution_diagnostics_available']=arrival['state']=='EXECUTION_DIAGNOSTICS_ONLY'
        tests['T3']['missing_required']=['heldout_economic_outcomes','sufficient_time_blocks','exchange_latency_validation']
    return {'schema':'polymarket_economic_research_report_v1','protocol_id':protocol['protocol_id'],
        'protocol_sha256':_canonical_hash(protocol),'dataset_sha256':manifest['dataset_sha256'],
        'identity':manifest['identity'],'paper_only':True,'execution_authority':False,'automatic_promotion':False,
        'state':'INCONCLUSIVE','code_deployment_authorized':False,
        'ledger':{'event_counts':dict(Counter(r.get('event_type') for r in ledger)),
            'aggregate_final_count':len(finals),'attribution_view_count':len(final_views),
            'observed_realized_paper_pnl':observed_pnl,'open_positions':len(fill_positions-closed_positions),
            'projection_errors':errors,'observed_hour_blocks':len(canonical_by_block),
            'minimum_time_blocks':protocol['minimum_time_blocks'],'profitability_proven':False},
        'native':native,'arrival_diagnostics':arrival,'tests':tests,
        'promotion_blockers':['PROSPECTIVE_HELDOUT_COMPARISON_NOT_COMPLETE','VARIABLE_COST_AND_ARRIVAL_PARITY_REQUIRED',
            *(['FIXED_COSTS_NOT_SUPPLIED'] if protocol['fixed_cost_usd_per_hour'] is None else []),
            *(['ACCOUNTING_INVALID'] if errors else [])],
        'note':'Accounting reconciliation and executable replay tests do not prove the signal profitable. No absent data or future outcome was imputed.'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--protocol',type=Path,default=HERE/'protocol.json')
    a=p.parse_args();protocol=json.loads(a.protocol.read_text())
    if a.output.exists():raise SystemExit('report destination exists; preserve prior runs')
    report=analyze(a.dataset,protocol);a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'state':report['state'],'ledger':report['ledger'],'native':report['native'],'output':str(a.output)},indent=2))
    return 0 if not report['ledger']['projection_errors'] else 2


if __name__=='__main__':raise SystemExit(main())
