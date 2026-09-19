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



def _decision_book_buy_cost(row: dict, quantity_shares: float) -> tuple[float, float] | None:
    """Cost a full BUY against visible decision-time asks.

    This is deliberately a decision-book counterfactual, not an arrival fill.
    It is used only for TTE-window hypothesis diagnostics.
    """
    minimum = int(row.get('minimum_order_microunits') or 0)
    target = int(round(float(quantity_shares) * 1_000_000))
    if minimum <= 0 or target < minimum:
        return None
    rate = row.get('fee_rate'); exponent = row.get('fee_exponent')
    if not isinstance(rate,(int,float)) or not isinstance(exponent,(int,float)):
        return None
    asks = row.get('asks') or []
    if not asks and int(row.get('ask_e4') or 0) > 0:
        asks = [(int(row['ask_e4']), int(row.get('ask_quantity') or 0))]
    remaining = target
    cash = 0.0
    fee = 0.0
    for price_e4, quantity_microunits in asks:
        price_e4 = int(price_e4); available = int(quantity_microunits)
        if price_e4 <= 0 or available <= 0:
            continue
        take = min(remaining, available)
        price = price_e4 / 10_000.0
        shares = take / 1_000_000.0
        cash += price * shares
        fee += float(rate) * shares * ((price * (1.0 - price)) ** float(exponent))
        remaining -= take
        if remaining <= 0:
            break
    return None if remaining > 0 else (cash, fee)


