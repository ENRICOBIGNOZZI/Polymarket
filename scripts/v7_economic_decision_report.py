#!/usr/bin/env python3
"""Computed PAPER diagnosis and next-action memo; never execution authority."""
from collections import Counter, defaultdict
from decimal import Decimal
import argparse
import gzip
import json
from pathlib import Path
import time

from v7_evidence_store import AUTH, canonical, digest, immutable
from v7_permanent_evidence import atomic
from v7_profit_attribution import dec

CODE = Path(__file__).read_bytes()
CODE_HASH = digest(CODE)
ZERO = Decimal(0)


def money_sum(values):
    values = [dec(v) for v in values]
    return str(sum(values, ZERO)) if all(v is not None for v in values) else None


def ranked_regions(signal,minimum_contracts=12):
    """Descriptive ranking of fixed cells; tiny or missing cells cannot win."""
    eligible=[];excluded=0
    for name,metrics in signal.get('cells',{}).items():
        if name=='ALL':continue
        brier=metrics.get('brier_improvement_over_pm') or {}
        if brier.get('mean') is None or brier.get('contracts',0)<minimum_contracts:
            excluded+=1;continue
        eligible.append({'region':name,'contracts':brier['contracts'],'brier_improvement_over_pm':brier['mean'],
            'settlement_surplus_cost2_delay1000':metrics.get('settlement_surplus_delay1000_cost2'),
            'definition_status':'ORIGINAL_PROTOCOL_FIXED_BIN' if name.split('|')[0] in
                ('outcome','margin','tte','outcome_margin_tte') else signal.get('registration_status','UNKNOWN')})
    eligible.sort(key=lambda r:(r['brier_improvement_over_pm'],r['region']))
    return {'weakest':eligible[:3],'strongest':list(reversed(eligible[-3:])),
        'eligible_cells':len(eligible),'excluded_small_or_missing_cells':excluded,
        'minimum_contracts_for_descriptive_display':minimum_contracts,
        'interpretation':'POST_HOC_RANK_ORDER_OF_FIXED_CELLS; OVERLAPPING_REGIONS; NO_WINNER_PROMOTION_OR_CONFIRMATORY_CLAIM'}


def quality_metric(count, denominator=None, *, observations=None, scope, previous=None):
    observations = observations if observations is not None else None
    contracts = sorted({r['market_id'] for r in observations if r.get('market_id')}) if observations is not None else None
    stamps = [r.get('timestamp_ms') for r in observations or [] if isinstance(r.get('timestamp_ms'), (int, float))]
    notionals = [r.get('notional') for r in observations or []]
    comparable = previous and previous.get('counter_scope') == scope and previous.get('count') is not None and count is not None
    delta = count-previous['count'] if comparable and count >= previous['count'] else None
    return {'count': count, 'denominator': denominator,
            'fraction': count/denominator if count is not None and denominator else None,
            'affected_contracts': len(contracts) if contracts is not None else None,
            'affected_contract_ids': contracts,
            'affected_economic_notional_usd': money_sum(notionals) if observations is not None else None,
            'first_occurrence_ms': min(stamps) if stamps else None, 'last_occurrence_ms': max(stamps) if stamps else None,
            'counter_scope': scope, 'trend': {'delta_since_previous_report': delta,
                'state': 'COMPARABLE_COUNTER_DELTA' if delta is not None else 'NO_COMPARABLE_PREVIOUS_COUNTER'},
            'unknown_fields_mean': 'UNOBSERVABLE_FROM_THIS_SOURCE; NOT_ZERO'}


