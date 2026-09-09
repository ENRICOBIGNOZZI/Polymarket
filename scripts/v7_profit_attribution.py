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
import re
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


def read_sources(paths, *, require_complete=False):
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
                if not line: break
                if not line.endswith(b'\n'):
                    if require_complete or path.suffix == '.gz':
                        raise ValueError('incomplete sealed ledger source: '+str(path))
                    break
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


def archived_ledgers(root):
    """Include every retained checkpoint; deduplication happens by exact record ID."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('missing or unsafe canonical archive root')
    paths, inventory = [], []
    for archive in sorted(root.glob('cutover-*')):
        if archive.is_symlink() or not archive.is_dir():
            raise ValueError('unsafe cutover directory')
        if not re.fullmatch(r'cutover-[0-9a-f]{40}-[0-9]+-[0-9]+', archive.name):
            raise ValueError('invalid cutover identity')
        folders = [archive/'ledger', archive/'archive', archive/'archive/canonical-ledger']
        if any(p.is_symlink() for p in folders):
            raise ValueError('unsafe archived ledger directory')
        selected = [p for p in (archive/'ledger/execution.jsonl', archive/'ledger/execution.jsonl.gz') if p.exists()]
        selected += sorted((archive/'archive/canonical-ledger').glob('*.jsonl.gz'))
        if any(p.is_symlink() for p in selected):
            raise ValueError('unsafe archived ledger source')
        paths.extend(selected)
        inventory.append({'archive':str(archive), 'ledger_sources':len(selected),
                          'coverage':'RETAINED_CANONICAL_SOURCES' if selected else 'NO_RETAINED_LEDGER_SOURCE'})
    return paths, inventory


AUXILIARY_STREAMS={
    'decisions':'opportunities/decisions.jsonl',
    'authorization_attempts':'micro_maker/authorization_attempts.jsonl',
    'authorization_publications':'opportunities/authorization_publications.jsonl',
}


def read_auxiliary(root, *, sealed=False):
    """Freeze observed prefixes and source context; never rewrite original rows."""
    root=Path(root);outputs={kind:[] for kind in AUXILIARY_STREAMS};sources=[];inventory={}
    runtime=root/'control/runtime_status.json';context={}
    if (root/'control').is_symlink() or runtime.is_symlink():raise ValueError('unsafe runtime identity source')
    if runtime.is_file():
        raw=runtime.read_bytes();value=json.loads(raw)
        sha=value.get('model_sha')
        if re.fullmatch('[0-9a-f]{40}',str(sha or '')) and value.get('paper_only') is True and value.get('authenticated_execution') is False and value.get('real_order_submission') is not True:
            if sealed and not root.name.startswith('cutover-'+sha+'-'):raise ValueError('archived auxiliary/runtime SHA conflict')
            context={'model_sha':sha,'run_id':value.get('run_id'),'runtime_source_sha256':hashlib.sha256(raw).hexdigest(),
                     'identity_source':'OBSERVED_RUNTIME_GENERATION_METADATA; NOT_RECORD_SPECIFIC_MODEL_IDENTITY'}
            sources.append({'path':str(runtime),'sha256':context['runtime_source_sha256'],'prefix_bytes':len(raw),'source_role':'auxiliary_generation_identity'})
    for kind,relative in AUXILIARY_STREAMS.items():
        base=root/relative
        if base.parent.is_symlink():raise ValueError('unsafe auxiliary source directory')
        paths=[p for p in [base,Path(str(base)+'.gz')] if p.exists()]
        paths+=sorted(base.parent.glob(base.name+'.segment-*.jsonl.gz'))
        paths+=sorted(p for p in base.parent.glob(base.name+'.*') if re.fullmatch(re.escape(base.name)+r'\.[0-9]+(?:\.gz)?',p.name))
        inventory[kind]={'source_files':len(paths),'coverage':'OBSERVED_RETAINED_SOURCES' if paths else 'NO_RETAINED_SOURCE; NOT_ZERO_ATTEMPTS'}
        for path in paths:
            if path.is_symlink() or not path.is_file():raise ValueError('unsafe auxiliary source')
            h=hashlib.sha256();size=count=0;partial=False
            opener=gzip.open if path.suffix=='.gz' else open
            with opener(path,'rb') as f:
                limit=None if path.suffix=='.gz' else os.fstat(f.fileno()).st_size
                while limit is None or f.tell()<limit:
                    line=f.readline() if limit is None else f.readline(limit-f.tell())
                    if not line:break
                    if not line.endswith(b'\n'):
                        if sealed or path!=base:raise ValueError('incomplete sealed auxiliary source: '+str(path))
                        partial=True;break
                    h.update(line);size+=len(line);count+=1
                    if not line.strip():continue
                    row=json.loads(line)
                    if not isinstance(row,dict) or row.get('paper_only') is not True or row.get('authenticated_execution') is not False or row.get('real_order_submission') is True:raise ValueError('non-PAPER auxiliary source')
                    if any(k.startswith('_evidence_') for k in row):raise ValueError('reserved auxiliary provenance field')
                    row['_evidence_record_sha256']=hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
                    row['_evidence_generation']=context
                    outputs[kind].append(row)
            sources.append({'path':str(path),'source_role':kind,'sha256':h.hexdigest(),'decompressed_prefix_bytes':size,
                            'complete_lines':count,'partial_tail_waiting':partial,'sealed':sealed or path!=base})
    return outputs,sources,inventory


def auxiliary_record_hash(value):
    original={k:v for k,v in value.items() if not k.startswith('_evidence_')}
    return hashlib.sha256(json.dumps(original,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def compact_funnel(funnel):
    """Small permanent stage/attempt summary; detailed opportunities stay in attribution."""
    fields=('distinct_opportunities','attempt_totals','with_submitted_orders','with_operational_fills',
            'with_authorization_rejections','stages','authorization_generation','archive_coverage',
            'unidentified_or_legacy_observations','limitations','statistical_unit')
    result={k:funnel[k] for k in fields if k in funnel};by_code=defaultdict(list);reasons=Counter()
    for row in funnel.get('opportunities',[]):
        by_code[row['model_sha']].append(row);reasons.update(row.get('rejection_reasons',{}))
    result['authorization_rejection_reasons']=dict(reasons)
    result['generations']={sha:{'distinct_opportunities':len(rows),
        'coordinator_opportunity_attempts':sum(r['coordinator_attempts'] for r in rows),
        'selected_opportunities':sum(r['selected_attempts']>0 for r in rows),
        'authorization_rejection_attempts':sum(r['authorization_rejection_attempts'] for r in rows),
        'with_orders':sum(bool(r['order_ids']) for r in rows),'with_fills':sum(bool(r['fill_ids']) for r in rows),
        'with_settlement':sum(bool(r['settled_position_ids']) for r in rows)} for sha,rows in sorted(by_code.items())}
    result['auxiliary_source_inventory']=funnel.get('auxiliary_source_inventory',[])
    return result


def historical_summary(report):
    """Accounting strata remain separate from the current deployed generation."""
    if (report.get('paper_only') is not True or report.get('authenticated_execution') is not False
            or report.get('real_order_submission') is not False
            or report.get('execution_authority') != 'ZERO_AUTHORITY_RESEARCH_ONLY'):
        raise ValueError('unsafe historical attribution')
    positions = report['positions']
    if (len(positions) != report['canonical_final_positions']
            or sum((dec(p['ledger_final_pnl']) for p in positions), ZERO) != dec(report['canonical_final_pnl'])):
        raise ValueError('historical position population does not reconcile')
    generations = {}
    for code in report['source_code_shas']:
        rows = [p for p in positions if p['code_sha'] == code]
        components = defaultdict(list)
        for p in rows: components[p['component']].append(p)
        generations[code] = {
            'positions':len(rows), 'net_pnl_usd':str(sum((dec(p['ledger_final_pnl']) for p in rows), ZERO)),
            'reconciled_positions':sum(p.get('reconciled_to_microdollar') is True for p in rows),
            'unexplained_pnl_usd':str(sum((dec(p['ledger_final_pnl']) for p in rows if not p.get('reconciled_to_microdollar')), ZERO)),
            'components':{name:{'positions':len(ps), 'net_pnl_usd':str(sum((dec(p['ledger_final_pnl']) for p in ps), ZERO))}
                          for name, ps in sorted(components.items())},
            'missing_or_inconsistent':dict(Counter(reason for p in rows for reason in p['missing_or_inconsistent']))}
    return {'scope':'ARCHIVED_GENERATIONS_ACCOUNTING_ONLY; NOT_CURRENT_RUNTIME_PERFORMANCE_OR_MODEL_COMPARISON',
            'recorded_at_ns':report['recorded_at_ns'], 'positions':len(positions),
            'net_pnl_usd':str(report['canonical_final_pnl']), 'reconciled_positions':report['reconciled_positions'],
            'unexplained_pnl_usd':str(report['unattributed_ledger_pnl']), 'generations':generations,
            'archive_inventory':report.get('archive_inventory', []),
            'source_count':len(report.get('sources', [])),
            'opportunity_funnel':compact_funnel(report.get('opportunity_funnel',{})),
            'missing_values_are_unknown':True, 'automatic_promotion':False}


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
    execution=m.get('execution_alpha') or envelope.get('execution_alpha') or {}
    features=execution.get('features') or {};context=envelope.get('crypto_context') or (m.get('coordinator_receipt') or {}).get('crypto_context') or {}
    bounds=envelope.get('fair_value') or {}
    outcome=m.get('outcome');pm_yes=dec(m.get('pm_mid'));arrival_pm_yes=dec(m.get('arrival_pm_mid'))
    token_probability=lambda p: p if outcome=='YES' else 1-p if p is not None and outcome=='NO' else None
    if envelope:lower,upper=dec(bounds.get('lower')),dec(bounds.get('upper'))
    elif outcome=='YES':lower,upper=dec(m.get('fair_lower')),dec(m.get('fair_upper'))
    elif outcome=='NO':lower,upper=token_probability(dec(m.get('fair_upper'))),token_probability(dec(m.get('fair_lower')))
    else:lower,upper=None,None
    stamp=(envelope.get('decision_receive_timestamp_ns') or 0)//1_000_000 if envelope else m.get('decision_observed_ts_ms')
    feature_stamp=m.get('placement_features_timestamp_ms')
    return {'probability':probability,'probability_source':source if probability is not None else None,
        'run_id':envelope.get('run_id'),'asset':context.get('asset'),'horizon':context.get('horizon'),
        'contract_family':context.get('contract_family'),'settlement_semantic_hash':context.get('settlement_semantic_hash'),
        'source_snapshot_identity':envelope.get('source_snapshot_identity') or order.get('book_snapshot_id'),
        'order_record_id':order.get('record_id'),'order_id':order.get('order_id'),
        'decision_pm_probability':token_probability(pm_yes),
        'arrival_pm_probability':token_probability(arrival_pm_yes),
        'source_decision_pm_yes':pm_yes,'source_arrival_pm_yes':arrival_pm_yes,
        'probability_lower':lower,'probability_upper':upper,
        'probability_bounds_semantics':'PURCHASED_TOKEN_PROBABILITY',
        'outcome':m.get('outcome'),'tte_seconds':dec(m.get('tte_seconds',features.get('tte_seconds'))),
        'arrival_tte_seconds':dec(m.get('arrival_tte_seconds')),
        'execution_model_id':execution.get('model_id'),'execution_model_hash':execution.get('model_hash'),
        'execution_policy_hash':m.get('policy_hash'),'execution_config_hash':m.get('config_hash'),
        'portfolio_policy_hash':envelope.get('policy_hash'),'portfolio_config_hash':envelope.get('config_hash'),
        'fill_probability_estimate':dec(order.get('predicted_fill_probability')),
        'placement_feature_timestamp_ms':feature_stamp,
        'placement_feature_age_at_decision_ms':stamp-feature_stamp if stamp and feature_stamp else None,
        'placement_features':m.get('placement_features'),'selection_features':execution.get('features'),
        'selection_feature_age_at_decision_ms':(stamp-execution['feature_receive_timestamp_ns']/1_000_000)
            if stamp and execution.get('feature_receive_timestamp_ns') else None,
        'model_id':model.get('model_id') or m.get('decision_probability_model_id') or m.get('probability_model_id'),
        'model_hash':model.get('model_hash') or m.get('decision_probability_model_hash') or m.get('probability_model_hash'),
        'model_stage_explicit':bool(model or m.get('decision_probability_model_id')),
        'decision_price':dec(m.get('decision_limit_price')) if not envelope else dec(((envelope.get('execution_plan') or {}).get('legs') or [{}])[0].get('limit_price')),
        'decision_timestamp_ms':stamp or None,
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
            'fill_timestamp_ms':fill.get('receive_ts_ms'),'fill_record_id':fill.get('record_id'),
            'opposite_flow_prints_seen':metadata(fill).get('opposite_flow_prints_seen'),
            'opposite_flow_shares_seen':dec(metadata(fill).get('opposite_flow_shares_seen')),
            'price_reach_prints_seen':metadata(fill).get('price_reach_prints_seen'),
            'filled_fraction':q/dec(order['intended_size']) if dec(order.get('intended_size')) and dec(order['intended_size'])>0 else None,
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


def order_replay_identity(value):
    """Use a submitted order's own authorization, never another nearby decision."""
    m=metadata(value); envelope=m.get('opportunity_envelope') or {}
    explicit=m.get('opportunity_replay_key') or envelope.get('deterministic_replay_key')
    receipt=m.get('coordinator_receipt') or {}; selected=receipt.get('selected_replay_key')
    matches=[item for item in receipt.get('opportunity_inputs',[]) if selected and item.get('replay_key')==selected
        and item.get('model_sha')==value.get('model_sha') and item.get('market_id')==value.get('market_id')
        and item.get('token_id')==value.get('token_id')]
    if len(matches)==1:
        if explicit and explicit!=selected:raise ValueError('order/receipt replay identity conflict')
        return selected,'EXACT_ORDER_COORDINATOR_RECEIPT'
    if explicit:return explicit,'EXPLICIT_REPLAY_IDENTITY'
    # Legacy candidate IDs are preserved, but never guessed into a namespaced key.
    return value.get('opportunity_id') or value.get('candidate_id'),'LEGACY_IDENTITY'


