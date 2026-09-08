#!/usr/bin/env python3
"""Read-only position attribution; canonical cash truth, no execution authority."""
from __future__ import annotations
import argparse
import csv
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from v7_maker_durable_learning import order_examples, placement_features

SCHEMA = 'polymarket_v7_profit_attribution_v1'
ZERO = Decimal(0)


def dec(value):
    if value is None or isinstance(value, bool): return None
    try: result = Decimal(str(value))
    except (InvalidOperation, ValueError): return None
    return result if result.is_finite() else None


def metadata(row):
    return row.get('metadata') if isinstance(row.get('metadata'), dict) else {}


def read_sources(paths):
    """Freeze complete source prefixes, including decompressed archival checkpoints."""
    records, sources = {}, []
    for path in paths:
        path = Path(path)
        if path.is_symlink() or not path.is_file(): raise ValueError('unsafe ledger source')
        digest = hashlib.sha256(); count = size = 0
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rb') as stream:
            limit = None if path.suffix == '.gz' else os.fstat(stream.fileno()).st_size
            while limit is None or stream.tell() < limit:
                line = stream.readline() if limit is None else stream.readline(limit-stream.tell())
                if not line or not line.endswith(b'\n'): break
                digest.update(line); size += len(line); count += 1
                if not line.strip(): continue
                row = json.loads(line)
                if not isinstance(row, dict) or row.get('paper_only') is not True or row.get('authenticated_execution') is not False:
                    raise ValueError('noncanonical PAPER ledger source')
                if row.get('real_order_submission') is True: raise ValueError('real execution evidence')
                sha, identity = str(row.get('model_sha') or ''), str(row.get('record_id') or '')
                if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha) or not identity:
                    raise ValueError('missing exact ledger identity')
                key = sha, identity
                if key in records and records[key] != row: raise ValueError('conflicting canonical record: '+identity)
                records[key] = row
        sources.append({'path':str(path),'decompressed_prefix_bytes':size,'complete_lines':count,'sha256':digest.hexdigest()})
    return list(records.values()), sources


def decision_evidence(order, fill):
    m = metadata(order) or metadata(fill)
    envelope = m.get('opportunity_envelope') or {}
    model = envelope.get('settlement_model') or {}
    probability = dec(m.get('decision_point_probability'))
    source = 'EXPLICIT_DECISION_TOKEN_PROBABILITY'
    if probability is None and dec(m.get('fair_yes')) is not None and m.get('outcome') in {'YES','NO'}:
        probability = dec(m['fair_yes']) if m['outcome']=='YES' else 1-dec(m['fair_yes'])
        source = 'EXPLICIT_LEGACY_DECISION_FAIR_YES_CONVERTED_TO_TOKEN'
    if probability is None:
        probability = dec(m.get('point_probability')); source = 'LEGACY_DECISION_TOKEN_PROBABILITY'
    if probability is None:
        probability = dec((envelope.get('fair_value') or {}).get('point')); source = 'AUTHORIZED_ENVELOPE_TOKEN_PROBABILITY'
    if probability is not None and not ZERO <= probability <= 1: probability = None
    return {'probability':probability,'probability_source':source if probability is not None else None,
        'model_id':model.get('model_id') or m.get('decision_probability_model_id') or m.get('probability_model_id'),
        'model_hash':model.get('model_hash') or m.get('decision_probability_model_hash') or m.get('probability_model_hash'),
        'model_stage_explicit':bool(model or m.get('decision_probability_model_id')),
        'decision_price':dec(m.get('decision_limit_price')) if not envelope else dec(((envelope.get('execution_plan') or {}).get('legs') or [{}])[0].get('limit_price')),
        'decision_timestamp_ms':m.get('decision_observed_ts_ms') if not envelope else (envelope.get('decision_receive_timestamp_ns') or 0)//1000000,
        'arrival_probability':dec(m.get('arrival_point_probability')),
        'arrival_price':dec(m.get('arrival_best_ask')),
        'arrival_timestamp_ms':m.get('arrival_receive_ts_ms'),
        'arrival_fee_per_share':dec(m.get('arrival_fee_per_share')),
        'arrival_execution_risk_per_share':dec(m.get('arrival_execution_risk_per_share'))}