def scorecard(attribution, cohorts, statuses, previous=None):
    previous = previous or {}; metrics = {}; fills = []
    for position in attribution['positions']:
        for fill in position['fill_details']:
            fills.append({**fill, 'market_id': position['market_id'], 'timestamp_ms': fill.get('fill_timestamp_ms'),
                          'notional': str(dec(fill['price'])*dec(fill['quantity']))})
    generation = '|'.join(attribution['source_code_shas'])
    def add(key, count, denominator=None, observations=None, scope=generation):
        metrics[key] = quality_metric(count, denominator, observations=observations, scope=scope, previous=previous.get(key))
    for key, field in [('missing_decision_probability','probability'), ('missing_arrival_probability','arrival_probability'),
                       ('missing_model_identity','model_hash'), ('missing_feature_vector','placement_features')]:
        relevant = [f for f in fills if field != 'placement_features' or f.get('placement_action') is not None]
        affected = [f for f in relevant if f.get(field) is None]
        add(key, len(affected), len(relevant), affected)
    for horizon in (1,5,10,30):
        cutoff_ms=attribution['recorded_at_ns']//1_000_000
        relevant = [f for f in fills if f.get('placement_action') is not None
                    and isinstance(f.get('fill_timestamp_ms'),(int,float)) and f['fill_timestamp_ms']+horizon*1000<=cutoff_ms]
        affected = [f for f in relevant if not any(dec((m.get('markouts') or {}).get(str(horizon)+'s')) is not None for m in f.get('markouts', []))]
        add(f'missing_maker_markout_{horizon}s', len(affected), len(relevant), affected)
    add('unreconciled_positions', attribution['canonical_final_positions']-attribution['reconciled_positions'],
        attribution['canonical_final_positions'])
    for cohort in cohorts:
        prefix = cohort['manifest_sha256']; total = sum(cohort.get('censored_labels', {}).values())
        # Categories overlap different experimental units. No fabricated common denominator.
        add('experiment_censored_labels|'+prefix, total, scope=prefix)
        for category, count in cohort.get('maker_coverage', {}).items():
            add('maker_arm_outcome|'+prefix+'|'+category, count, scope=prefix)
        add('unresolved_selected_contracts|'+prefix, cohort['selected_contracts']-cohort['resolved_selected_contracts'],
            cohort['selected_contracts'], scope=prefix)
    for name, value in statuses.items():
        session = str(value.get('observer_session_id') or value.get('collector_session_id') or '')
        scope = name+'|'+str(value.get('model_sha') or value.get('code_sha') or '')+'|'+session
        fields = ('feed_messages','raw_last_trade_events','valid_trade_prints','book_events_written','dropped_events',
                  'decoder_failures','missing_side','missing_size','invalid_quantity','invalid_price','invalid_timestamp',
                  'unknown_asset','feed_reconnects','gaps','reconnects','book_timeline_gaps','book_gap_censors',
                  'labels','late_labels','nominal_horizon_eligible_labels','invalid_reads')
        for field in fields:
            if field not in value: continue
            denominator = value.get('raw_last_trade_events') if field in {'valid_trade_prints','missing_side','missing_size','invalid_quantity','invalid_price','invalid_timestamp'} else value.get('labels') if field in {'late_labels','book_gap_censors','nominal_horizon_eligible_labels'} else None
            add(name+'|'+field, value[field], denominator, scope=scope if session else scope+'|UNVERIFIED_SESSION|'+str(value.get('timestamp_ns') or value.get('timestamp_ms')))
    for code, coverage in attribution.get('learning_coverage_by_sha', {}).items():
        add('incomplete_maker_feature_vectors|'+code, coverage['orders']-coverage['complete_vectors'], coverage['orders'], scope=code)
        for field, count in coverage.get('missing_fields_overlapping', {}).items():
            add('missing_maker_feature|'+code+'|'+field, count, coverage['orders'], scope=code)
    return {'schema':'polymarket_v7_economic_data_quality_v1','metrics':metrics,
            'unexplained_pnl_usd':attribution['unattributed_ledger_pnl'],
            'limitations':['Fill metrics cover canonical closed positions in the supplied ledger prefix.',
                'Source status counters lack event-level contract/notional/first-occurrence attribution; those fields remain unknown.',
                'Counter reset or unknown session identity suppresses trend deltas. Censor categories are not pooled as independent contracts.']}