def opportunity_funnel(values, decisions=(), attempts=()):
    """Stable upstream identities are not independent statistical trials."""
    groups={}; unidentified=0;attempt_ids={};decision_ids={};generation_observed=False
    duplicates=Counter();decision_count=attempt_count=legacy_selected_count=0
    def group(sha,key):
        return groups.setdefault((sha,key), {'model_sha':sha,'replay_key':key,'coordinator_attempts':0,
            'selected_attempts':0,'portfolio_not_selected_attempts':0,'execution_selection_filtered_attempts':0,
            'authorization_rejection_attempts':0,'authorization_generated_attempts':0,'rejection_reasons':Counter(),
            'order_ids':set(),'live_order_ids':set(),'fill_ids':set(),'markout_fill_ids':set(),
            'settled_position_ids':set(),'profitable_position_ids':set(),'identity_sources':set(),
            'opposite_flow_order_ids':set(),'price_reached_order_ids':set(),'queue_exhausted_order_ids':set(),
            'operational_filled_shares':ZERO,'terminal_outcomes':set()})
    for decision in decisions:
        inputs=decision.get('opportunity_inputs') or []
        context=decision.get('_evidence_generation') or {}
        shas=tuple(sorted({item.get('model_sha') for item in inputs if item.get('model_sha')})) or ((context['model_sha'],) if context.get('model_sha') else ())
        clock=decision.get('decision_timestamp_ns')
        if shas and isinstance(clock,int) and not isinstance(clock,bool) and clock>0:
            identity=(shas,decision.get('owner'),clock);fingerprint=auxiliary_record_hash(decision)
            if identity in decision_ids:
                if decision_ids[identity]!=fingerprint:raise ValueError('conflicting coordinator attempt identity')
                duplicates['coordinator_decisions']+=1;continue
            decision_ids[identity]=fingerprint
        decision_count+=1
        if not inputs:
            selected=decision.get('selected_replay_key')
            if selected and context.get('model_sha'):
                inputs=[{'model_sha':context['model_sha'],'replay_key':selected}]
                legacy_selected_count+=1
            else:unidentified+=1
        for item in inputs:
            if not item.get('model_sha') or not item.get('replay_key'): unidentified+=1;continue
            row=group(item['model_sha'],item['replay_key']);row['coordinator_attempts']+=1
            row['identity_sources'].add('EXPLICIT_COORDINATOR_INPUT' if decision.get('opportunity_inputs') else 'LEGACY_SELECTED_KEY_WITH_RUNTIME_GENERATION')
            row['selected_attempts']+=int(item['replay_key']==decision.get('selected_replay_key'))
            row['portfolio_not_selected_attempts']+=int(item['replay_key']!=decision.get('selected_replay_key'))
            row['execution_selection_filtered_attempts']+=int(item.get('retained_after_execution_selection') is False)
            if item.get('market_id') is not None:row['market_id']=item['market_id']
            if item.get('paper_probe') is not None:row['paper_probe']=item['paper_probe']
    for attempt in attempts:
        attempt_id=attempt.get('attempt_id')
        if attempt_id:
            identity=(attempt.get('model_sha'),attempt_id)
            fingerprint=auxiliary_record_hash(attempt)
            if identity in attempt_ids:
                if attempt_ids[identity]!=fingerprint:raise ValueError('conflicting authorization attempt identity')
                duplicates['authorization_attempts']+=1;continue
            attempt_ids[identity]=fingerprint
        attempt_count+=1
        if not attempt.get('model_sha') or not attempt.get('replay_key'): unidentified+=1;continue
        row=group(attempt['model_sha'],attempt['replay_key'])
        if attempt.get('result','REJECTED')=='REJECTED':
            row['authorization_rejection_attempts']+=1
            row['rejection_reasons'][str(attempt.get('reason') or 'UNKNOWN')]+=1
        elif attempt.get('result') in {'GENERATED','AUTHORIZED'}:
            row['authorization_generated_attempts']+=1;generation_observed=True
    order_keys={};position_keys=defaultdict(set);fill_keys={}
    for value in values:
        if value.get('event_type')!='ORDER_SUBMITTED':continue
        key,identity_source=order_replay_identity(value)
        if not key:unidentified+=1;continue
        sha=value['model_sha'];oid=str(value.get('order_id') or '')
        order_keys[(sha,oid)]=key;row=group(sha,key);row['order_ids'].add(oid)
        row['identity_sources'].add(identity_source);row['market_id']=value.get('market_id')
        if value.get('order_state')=='LIVE':row['live_order_ids'].add(oid)
    for value in values:
        sha=value['model_sha'];oid=str(value.get('order_id') or '')
        key=order_keys.get((sha,oid))
        if not key:continue
        row=group(sha,key);m=metadata(value)
        if value.get('order_state')=='LIVE':row['live_order_ids'].add(oid)
        if (dec(m.get('opposite_flow_prints_seen')) or ZERO)>0:row['opposite_flow_order_ids'].add(oid)
        if (dec(m.get('price_reach_prints_seen')) or ZERO)>0:row['price_reached_order_ids'].add(oid)
        if value.get('event_type')=='FILL' and dec(value.get('filled_size')) is not None and dec(value['filled_size'])>0:
            identity=str(value.get('fill_id') or value.get('record_id'))
            if identity not in row['fill_ids']:
                row['fill_ids'].add(identity);row['operational_filled_shares']+=dec(value['filled_size'])
            fill_keys[(sha,identity)]=key
            if value.get('position_id'):position_keys[(sha,value['position_id'])].add(key)
            if m.get('component')=='professional_maker':row['queue_exhausted_order_ids'].add(oid)
        if value.get('event_type')=='ORDER_STATE':
            outcome=metadata(value).get('execution_outcome')
            if outcome:row['terminal_outcomes'].add(str(outcome))
    for value in values:
        sha=value['model_sha']
        if value.get('event_type')=='MARKOUT':
            key=fill_keys.get((sha,str(value.get('fill_id') or '')))
            if key and any(dec(x) is not None for x in (value.get('markouts') or {}).values()):
                group(sha,key)['markout_fill_ids'].add(str(value['fill_id']))
        if value.get('event_type')=='FINAL':
            keys=position_keys.get((sha,value.get('position_id')),set())
            for key in keys:
                row=group(sha,key);row['settled_position_ids'].add(str(value['position_id']))
                # A position may contain several opportunities. Only its full
                # position outcome is identified; never allocate its PnL twice.
                if len(keys)==1 and dec(value.get('final_pnl')) is not None and dec(value['final_pnl'])>0:
                    row['profitable_position_ids'].add(str(value['position_id']))
    output=[]
    for row in groups.values():
        output.append({k:sorted(v) if isinstance(v,set) else dict(v) if isinstance(v,Counter) else v for k,v in row.items()})
    return {'opportunities':output,'distinct_opportunities':len(output),
        'attempt_totals':{'coordinator_decisions':decision_count,'coordinator_opportunity_inputs':sum(r['coordinator_attempts'] for r in output),
            'authorization_records':attempt_count,'legacy_selected_only_decisions':legacy_selected_count,
            'exact_duplicate_records_excluded':dict(duplicates)},
        'with_submitted_orders':sum(bool(r['order_ids']) for r in output),'with_operational_fills':sum(bool(r['fill_ids']) for r in output),
        'with_authorization_rejections':sum(r['authorization_rejection_attempts']>0 for r in output),
        'stages':{name:sum(bool(r[field]) for r in output) for name,field in {
            'selected':'selected_attempts','portfolio_not_selected':'portfolio_not_selected_attempts',
            'live_orders':'live_order_ids',
            'opposite_flow':'opposite_flow_order_ids','price_reached':'price_reached_order_ids',
            'queue_exhausted_positive_fill':'queue_exhausted_order_ids','markouts':'markout_fill_ids',
            'settled':'settled_position_ids','profitable_unambiguously_attributed':'profitable_position_ids'}.items()},
        'authorization_generation':{'distinct_opportunities_observed':sum(bool(r['authorization_generated_attempts']) for r in output) if generation_observed else None,
            'attempts_observed':sum(r['authorization_generated_attempts'] for r in output) if generation_observed else None,
            'submitted_order_lower_bound':sum(len(r['order_ids']) for r in output),
            'coverage':'EXPLICIT_PUBLICATION_EVENTS_ONLY; LEGACY_ATTEMPT_TOTAL_UNKNOWN'},
        'limitations':['Legacy decisions without an input list expose at most the selected key; other candidates remain unknown.',
            'Decision attempts are deduplicated only with an observed generation, owner and positive decision timestamp; unidentified attempts are not silently collapsed.',
            'Authorization generation is counted only where an explicit generation event exists; submitted orders are a separate lower bound.',
            'Flow/reach/queue stages apply to Maker evidence; missing Taker stages are not zero-flow observations.',
            'A profitable shared position is not allocated to an individual opportunity without leg accounting.'],
        'unidentified_or_legacy_observations':unidentified,'statistical_unit':'CONTRACT_NOT_ATTEMPT_OR_OPPORTUNITY'}