def position_row(final, fills, orders, markouts, examples):
    m = metadata(final); reasons=[]
    position = str(final.get('position_id') or '')
    result = {'code_sha':final['model_sha'],'position_id':position,'market_id':final.get('market_id'),
        'event_id':final.get('event_id'),'token_id':final.get('token_id'),
        'component':m.get('component') or m.get('model_family') or final.get('strategy'),
        'paper_probe':m.get('paper_bootstrap_probe'), 'ledger_final_pnl':dec(final.get('final_pnl')),
        'final_record_id':final.get('record_id'),'final_timestamp_ms':final.get('recorded_ts_ms'),
        'fill_ids':[f.get('fill_id') for f in fills], 'missing_or_inconsistent':reasons}
    if not fills: reasons.append('MISSING_POSITION_FILLS')
    if any(f.get('token_id') != final.get('token_id') or f.get('side') != 'BUY' for f in fills):
        reasons.append('NOT_A_SINGLE_TOKEN_BUY_POSITION')
    if final.get('bundle_id'): reasons.append('MULTILEG_UNIT_REQUIRES_LEG_ATTRIBUTION')
    if 'NOT_A_SINGLE_TOKEN_BUY_POSITION' in reasons or final.get('bundle_id'): return result
    quantities=[dec(f.get('filled_size')) for f in fills]; prices=[dec(f.get('fill_price')) for f in fills]
    if not fills or any(q is None or q <= 0 for q in quantities) or any(p is None or not 0 < p < 1 for p in prices):
        reasons.append('MISSING_OR_INVALID_FILL_ECONOMICS'); return result
    quantity=sum(quantities,ZERO); notional=sum((q*p for q,p in zip(quantities,prices)),ZERO)
    entry_fees=[dec(f.get('fee')) for f in fills]
    if any(f is None for f in entry_fees): reasons.append('MISSING_ENTRY_FEES')
    fees=sum((f for f in entry_fees if f is not None),ZERO)
    debit=dec(m.get('entry_debit'))
    if debit is None: reasons.append('MISSING_CANONICAL_ENTRY_DEBIT')
    payout=dec(m.get('settlement_payout',final.get('realized_cashflow')))
    winner=str(m.get('winning_token_id') or '')
    payoff=Decimal(int(winner==str(final.get('token_id')))) if winner else None
    if payoff is None: reasons.append('MISSING_TOKEN_SETTLEMENT_IDENTITY')
    if payoff is not None and payout is not None and abs(payout-quantity*payoff)>Decimal('.000001'):
        reasons.append('SETTLEMENT_QUANTITY_PAYOUT_MISMATCH')
    terminal_costs=[dec(final.get(k)) for k in ('fee','capital_cost','latency_cost','unwind_loss')]
    if any(c is None for c in terminal_costs): reasons.append('MISSING_TERMINAL_CASH_COSTS')
    extra_terminal=sum((c for c in terminal_costs if c is not None),ZERO)
    # Actual execution price already contains execution slippage. Never subtract
    # a second modelled markout/slippage estimate from the same cash movement.
    entry_other = debit-notional-fees if debit is not None and all(f is not None for f in entry_fees) else None
    if entry_other is not None and entry_other < -Decimal('.000001'): reasons.append('ENTRY_DEBIT_FEE_MISMATCH')
    terminal_complete=all(c is not None for c in terminal_costs)
    cash_cost = debit-notional+extra_terminal if debit is not None and terminal_complete else None
    cash_identity = payout-debit-extra_terminal if payout is not None and debit is not None and terminal_complete else None
    residual=cash_identity-result['ledger_final_pnl'] if cash_identity is not None and result['ledger_final_pnl'] is not None else None
    if residual is None or abs(residual)>Decimal('.000001'): reasons.append('CASH_IDENTITY_NOT_RECONCILED')
    predicted = ZERO; probability_complete=True; fill_details=[]
    for fill,q,price in zip(fills,quantities,prices):
        order=orders.get((fill['model_sha'],str(fill.get('order_id') or '')), {})
        evidence=decision_evidence(order, fill); p=evidence['probability']
        evidence['decision_fee_per_share']=dec(metadata(order).get('expected_fee_per_share'))
        evidence['decision_risk_per_share']=dec(metadata(order).get('expected_execution_risk'))
        evidence['decision_gross_margin_per_share']=p-evidence['decision_price'] if p is not None and evidence['decision_price'] is not None else None
        ap, price_at_arrival = evidence['arrival_probability'], evidence['arrival_price']
        evidence['arrival_gross_margin_per_share']=ap-price_at_arrival if ap is not None and price_at_arrival is not None else None
        for stage in ('decision','arrival'):
            gross=evidence.get(stage+'_gross_margin_per_share')
            fee=evidence.get(stage+'_fee_per_share')
            risk=evidence.get('arrival_execution_risk_per_share' if stage=='arrival' else 'decision_risk_per_share')
            evidence[stage+'_net_margin_per_share']=gross-fee-risk if all(x is not None for x in (gross,fee,risk)) else None
        if p is None: probability_complete=False
        else: predicted += q*p
        oid=str(fill.get('order_id') or ''); example=examples.get((fill['model_sha'],oid),{})
        fill_details.append({'fill_id':fill.get('fill_id'),'quantity':q,'price':price,**evidence,
            'queue_ahead':dec(order.get('queue_ahead')),'quote_duration_ms':example.get('exposure_ms'),
            'placement_action':example.get('action'), 'placement_features_source':metadata(order).get('placement_features_source'),
            'markouts':markouts.get((fill['model_sha'],str(fill.get('fill_id') or '')),[])})
    if not probability_complete: reasons.append('MISSING_DECISION_PROBABILITY')
    if any(not x.get('model_id') or not x.get('model_hash') for x in fill_details): reasons.append('MISSING_DECISION_MODEL_IDENTITY')
    if any(not x.get('model_stage_explicit') for x in fill_details): reasons.append('LEGACY_MODEL_STAGE_NOT_EXPLICIT')
    if any(x.get('arrival_probability') is None for x in fill_details): reasons.append('MISSING_ARRIVAL_POINT_PROBABILITY')
    result.update(quantity=quantity,fill_vwap=notional/quantity,entry_notional=notional,
        entry_fees=fees if all(f is not None for f in entry_fees) else None,
        other_entry_cash_costs=entry_other,terminal_cash_costs=extra_terminal,
        costs=cash_cost,payoff=payoff,settlement_payout=payout,entry_debit=debit,
        cash_identity_pnl=cash_identity,ledger_reconciliation_residual=residual,
        reconciled_to_microdollar=residual is not None and abs(residual)<=Decimal('.000001'),
        predicted_margin=predicted-notional if probability_complete else None,
        outcome_surprise=quantity*payoff-predicted if probability_complete and payoff is not None else None,
        gross_trading_pnl=quantity*payoff-notional if payoff is not None else None,
        fill_details=fill_details)
    if result['predicted_margin'] is not None and result['outcome_surprise'] is not None and cash_cost is not None and result['ledger_final_pnl'] is not None:
        result['decomposition_pnl']=result['predicted_margin']+result['outcome_surprise']-cash_cost
        result['decomposition_residual']=result['decomposition_pnl']-result['ledger_final_pnl']
        result['decomposition_reconciled_to_microdollar']=abs(result['decomposition_residual'])<=Decimal('.000001')
    if result['gross_trading_pnl'] is not None and cash_cost is not None:
        result['net_pnl_under_cost_stress']={str(multiplier):result['gross_trading_pnl']-cash_cost*multiplier
            for multiplier in (Decimal(1),Decimal('1.5'),Decimal(2))}
        result['gross_positive_net_negative']=result['gross_trading_pnl']>0 and result['ledger_final_pnl'] is not None and result['ledger_final_pnl']<0
    if any(x.get('decision_price') is None for x in fill_details): reasons.append('MISSING_DECISION_PRICE')
    result['missing_or_inconsistent']=sorted(set(reasons))
    return result