def diagnose(attribution, experiments, statuses, benchmark=None, previous=None):
    if any(attribution.get(k) != v for k,v in AUTH.items()): raise ValueError('unsafe attribution authority')
    cohorts = experiments.get('cohorts', [experiments] if experiments.get('manifest_sha256') else [])
    positions = attribution['positions']; components = defaultdict(list); markouts = defaultdict(list)
    total=money_sum(p.get('ledger_final_pnl') for p in positions)
    if (total is None or dec(total)!=dec(attribution.get('canonical_final_pnl'))
            or len(positions)!=int(attribution.get('canonical_final_positions',-1))):
        raise ValueError('attribution position population does not reconcile to supplied canonical cash total')
    for position in positions:
        components[position['component']].append(position)
        for fill in position['fill_details']:
            for mark in fill.get('markouts', []):
                for horizon, value in (mark.get('markouts') or {}).items():
                    if dec(value) is not None:
                        markouts[(position['component'],horizon)].append((dec(fill['quantity'])*dec(value),position['market_id']))
    accounting = []
    for component, values in components.items():
        accounting.append({'component':component,'positions':len(values),
            'contracts':len({v['market_id'] for v in values}),
            'net_pnl_usd':money_sum(v['ledger_final_pnl'] for v in values),
            'gross_pnl_usd':money_sum(v['gross_trading_pnl'] for v in values),
            'costs_usd':money_sum(v['costs'] for v in values),
            'predicted_margin_usd':money_sum(v.get('predicted_margin') for v in values),
            'outcome_surprise_usd':money_sum(v.get('outcome_surprise') for v in values)})
    accounting.sort(key=lambda row:dec(row['net_pnl_usd']))
    gross = money_sum(p['gross_trading_pnl'] for p in positions); costs = money_sum(p['costs'] for p in positions)
    forecasts = []; delays = []; diagnostics = []
    for cohort in cohorts:
        signal = cohort.get('signal_analysis', {}); cells = signal.get('cells', {}).get('ALL', {})
        brier = cells.get('brier_improvement_over_pm', {})
        forecasts.append({'manifest_sha256':cohort['manifest_sha256'],'model_hash':cohort['settlement_model_hash'],
            'protocol_id':cohort['protocol_id'],'registration':signal.get('registration_status'),
            'brier_improvement_over_pm':brier,'log_loss_improvement_over_pm':cells.get('log_loss_improvement_over_pm'),
            'calibration':signal.get('calibration_fixed_deciles'),
            'ranked_fixed_regions':ranked_regions(signal),
            'confirmatory_status':{k:(cohort.get('confirmatory') or {}).get(k) for k in
                ('state','final_analysis_state','final_sha256','subsequent_window_input_changed','automatic_promotion')},
            'settlement_surplus_cost2_delay1000':cells.get('settlement_surplus_delay1000_cost2')})
        delays.append({'manifest_sha256':cohort['manifest_sha256'],
            'statistics':signal.get('fixed_signal_delay',{}).get('ALL',{}),
            'interpretation':signal.get('clock_interpretation')})
        if brier.get('mean') is not None:
            diagnostics.append({'question':'FORECAST_INCREMENT_OVER_PM','cohort':cohort['manifest_sha256'],
                'finding':'NO_DESCRIPTIVE_INCREMENT' if brier['mean']<=0 else 'POSITIVE_DESCRIPTIVE_INCREMENT',
                'contracts':brier['contracts'],'value':brier['mean'],
                'causal_identification':'NOT_IDENTIFIED; SELECTED_ORIGINS_AND_TEMPORAL_UNCERTAINTY',
                'expected_intervention_gain_usd':None})
    diagnostics.insert(0,{'question':'WHERE_CANONICAL_LOSSES_OCCUR','finding':accounting[0]['component'] if accounting and dec(accounting[0]['net_pnl_usd'])<0 else 'NO_NEGATIVE_COMPONENT',
        'evidence':'EXACT_CANONICAL_ACCOUNTING','causal_identification':'ACCOUNTING_LOCATION_ONLY',
        'expected_intervention_gain_usd':None})
    diagnostics.append({'question':'FEES_AS_SOLE_CAUSE','finding':'RULED_OUT_IN_THIS_SAMPLE' if gross is not None and dec(gross)<0 else 'NOT_RULED_OUT',
        'gross_pnl_usd':gross,'costs_usd':costs,'causal_identification':'REMOVING_RECORDED_COSTS_ALONE_CANNOT_FIX_NEGATIVE_GROSS_PNL'})
    quality = scorecard(attribution,cohorts,statuses,(previous or {}).get('data_quality',{}).get('metrics'))
    outcomes=Counter()
    for counts in attribution['maker_outcomes_by_sha'].values():outcomes.update(counts)
    maker_accounting=[r for r in accounting if r['component']=='professional_maker']
    maker_causes=[
        {'category':'FORECAST_FAILURE','evidence':maker_accounting,'identification':'ACCOUNTING_SURPRISE_AND_SEPARATE_MODEL_VS_PM; NOT_CAUSAL'},
        {'category':'PLACEMENT_FAILURE','orders_price_not_reached':outcomes['PRICE_NOT_REACHED'],'identification':'OBSERVED_REACH_FAILURE; ALTERNATIVE_PLACEMENT_REQUIRES_PAIRED_ARMS'},
        {'category':'ADVERSE_SELECTION','negative_markout_observations':sum(v<0 for (c,_),vs in markouts.items() if c=='professional_maker' for v,_ in vs),
         'identification':'FILL_CONDITIONED_MARKS; CANNOT_SEPARATE_SELECTION_FROM_FORECAST_OR_COMMON_PRICE_MOVEMENT'},
        {'category':'QUEUE_FAILURE','orders_queue_not_depleted':outcomes['QUEUE_NOT_DEPLETED'],'identification':'OBSERVED_ORDER_OUTCOME; NOT_INDEPENDENT_OPPORTUNITIES'},
        {'category':'FLOW_FAILURE','orders_without_opposite_flow':outcomes['NO_OPPOSITE_FLOW'],'identification':'OBSERVED_ORDER_OUTCOME; NOT_INDEPENDENT_OPPORTUNITIES'},
        {'category':'LIFETIME_FAILURE','paired_cohorts':[{'manifest_sha256':c['manifest_sha256'],'comparisons':c.get('maker_paired_net_delta_vs_join5s',{})} for c in cohorts],
         'identification':'NATIVE_COUNTERFACTUAL_PAIRS; REPORT_CENSORS_AND_TEMPORAL_INTERVAL_LIMITS'},
        {'category':'TIMING_FAILURE','identified_economic_gain_usd':None,'identification':'ENTRY_TIMING_INTERVENTION_NOT_IDENTIFIED'},
        {'category':'ECONOMIC_OBJECTIVE_MISMATCH','observed_settlement_pnl_usd':money_sum(r['net_pnl_usd'] for r in maker_accounting),
         'identification':'SHORT_MARKOUT_AND_FINAL_SETTLEMENT_ARE_DIFFERENT_TARGETS; DO_NOT_ADD_MARKOUT_TO_CASH'}]
    loss_component=accounting[0]['component'] if accounting and dec(accounting[0]['net_pnl_usd'])<0 else None
    censored_maker=sum(n for c in cohorts for key,n in c.get('maker_coverage',{}).items() if 'CENSORED' in key or 'STALE_OR_INCOMPLETE' in key)
    if dec(attribution['canonical_final_pnl'])<0 and dec(gross)>=0:
        next_test='FORWARD_COST_STRESS_WITH_FIXED_SIGNAL_AND_OBSERVED_PRICES'
        rationale='The recorded cost debit changes gross-positive accounting into net-negative accounting; test net surplus prospectively.'
    elif loss_component=='professional_maker' and censored_maker:
        next_test='PRESERVE_AND_COMPARE_PREDECLARED_MAKER_EXECUTION_WITH_SEPARATE_MARKOUTS'
        rationale='Maker has the largest observed loss contribution and censored paired comparisons limit identification of a policy change.'
    elif loss_component:
        next_test='FORWARD_PM_VS_FROZEN_MODEL_AND_FIXED_SIGNAL_DELAY_ENDPOINTS'
        rationale='The largest losing component requires prospective forecast and net-surplus evidence before selecting a model or timing intervention.'
    else:
        next_test='CONTINUE_FROZEN_FORWARD_PROTOCOL_WITHOUT_POLICY_PROMOTION'
        rationale='No negative component contribution is identified in this prefix; descriptive results alone cannot justify promotion.'
    now_ms=time.time_ns()//1_000_000
    return {'schema':'polymarket_v7_economic_decision_report_v1',**AUTH,'timestamp_ms':now_ms,
        'state':'STALE_CANONICAL_INPUT' if now_ms-attribution['recorded_at_ns']//1_000_000>300000 else 'CURRENT_CANONICAL_PREFIX',
        'canonical':{'net_pnl_usd':attribution['canonical_final_pnl'],'positions':attribution['canonical_final_positions'],
            'evidence_recorded_at_ns':attribution['recorded_at_ns'],
            'reconciled_positions':attribution['reconciled_positions'],'unexplained_pnl_usd':attribution['unattributed_ledger_pnl'],
            'gross_pnl_usd':gross,'costs_usd':costs,'components_ranked_by_realized_loss':accounting,
            'component_probe_model_strata':attribution['strata_by_component_probe_model']},
        'forecast':forecasts,'fixed_signal_delay':delays,'opportunity_funnel':attribution['opportunity_funnel'],
        'maker_outcomes_by_sha':attribution['maker_outcomes_by_sha'],
        'maker_profit_causes':maker_causes,
        'maker_markouts':[{'component':c,'horizon':h,'filled_quantity_times_price_change_usd':str(sum((v for v,_ in values),ZERO)),
            'observations':len(values),'contracts':len({m for _,m in values}),
            'scope':'FILL_CONDITIONED_PRICE_DIAGNOSTIC; NOT_CANONICAL_CASH_OR_CAUSAL_ADVERSE_SELECTION'} for (c,h),values in sorted(markouts.items())],
        'diagnosis':diagnostics,'data_quality':quality,'offline_benchmark':benchmark,
        'intervention_ranking':{'formula':'EXPECTED_ECONOMIC_IMPACT_TIMES_CONFIDENCE_DIVIDED_BY_IMPLEMENTATION_RISK',
            'numerical_ranking_state':'NOT_IDENTIFIED; NO_INVENTED_EXPECTED_DOLLAR_GAINS',
            'provisional_next_test':next_test,'reason':rationale},
        'automatic_promotion':False}