def analyze(values, sources=(), *, decisions=(), attempts=()):
    orders={}; fills=defaultdict(list); fill_records={}; finals={}; markouts=defaultdict(list); examples={}; by_sha=defaultdict(list)
    for row in values:
        if metadata(row).get('counterfactual') is True or metadata(row).get('excluded_from_portfolio_equity') is True: continue
        sha=row['model_sha']; typ=row.get('event_type'); by_sha[sha].append(row)
        if typ=='ORDER_SUBMITTED': orders[(sha,str(row.get('order_id') or ''))]=row
        elif typ=='FILL' and dec(row.get('filled_size')) is not None and dec(row['filled_size'])>0:
            fills[(sha,str(row.get('position_id') or ''))].append(row)
            fill_records[(sha,row['record_id'])]=row
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
    positions=[];claimed_fills=set()
    for key,final in sorted(finals.items()):
        linked=list(fills.get(key,[]));reference=metadata(final).get('canonical_maker_fill_record_id')
        mode='POSITION_ID';missing_reference=False
        if reference:
            referenced=fill_records.get((key[0],reference))
            if referenced is None:
                missing_reference=True
            else:
                for field in ('model_sha','fill_id','order_id','market_id','token_id','side'):
                    if not referenced.get(field) or referenced[field]!=final.get(field):
                        raise ValueError('canonical final fill-reference identity conflict: '+field)
                if (referenced.get('event_id') and final.get('event_id')
                        and referenced['event_id']!=final['event_id']):
                    raise ValueError('canonical final fill-reference identity conflict: event_id')
                if referenced.get('position_id') not in (None,'',final.get('position_id')):
                    raise ValueError('canonical final fill-reference position conflict')
                start,end=dec(referenced.get('recorded_ts_ms')),dec(final.get('recorded_ts_ms'))
                if start is None or end is None or not ZERO<start<=end:
                    raise ValueError('canonical final fill-reference causal timestamp conflict')
                if linked and not any(r['record_id']==reference for r in linked):
                    raise ValueError('canonical final fill-reference disagrees with position fills')
                if not linked:
                    linked=[referenced];mode='EXPLICIT_CANONICAL_FINAL_FILL_REFERENCE'
        for fill in linked:
            identity=(fill['model_sha'],fill['record_id'])
            if identity in claimed_fills:raise ValueError('canonical fill assigned to multiple final positions')
            claimed_fills.add(identity)
        result=position_row(final,linked,orders,markouts,examples)
        result['fill_join']={'mode':mode,'final_fill_record_reference':reference,
            'source_fill_record_ids':[r['record_id'] for r in linked],
            'source_fill_position_ids':[r.get('position_id') for r in linked]}
        if missing_reference:result['missing_or_inconsistent'].append('CANONICAL_FINAL_FILL_REFERENCE_UNAVAILABLE')
        positions.append(result)
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
    ap=argparse.ArgumentParser(description=__doc__)
    scope=ap.add_mutually_exclusive_group(required=True)
    scope.add_argument('--ledger',type=Path,action='append')
    scope.add_argument('--archive-root',type=Path,help='All archived canonical ledgers/checkpoints; output stays separate from current cash')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--run-root',type=Path);ap.add_argument('--csv',type=Path)
    args=ap.parse_args()
    if args.archive_root and args.run_root:ap.error('--run-root auxiliary observations require current --ledger scope')
    paths, inventory = archived_ledgers(args.archive_root) if args.archive_root else (args.ledger, [])
    values,sources=read_sources(paths,require_complete=bool(args.archive_root))
    roots=[Path(item['archive']) for item in inventory] if args.archive_root else [args.run_root] if args.run_root else []
    markout_paths=[]
    for root in roots:
        folder=root/'research/evidence/maker_markout'
        if any(p.is_symlink() for p in [root/'research',root/'research/evidence',folder]):raise ValueError('unsafe markout source directory')
        markout_paths+=sorted(folder.glob('*.json'))+sorted(folder.glob('*.json.gz'))
    if markout_paths:
        marks,mark_sources=read_sources(markout_paths,require_complete=True)
        if any(row.get('event_type')!='MARKOUT' for row in marks):raise ValueError('non-markout research source')
        # Exact identities join descriptive labels; these files cannot introduce cash.
        existing={(row['model_sha'],row['record_id']):row for row in values}
        for row in marks:
            key=(row['model_sha'],row['record_id'])
            if key in existing and existing[key]!=row:raise ValueError('conflicting markout source')
            existing[key]=row
        values=list(existing.values());sources.extend({**p,'source_role':'maker_markout'} for p in mark_sources)
    decisions=[];attempts=[];auxiliary_inventory=[]
    for root in roots:
        streams,proofs,coverage=read_auxiliary(root,sealed=bool(args.archive_root))
        decisions.extend(streams['decisions']);attempts.extend(streams['authorization_attempts']);attempts.extend(streams['authorization_publications'])
        sources.extend(proofs);auxiliary_inventory.append({'run_root':str(root),'streams':coverage})
    report=analyze(values,sources,decisions=decisions,attempts=attempts)
    report['opportunity_funnel']['auxiliary_source_inventory']=auxiliary_inventory
    if args.archive_root:
        report['archive_inventory']=inventory
        report['opportunity_funnel']['archive_coverage']='ALL_RETAINED_LEDGER_AND_DECLARED_COORDINATOR_AUTHORIZATION_STREAMS; MISSING_SOURCES_AND_LEGACY_INPUTS_EXPLICIT'
        report['historical_summary']=historical_summary(report)
    args.output.parent.mkdir(parents=True,exist_ok=True);tmp=args.output.with_suffix('.tmp')
    encoded=(json.dumps(report,default=str,sort_keys=True,indent=2)+'\n').encode()
    tmp.write_bytes(gzip.compress(encoded,mtime=0) if args.output.suffix=='.gz' else encoded);os.replace(tmp,args.output)
    if args.csv:
        fields=['code_sha','position_id','market_id','token_id','component','paper_probe','quantity','fill_vwap',
            'predicted_margin','outcome_surprise','costs','ledger_final_pnl','ledger_reconciliation_residual','missing_or_inconsistent']
        args.csv.parent.mkdir(parents=True,exist_ok=True)
        with args.csv.open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
            for row in report['positions']:writer.writerow({**row,'missing_or_inconsistent':';'.join(row['missing_or_inconsistent'])})
    print(json.dumps({k:str(report[k]) for k in ['canonical_final_positions','canonical_final_pnl','reconciled_positions','unattributed_ledger_pnl']}))

if __name__=='__main__': main()