def learning_coverage(values):
    orders=[r for r in values if r.get('event_type')=='ORDER_SUBMITTED' and metadata(r).get('component')=='professional_maker']
    missing=Counter(); complete=0
    for row in orders:
        if placement_features(row) is not None: complete+=1; continue
        raw=metadata(row).get('placement_features') or {}
        required=['spread_ticks','imbalance','ofi','ew_vol_ticks','trade_intensity','cancel_intensity',
                  'short_return_ticks','inventory_fraction','local_latency_ms',
                  'aggressive_sell_prints_per_second' if row.get('side')=='BUY' else 'aggressive_buy_prints_per_second']
        absent=[name for name in required if dec(raw.get(name)) is None]
        for name in absent: missing[name]+=1
        if not absent: missing['INVALID_ACTION_SIDE_SIZE_OR_QUEUE']+=1
    order_ids={str(row.get('order_id')) for row in orders}
    fills={str(row['fill_id']):row for row in values if row.get('event_type')=='FILL' and row.get('fill_id')
           and str(row.get('order_id')) in order_ids and (dec(row.get('filled_size')) or ZERO)>0}
    marks=defaultdict(dict)
    for row in values:
        if row.get('event_type')=='MARKOUT' and str(row.get('fill_id')) in fills:
            for horizon,value in (row.get('markouts') or {}).items():
                if dec(value) is not None:marks[str(row['fill_id'])][horizon]=value
    coverage={}
    now_ms=time.time_ns()//1000000
    for horizon in (1,5,10,30):
        observed=sum(f'{horizon}s' in marks[fid] for fid in fills)
        pending=sum(f'{horizon}s' not in marks[fid] and now_ms-(row.get('receive_ts_ms') or row.get('recorded_ts_ms') or 0)<horizon*1000 for fid,row in fills.items())
        coverage[f'{horizon}s']={'positive_quantity_fills':len(fills),'observed':observed,'horizon_not_yet_due':pending,
            'missing_or_nonfinite_after_horizon':len(fills)-observed-pending}
    return {'orders':len(orders),'complete_vectors':complete,'complete_fraction':complete/len(orders) if orders else None,
            'fill_conditioned_markout_coverage':coverage,
            'missing_fields_overlapping':dict(missing),'excluded_orders':len(orders)-complete}