def memo(report):
    c=report['canonical']; components=c['components_ranked_by_realized_loss']; largest=components[0] if components else None
    excluded=next(x['finding'] for x in report['diagnosis'] if x['question']=='FEES_AS_SOLE_CAUSE')
    ranking=report['intervention_ranking']
    return '\n'.join([
        '# NEXT_ECONOMIC_ACTION', '', 'Computed from the exact source hashes in the accompanying decision report. Source state: '+report['state']+'. This is not a profitability claim.', '',
        f"1. **Current loss location:** canonical PnL ${c['net_pnl_usd']} across {c['positions']} closed positions. Component contributions: "+'; '.join(f"{x['component']} ${x['net_pnl_usd']}" for x in components)+'. Accounting location is not causal attribution.',
        f"2. **Ruled out:** unexplained accounting residual is ${c['unexplained_pnl_usd']}; fees as the sole explanation: {excluded}. This does not rule out fees affecting individual trades.",
        '3. **Unidentified:** causal forecast error versus fill selection, the return from changing placement/queue/flow/lifetime/timing, and infrastructure latency value. Missing arrival probabilities, feature vectors and markouts remain visible in the scorecard.',
        f"4. **Largest observed dollar contribution:** {largest['component']+' $'+largest['net_pnl_usd'] if largest else 'UNKNOWN'}. This is realized exposure, not an estimated recoverable profit.",
        '5. **Best supported fixability evidence:** execution-window conservation is directly testable as an engineering change; its profit effect remains unknown. The computed research priority is '+ranking['provisional_next_test']+'.',
        '6. **Exact next test:** '+ranking['provisional_next_test']+'. Use the frozen protocol: JOIN 5s, flow-filtered JOIN 5s, JOIN 10s and post-only IMPROVE1 5s with common quantity caps, separate 1/5/10/30s markouts, plus PM/model and fixed-signal 0/100/250/500/1000ms cost-stress endpoints. Preserve native pessimistic queue/trade rules.',
        '7. **Why this priority:** '+ranking['reason']+' The nested benchmark and forward model-versus-PM results must support any later tuning decision; no offline candidate is promoted.',
        '8. **If tuning is later warranted:** target incremental settlement probability over PM using regularized PM-logit residuals and only causally observed oracle distance, TTE, external returns/dispersion/volatility and liquidity features. Missingness indicators and scaling must be learned only on training contracts. Do not select features on the audit window.',
        '9. **Forward proof:** freeze a new seven-calendar-day cohort before its first eligible contract; keep each contract in one temporal partition; evaluate the three predeclared primary endpoints and report every censor/exclusion.',
        '10. **Promotion threshold:** positive lower familywise-adjusted bounds for model-versus-PM Brier improvement, selected surplus at 2x costs and 1000ms, and paired JOIN10-minus-JOIN5 net settlement; require valid temporal-block sensitivity, adequate tail resolution, causal completeness, and independent review. No automatic promotion.',
        '11. **Do not change yet:** capital, sizing, risk gates, probability bounds, execution optimism or real-money authority. Do not deploy an offline winner directly.', '',
        'Expected-impact × confidence ÷ implementation-risk scores remain unidentified where counterfactual gains are unknown. The next test is an evidence-based research priority, not a fabricated numeric optimization.', ''])


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run-root',type=Path,required=True)
    parser.add_argument('--durable-root',type=Path,required=True);parser.add_argument('--attribution',type=Path)
    parser.add_argument('--experiments',type=Path);parser.add_argument('--benchmark',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();root=args.run_root;output=args.output or root/'economic_decision_report.json'
    sources={}
    def read(name,path,required=False):
        if not path.exists():
            if required:raise ValueError('missing required source '+name)
            return {}
        raw=path.read_bytes();sources[name]={'path':str(path),'sha256':digest(raw)};return json.loads(raw)
    previous=json.loads(output.read_text()) if output.exists() else {}
    attribution=read('attribution',args.attribution or root/'profit_attribution.json',True)
    experiments=read('experiments',args.experiments or root/'profit_experiment_report.json',True)
    statuses={name:read(name,root/path) for name,path in [('book_feed','micro_maker/fillability_ws_status.json'),
        ('lead_lag','external_fair/lead_lag_collector_status.json'),('oracle','external_fair/oracle_status.json')]}
    benchmark=read('benchmark',args.benchmark) if args.benchmark else None
    result=diagnose(attribution,experiments,statuses,benchmark,previous);result['sources']=sources;result['implementation_sha256']=CODE_HASH
    result['data_quality']['spool_backlog_files']=sum(1 for p in (root/'ledger/spool').glob('*.json') if p.is_file())
    atomic(output,result);memo_path=output.parent/'NEXT_ECONOMIC_ACTION.md'
    temporary=memo_path.with_suffix('.md.tmp');temporary.write_text(memo(result));temporary.replace(memo_path)
    permanent=args.durable_root/'permanent_evidence/decision_reports';encoded=canonical(result)
    # At most one compact diagnostic history point per ten-minute bucket.
    bucket=str(result['timestamp_ms']//600000);history=permanent/'history'/(bucket+'.json.gz')
    immutable(permanent/'implementation_sources'/(CODE_HASH+'.py.gz'),gzip.compress(CODE,mtime=0))
    if not history.exists():immutable(history,gzip.compress(encoded,mtime=0))
    print(json.dumps({'positions':result['canonical']['positions'],'pnl':result['canonical']['net_pnl_usd'],'output':str(output)}))


if __name__=='__main__':main()