def tte_window_diagnostics(points: list[dict], ledger: list[dict], protocol: dict) -> dict:
    """Evaluate preregistered TTE windows without granting promotion authority.

    Only decision rows whose canonical reason is Accepted (1) or
    TteOutsideWindow (6) are admissible.  Therefore the selector changes TTE
    only; other known decision blockers are never reinterpreted as trades.
    Reason-6 occurs before book admission in the native lane, so visible depth,
    minimum order metadata and fee metadata are checked again here.

    Settlement labels currently come from canonical FINAL rows.  If labels are
    absent for markets that were observed but not traded, the result is
    explicitly selection-conditioned and hypothesis-generating.
    """
    quantity = float(min(protocol.get('quantity_shares') or [5]))
    labels: dict[str,str] = {}
    canonical_pnl: dict[str,float] = {}
    conflicts: set[str] = set()
    for row in ledger:
        if row.get('event_type') != 'FINAL':
            continue
        market = str(row.get('market_id') or '')
        metadata = row.get('metadata') if isinstance(row.get('metadata'),dict) else {}
        winning = str(metadata.get('winning_token_id') or '')
        if not market or not winning:
            continue
        previous = labels.get(market)
        if previous is not None and previous != winning:
            conflicts.add(market)
            continue
        labels[market] = winning
        value = row.get('final_pnl')
        if isinstance(value,(int,float)):
            canonical_pnl[market] = float(value)
    for market in conflicts:
        labels.pop(market,None); canonical_pnl.pop(market,None)

    eligible: dict[str,list[dict]] = defaultdict(list)
    observed_markets: set[str] = set()
    for row in points:
        market = str(row.get('market_id') or '')
        if not market:
            continue
        observed_markets.add(market)
        if int(row.get('reason') or 0) not in (1,6):
            continue
        if row.get('book_valid') is not True:
            continue
        if row.get('confirmed_non_opposing') is not True:
            continue
        if _decision_book_buy_cost(row,quantity) is None:
            continue
        wall = int(row.get('decision_wall_ns') or 0)
        mono = int(row.get('decision_monotonic_ns') or 0)
        if wall <= 0 and mono <= 0:
            continue
        eligible[market].append(row)
    for rows in eligible.values():
        rows.sort(key=lambda r:(int(r.get('decision_wall_ns') or 0),
                                str(r.get('_capture') or ''),
                                int(r.get('decision_monotonic_ns') or 0)))

    scenarios=[]
    for window in protocol.get('tte_windows_seconds') or []:
        if not isinstance(window,list) or len(window) != 2:
            continue
        lower,upper = float(window[0]),float(window[1])
        trades=[]
        for market,winning in labels.items():
            candidate = next((row for row in eligible.get(market,[])
                              if lower <= float(row.get('tte_ns') or 0)/1e9 <= upper),None)
            if candidate is None:
                continue
            cost = _decision_book_buy_cost(candidate,quantity)
            if cost is None:
                continue
            cash,fee = cost
            payout = quantity if str(candidate.get('token_id') or '') == winning else 0.0
            trades.append({
                'market_id':market,
                'net_pnl':payout-cash-fee,
                'won':payout > 0.0,
                'reason':int(candidate.get('reason') or 0),
                'tte_seconds':float(candidate.get('tte_ns') or 0)/1e9,
                'signal_age_ms':float(candidate.get('signal_age_ns') or 0)/1e6,
            })
        total=sum(row['net_pnl'] for row in trades)
        scenarios.append({
            'window_seconds':[lower,upper],
            'markets':len(trades),
            'wins':sum(row['won'] for row in trades),
            'net_pnl_usd':total if trades else None,
            'mean_net_pnl_usd':total/len(trades) if trades else None,
            'decision_book_execution_only':True,
            'trades':trades,
        })

    labeled_observed = observed_markets & set(labels)
    coverage = len(labeled_observed)/len(observed_markets) if observed_markets else 0.0
    accepted=[]
    for market,winning in labels.items():
        row=next((r for r in eligible.get(market,[]) if int(r.get('reason') or 0)==1),None)
        if row is None:
            continue
        cost=_decision_book_buy_cost(row,quantity)
        if cost is None:
            continue
        cash,fee=cost
        payout=quantity if str(row.get('token_id') or '')==winning else 0.0
        accepted.append((market,payout-cash-fee))
    baseline_decision_pnl=sum(value for _,value in accepted) if accepted else None
    baseline_canonical_pnl=sum(canonical_pnl[m] for m,_ in accepted if m in canonical_pnl) if accepted else None

    if conflicts:
        state='INCONCLUSIVE'
        reason='SETTLEMENT_LABEL_CONFLICT'
    elif not observed_markets:
        state='INCONCLUSIVE'
        reason='NO_NATIVE_DECISION_MARKETS'
    elif not labels:
        state='INCONCLUSIVE'
        reason='NO_SETTLEMENT_LABELS'
    elif coverage < 0.999999:
        state='HYPOTHESIS_GENERATING_SELECTION_CONDITIONED'
        reason='SETTLEMENT_LABELS_NOT_AVAILABLE_FOR_ALL_OBSERVED_MARKETS'
    else:
        state='COUNTERFACTUAL_DIAGNOSTICS_AVAILABLE'
        reason=''
    return {
        'state':state,
        'reason':reason,
        'quantity_shares':quantity,
        'observed_markets':len(observed_markets),
        'settlement_labeled_observed_markets':len(labeled_observed),
        'settlement_label_coverage':coverage,
        'settlement_label_conflicts':sorted(conflicts),
        'selection_conditioned_on_available_settlement_labels':coverage < 0.999999,
        'baseline_window_seconds':protocol.get('baseline_tte_window_seconds'),
        'baseline_accepted_decision_book_pnl_usd':baseline_decision_pnl,
        'baseline_accepted_canonical_pnl_usd':baseline_canonical_pnl,
        'scenarios':scenarios,
        'execution_semantics':'DECISION_TIME_VISIBLE_L10_FULL_SIZE_WITH_CANONICAL_FEE_NO_ARRIVAL_CLAIM',
        'automatic_promotion':False,
        'real_money_authorized':False,
        'limits':'Not a held-out profit proof. Missing settlement labels are never imputed; decision-book execution is not arrival execution.',
    }


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
    tte={'state':'INCONCLUSIVE','reason':'NATIVE_TAPES_NOT_IN_DATASET','scenarios':[]}
    if any('native_observations' in r['path'] for r in manifest['files']):
        tape,points,native=read_native(root,manifest)
        arrival=arrival_diagnostics(tape,points,protocol)
        tte=tte_window_diagnostics(points,ledger,protocol)
        tests['T3']['execution_diagnostics_available']=arrival['state']=='EXECUTION_DIAGNOSTICS_ONLY'
        tests['T3']['missing_required']=['heldout_economic_outcomes','sufficient_time_blocks','exchange_latency_validation']
        tests['T6']['diagnostics_available']=tte['state']!='INCONCLUSIVE'
        tests['T6']['selection_conditioned']=tte.get('selection_conditioned_on_available_settlement_labels',True)
        tests['T6']['missing_required']=(['prospective_all_market_settlement_labels','arrival_adjusted_execution','heldout_confirmation']
            if tte.get('selection_conditioned_on_available_settlement_labels',True)
            else ['arrival_adjusted_execution','heldout_confirmation'])
    return {'schema':'polymarket_economic_research_report_v1','protocol_id':protocol['protocol_id'],
        'protocol_sha256':_canonical_hash(protocol),'dataset_sha256':manifest['dataset_sha256'],
        'identity':manifest['identity'],'paper_only':True,'execution_authority':False,'automatic_promotion':False,
        'state':'INCONCLUSIVE','code_deployment_authorized':False,
        'ledger':{'event_counts':dict(Counter(r.get('event_type') for r in ledger)),
            'aggregate_final_count':len(finals),'attribution_view_count':len(final_views),
            'observed_realized_paper_pnl':observed_pnl,'open_positions':len(fill_positions-closed_positions),
            'projection_errors':errors,'observed_hour_blocks':len(canonical_by_block),
            'minimum_time_blocks':protocol['minimum_time_blocks'],'profitability_proven':False},
        'native':native,'arrival_diagnostics':arrival,'tte_window_diagnostics':tte,'tests':tests,
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