def opportunity_funnel(values, decisions=(), attempts=()):
    """Stable upstream identities are not independent statistical trials."""
    groups={}; unidentified=0
    def group(sha,key):
        return groups.setdefault((sha,key), {'model_sha':sha,'replay_key':key,'coordinator_attempts':0,
            'selected_attempts':0,'authorization_rejection_attempts':0,'rejection_reasons':Counter(),
            'order_ids':set(),'fill_ids':set(),'operational_filled_shares':ZERO,'terminal_outcomes':set()})
    for decision in decisions:
        inputs=decision.get('opportunity_inputs') or []
        if not inputs: unidentified+=1
        for item in inputs:
            if not item.get('model_sha') or not item.get('replay_key'): unidentified+=1;continue
            row=group(item['model_sha'],item['replay_key']);row['coordinator_attempts']+=1
            row['selected_attempts']+=int(item['replay_key']==decision.get('selected_replay_key'))
            row['market_id']=item.get('market_id');row['paper_probe']=item.get('paper_probe')
    for attempt in attempts:
        if not attempt.get('model_sha') or not attempt.get('replay_key'): unidentified+=1;continue
        row=group(attempt['model_sha'],attempt['replay_key']);row['authorization_rejection_attempts']+=1
        row['rejection_reasons'][str(attempt.get('reason') or 'UNKNOWN')]+=1
    order_keys={}
    for value in values:
        if value.get('event_type')!='ORDER_SUBMITTED':continue
        key=metadata(value).get('opportunity_replay_key') or value.get('opportunity_id') or value.get('candidate_id')
        if not key:unidentified+=1;continue
        sha=value['model_sha'];oid=str(value.get('order_id') or '')
        order_keys[(sha,oid)]=key;row=group(sha,key);row['order_ids'].add(oid)
    for value in values:
        key=order_keys.get((value['model_sha'],str(value.get('order_id') or '')))
        if not key:continue
        row=group(value['model_sha'],key)
        if value.get('event_type')=='FILL' and dec(value.get('filled_size')) is not None and dec(value['filled_size'])>0:
            identity=str(value.get('fill_id') or value.get('record_id'))
            if identity not in row['fill_ids']:
                row['fill_ids'].add(identity);row['operational_filled_shares']+=dec(value['filled_size'])
        if value.get('event_type')=='ORDER_STATE':
            outcome=metadata(value).get('execution_outcome')
            if outcome:row['terminal_outcomes'].add(str(outcome))
    output=[]
    for row in groups.values():
        output.append({k:sorted(v) if isinstance(v,set) else dict(v) if isinstance(v,Counter) else v for k,v in row.items()})
    return {'opportunities':output,'distinct_opportunities':len(output),
        'with_submitted_orders':sum(bool(r['order_ids']) for r in output),'with_operational_fills':sum(bool(r['fill_ids']) for r in output),
        'with_authorization_rejections':sum(r['authorization_rejection_attempts']>0 for r in output),
        'unidentified_or_legacy_observations':unidentified,'statistical_unit':'CONTRACT_NOT_ATTEMPT_OR_OPPORTUNITY'}


def analyze(values, sources=(), *, decisions=(), attempts=()):
    orders={}; fills=defaultdict(list); finals={}; markouts=defaultdict(list); examples={}; by_sha=defaultdict(list)
    for row in values:
        if metadata(row).get('counterfactual') is True or metadata(row).get('excluded_from_portfolio_equity') is True: continue
        sha=row['model_sha']; typ=row.get('event_type'); by_sha[sha].append(row)
        if typ=='ORDER_SUBMITTED': orders[(sha,str(row.get('order_id') or ''))]=row
        elif typ=='FILL' and dec(row.get('filled_size')) is not None and dec(row['filled_size'])>0:
            fills[(sha,str(row.get('position_id') or ''))].append(row)
        elif typ=='FINAL':
            key=(sha,str(row.get('position_id') or row.get('fill_id') or row.get('order_id') or row.get('record_id')))
            if key in finals: raise ValueError('multiple final records for one position')
            finals[key]=row
        elif typ=='MARKOUT': markouts[(sha,str(row.get('fill_id') or ''))].append({'record_id':row.get('record_id'),'markouts':row.get('markouts'),'metadata':metadata(row)})
    for sha,rows in by_sha.items():
        maker_orders={str(row.get('order_id')) for row in rows if row.get('event_type')=='ORDER_SUBMITTED'
                      and metadata(row).get('component')=='professional_maker'}
        for row in order_examples([r for r in rows if str(r.get('order_id')) in maker_orders]):
            examples[(sha,row['order_id'])]=row
    positions=[position_row(final,fills.get(key,[]),orders,markouts,examples) for key,final in sorted(finals.items())]
    pnl=sum((p['ledger_final_pnl'] for p in positions if p['ledger_final_pnl'] is not None),ZERO)
    strata={}
    for p in positions:
        identities=sorted({str(f.get('model_id') or 'UNKNOWN')+'@'+str(f.get('model_hash') or 'UNKNOWN') for f in p.get('fill_details',[])})
        key=str(p['component'])+'|probe='+str(p['paper_probe'])+'|'+','.join(identities)
        row=strata.setdefault(key,{'positions':0,'pnl':ZERO,'quantity':ZERO,'legacy_model_stage_not_explicit':0})
        row['positions']+=1;row['pnl']+=p['ledger_final_pnl'] or ZERO;row['quantity']+=p.get('quantity') or ZERO
        row['legacy_model_stage_not_explicit']+=int('LEGACY_MODEL_STAGE_NOT_EXPLICIT' in p['missing_or_inconsistent'])
    return {'schema':SCHEMA,'recorded_at_ns':time.time_ns(),'source_code_shas':sorted(by_sha),
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','sources':list(sources),
        'positions':positions,'canonical_final_positions':len(positions),'canonical_final_pnl':pnl,
        'strata_by_component_probe_model':strata,
        'reconciled_positions':sum(p.get('reconciled_to_microdollar') is True for p in positions),
        'unattributed_ledger_pnl':sum((p['ledger_final_pnl'] for p in positions if not p.get('reconciled_to_microdollar') and p['ledger_final_pnl'] is not None),ZERO),
        'missing_or_inconsistent':dict(Counter(reason for p in positions for reason in p['missing_or_inconsistent'])),
        'learning_coverage_by_sha':{sha:learning_coverage(rows) for sha,rows in by_sha.items()},
        'maker_outcomes_by_sha':{sha:dict(Counter(row['execution_outcome'] for (s,_),row in examples.items() if s==sha)) for sha in by_sha},
        'opportunity_funnel':opportunity_funnel([r for rows in by_sha.values() for r in rows],decisions,attempts),
        'identity_is_accounting_not_causal':True}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--ledger',type=Path,action='append',required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--run-root',type=Path);ap.add_argument('--csv',type=Path)
    args=ap.parse_args();values,sources=read_sources(args.ledger)
    if args.run_root:
        markout_paths=sorted((args.run_root/'research/evidence/maker_markout').glob('*.json'))
        if markout_paths:
            marks,mark_sources=read_sources(markout_paths)
            if any(row.get('event_type')!='MARKOUT' for row in marks):raise ValueError('non-markout research source')
            # Exact fill identity joins; research labels never create cash rows.
            existing={(row['model_sha'],row['record_id']):row for row in values}
            for row in marks:
                key=(row['model_sha'],row['record_id'])
                if key in existing and existing[key]!=row:raise ValueError('conflicting markout source')
                existing[key]=row
            values=list(existing.values());sources.extend(mark_sources)
    def auxiliary(relative):
        if args.run_root is None:return []
        path=args.run_root/relative
        if not path.exists():return []
        with path.open('rb') as stream:
            data=stream.read(os.fstat(stream.fileno()).st_size)
        data=data[:data.rfind(b'\n')+1]
        sources.append({'path':str(path),'prefix_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        return [json.loads(line) for line in data.splitlines() if line.strip()]
    report=analyze(values,sources,decisions=auxiliary('opportunities/decisions.jsonl'),attempts=auxiliary('micro_maker/authorization_attempts.jsonl'))
    args.output.parent.mkdir(parents=True,exist_ok=True);tmp=args.output.with_suffix('.tmp')
    tmp.write_text(json.dumps(report,default=str,sort_keys=True,indent=2)+'\n');os.replace(tmp,args.output)
    if args.csv:
        fields=['code_sha','position_id','market_id','token_id','component','paper_probe','quantity','fill_vwap',
            'predicted_margin','outcome_surprise','costs','ledger_final_pnl','ledger_reconciliation_residual','missing_or_inconsistent']
        args.csv.parent.mkdir(parents=True,exist_ok=True)
        with args.csv.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
            for row in report['positions']:writer.writerow({**row,'missing_or_inconsistent':';'.join(row['missing_or_inconsistent'])})
    print(json.dumps({k:str(report[k]) for k in ['canonical_final_positions','canonical_final_pnl','reconciled_positions','unattributed_ledger_pnl']}))

if __name__=='__main__': main()
