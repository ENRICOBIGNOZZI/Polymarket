#!/usr/bin/env python3
"""Prospective research results. Public settlement labels; no portfolio mutation."""
from __future__ import annotations
import argparse
import copy
from collections import Counter,defaultdict
import json
import math
import gzip
import fcntl
import hashlib
from pathlib import Path
import time
import urllib.request
import urllib.parse
from v7_profit_protocol import digest,fixed_window_digest
from v7_profit_experiments import AUTH,rows,atomic,finite
from v7_profit_signal_analysis import summarize_signal,confirmatory
from v7_evidence_store import canonical,immutable

ANALYSIS_BYTES={name:(Path(__file__).parent/name).read_bytes() for name in
    ('v7_profit_report.py','v7_profit_signal_analysis.py','v7_profit_inference.py','v7_profit_protocol.py')}


def settlement(market,tokens,fetch):
    endpoint='https://gamma-api.polymarket.com/markets/'+urllib.parse.quote(market,safe='')
    raw=fetch(endpoint)
    parse=lambda x:json.loads(x) if isinstance(x,str) else x
    actual_tokens=parse(raw.get('clobTokenIds'));prices=parse(raw.get('outcomePrices'))
    if (raw.get('closed') is not True or str(raw.get('id'))!=market or not isinstance(actual_tokens,list)
        or len(actual_tokens)!=2 or not isinstance(prices,list) or len(prices)!=2 or not set(tokens)<=set(actual_tokens)):return None
    try:prices=[float(x) for x in prices]
    except (TypeError,ValueError):return None
    if sorted(prices)!=[0.,1.]:return None
    return {'market_id':market,'winning_token_id':str(actual_tokens[prices.index(1.)]),'tokens':actual_tokens,'prices':prices,
            'settlement_closed':True,'source_endpoint':endpoint,'observed_ms':time.time_ns()//1000000,'source_sha256':digest(raw),'raw_response':raw}


from v7_profit_inference import interval, describe


def frozen_execution_summary(observations,protocol):
    """Expose sealed execution even when a later comparison never completes.

    This is a separate descriptive population, never an implicit substitute for
    the paired final population or a source of confirmatory endpoint values.
    """
    windows={};marks={};completed=set();anchors={}
    for row in observations:
        kind=row['kind'];key=row.get('anchor_record_id')
        if kind=='MAKER_ANCHOR':
            key=row['order']['record_id']
            if key in anchors and anchors[key]!=row:raise ValueError('conflicting Maker anchor')
            anchors[key]=row
        elif kind=='MAKER_EXECUTION_WINDOW':
            for arm in row['arms']:
                identity=(key,arm['arm'])
                if identity in windows and windows[identity]!=(row,arm):
                    raise ValueError('conflicting frozen Maker execution')
                windows[identity]=(row,arm)
        elif kind=='MAKER_MARKOUT_LABEL':
            identity=(key,row['arm'],row['fill_index'],row['horizon_ms'])
            if identity in marks and marks[identity]!=row:raise ValueError('conflicting Maker markout')
            marks[identity]=row
        elif kind=='MAKER_COMPARISON':completed.add(key)
    counts=Counter();mark_counts=Counter();quantities=defaultdict(lambda:defaultdict(list));missing_anchors=set()
    for (key,aid),(row,arm) in windows.items():
        anchor=anchors.get(key)
        if anchor is None:
            missing_anchors.add(key);continue
        if any(anchor[k]!=row[k] for k in ('market_id','token_id')):
            raise ValueError('frozen Maker anchor identity mismatch')
        counts[aid+'|'+arm['state']]+=1
        if arm['state'] not in ('OBSERVED','FLOW_FILTER_ABSTAIN'):continue
        quantities[aid][row['market_id']].append(arm['operational_filled_shares'])
        for i,fill in enumerate(arm['fills']):
            for horizon in protocol['maker']['markout_horizons_ms']:
                mark=marks.get((key,aid,i,horizon))
                if mark and any(mark[k]!=row[k] for k in ('market_id','token_id')):
                    raise ValueError('frozen Maker markout identity mismatch')
                # Absence is unknown, including after a cutover; it is neither
                # a zero return nor proof of a permanent transport censor.
                state=mark['state'] if mark else 'NOT_RECORDED'
                if mark and ((state=='OBSERVED')!=(mark.get('markout') is not None)):
                    raise ValueError('inconsistent frozen Maker markout state')
                mark_counts[f'{aid}|{horizon}ms|{state}']+=1
    for row in observations:
        if row['kind']!='MAKER_COMPARISON':continue
        for arm in row['arms']:
            frozen=windows.get((row.get('anchor_record_id'),arm['arm']))
            if frozen is None:continue
            if any(row[k]!=frozen[0][k] for k in ('market_id','token_id')):
                raise ValueError('completed Maker identity differs from frozen execution')
            def execution_only(value):
                value=copy.deepcopy(value)
                for fill in value['fills']:
                    fill.pop('markouts',None);fill.pop('markout_states',None)
                return value
            if execution_only(arm)!=execution_only(frozen[1]):
                raise ValueError('completed Maker comparison changed frozen execution')
    observed_anchors={key for key,_ in windows if key in anchors}
    return {'scope':'DESCRIPTIVE_FROZEN_EXECUTION_INDEPENDENT_OF_FINAL_COMPARISON',
        'anchors_with_frozen_execution':len(observed_anchors),
        'anchors_without_final_comparison':len(observed_anchors-completed),
        'unmatched_anchor_ids':sorted(missing_anchors),'execution_coverage':dict(counts),
        'markout_coverage':dict(mark_counts),
        'filled_quantity_by_contract':{aid:describe([sum(v)/len(v) for v in contracts.values()])
                                       for aid,contracts in quantities.items()},
        'confirmatory_endpoint_source':False}


def summarize(observations,manifest,settlements,*,final_look=False,complete_primary_coverage=False):
    protocol=manifest['protocol'];config=protocol['signal'];selections={};delays={};makers=[];counts=Counter();censors=Counter();maker_times={};maker_seen={}
    for row in observations:
        if row.get('manifest_sha256')!=manifest['manifest_sha256'] or row.get('code_sha')!=manifest['code_sha']:
            raise ValueError('mixed experiment identity')
        if any(row.get(k)!=v for k,v in AUTH.items()):raise ValueError('research authority mismatch')
        counts[row['kind']]+=1
        if row['kind']=='SIGNAL_SELECTION':
            if row['origin_ns']<manifest['forward_start_ns']:raise ValueError('pre-registration contamination')
            if row.get('model_hash')!=manifest['frozen_model_hash']:raise ValueError('selection model identity mismatch')
            key=row['selection_key']
            if key in selections and row!=selections[key]:raise ValueError('conflicting selection')
            selections[key]=row
        elif row['kind']=='DELAY_LABEL':
            key=(row['selection_key'],row['delay_ms'])
            if key in delays and row!=delays[key]:raise ValueError('conflicting delay label')
            delays[key]=row
            if row['state']!='OBSERVED':censors[row['state']]+=1
        elif row['kind']=='MAKER_ANCHOR':
            maker_times[row['market_id']]=row.get('origin_ms',row.get('recorded_ns',0)/1e6)*1_000_000
        elif row['kind']=='MAKER_COMPARISON':
            key=row.get('anchor_record_id') or row['market_id']
            if key in maker_seen and row!=maker_seen[key]:raise ValueError('conflicting Maker comparison')
            if key not in maker_seen:makers.append(row);maker_seen[key]=row
    grouped=defaultdict(lambda:defaultdict(list));decision_decay=defaultdict(lambda:defaultdict(list))
    resolved=set()
    # Family includes all prespecified bins, outcomes, delays, cost stresses and Maker comparisons.
    family=2*(len(config['margin_edges'])-1)*(len(config['tte_edges_seconds'])-1)*(1+len(config['delays_ms'])*len(config['cost_stress_multipliers']))+16
    for key,row in sorted(selections.items(),key=lambda x:x[1]['origin_ns']):
        market=row['market_id'];outcome=settlements.get(market)
        prefix=f"{row['outcome']}|margin{row['margin_bin']}|tte{row['tte_bin']}"
        if outcome and row['token_id'] in outcome['tokens']:
            y=float(outcome['winning_token_id']==row['token_id']);resolved.add(market)
            grouped[prefix+'|brier_improvement_over_pm'][market].append((row['pm_probability']-y)**2-(row['model_probability']-y)**2)
        else:y=None
        initial=delays.get((key,0))
        for delay in config['delays_ms']:
            label=delays.get((key,delay))
            if not label or label['state']!='OBSERVED':continue
            if initial and initial['state']=='OBSERVED':
                decision_decay[str(delay)][market].append(label['point_net_margin']-initial['point_net_margin'])
            if y is None:continue
            for stress in config['cost_stress_multipliers']:
                net=y-label['book_cut']['best_ask']-stress*(label['fee_per_share']+label['risk_allowance_per_share'])
                grouped[prefix+f'|delay{delay}|cost{stress}'][market].append(net)
    # Include empty fixed bins, avoiding retrospective winner-only reporting.
    for outcome in ('YES','NO'):
        for mi in range(len(config['margin_edges'])-1):
            for ti in range(len(config['tte_edges_seconds'])-1):
                prefix=f'{outcome}|margin{mi}|tte{ti}'
                grouped[prefix+'|brier_improvement_over_pm']
                for delay in config['delays_ms']:
                    for stress in config['cost_stress_multipliers']:grouped[prefix+f'|delay{delay}|cost{stress}']
    estimates={key:interval([sum(v)/len(v) for v in contracts.values()],protocol,family) for key,contracts in grouped.items()}
    maker_groups=defaultdict(lambda:defaultdict(list));maker_coverage=Counter();markout_coverage=Counter();paired=defaultdict(lambda:defaultdict(list));primary_maker=defaultdict(list)
    for row in sorted(makers,key=lambda r:maker_times.get(r['market_id'],r.get('recorded_ns',0))):
        outcome=settlements.get(row['market_id']);arm_pnl={};arm_net={}
        for arm in row['arms']:
            aid=arm['arm'];maker_coverage[aid+'|'+arm['state']]+=1
            if arm['state'] not in ('OBSERVED','FLOW_FILTER_ABSTAIN'):continue
            qty=arm['operational_filled_shares'];maker_groups[aid+'|filled_quantity'][row['market_id']].append(qty)
            maker_groups[aid+'|any_operational_fill'][row['market_id']].append(float(qty>0))
            common=arm.get('common_quote_quantity') or (arm.get('research_request') or {}).get('quantity')
            if common and common>0:maker_groups[aid+'|filled_fraction'][row['market_id']].append(qty/common)
            start=(arm.get('research_request') or {}).get('start_ns')
            if start and arm['fills']:
                maker_groups[aid+'|first_fill_ms'][row['market_id']].append((min(f['receive_monotonic_ns'] for f in arm['fills'])-start)/1e6)
            for horizon in protocol['maker']['markout_horizons_ms']:
                cuts=[(f['quantity'],(f.get('markouts') or {}).get(str(horizon))) for f in arm['fills']]
                for _,cut in cuts:markout_coverage[aid+f'|{horizon}ms|'+('OBSERVED' if cut else 'BOOK_AT_MARKOUT_CENSORED')]+=1
                if qty>0 and cuts and all(v is not None for _,v in cuts):
                    maker_groups[aid+f'|mid_markout_{horizon}ms'][row['market_id']].append(sum(q*v['mid_minus_fill'] for q,v in cuts)/qty)
            if not outcome or row['token_id'] not in outcome['tokens']:continue
            y=float(outcome['winning_token_id']==row['token_id'])
            pnl=sum(f['quantity']*(y-f['price']) for f in arm['fills']);arm_pnl[aid]=pnl
            arm_net[aid]=pnl-2*qty*config['execution_risk_per_share']
            # Maker entry fee zero. Stress includes declared risk allowance and
            # is a research sensitivity, not an extra realized ledger debit.
            for stress in config['cost_stress_multipliers']:
                maker_groups[aid+f'|settlement_net_cost{stress}'][row['market_id']].append(pnl-stress*qty*config['execution_risk_per_share'])
        if 'JOIN_5S' in arm_pnl:
            for aid,pnl in arm_pnl.items():
                if aid!='JOIN_5S':
                    paired[aid+'|gross'][row['market_id']].append(pnl-arm_pnl['JOIN_5S'])
                    paired[aid+'|net_cost2'][row['market_id']].append(arm_net[aid]-arm_net['JOIN_5S'])
            origin=maker_times.get(row['market_id'])
            if 'JOIN_10S' in arm_net and origin and manifest['forward_start_ns']<=origin<manifest.get('confirmatory_end_ns',0):
                primary_maker[row['market_id']].append(arm_net['JOIN_10S']-arm_net['JOIN_5S'])
    signal=summarize_signal(selections,delays,manifest,settlements,time.time_ns())
    primary=signal.pop('primary_contract_values');times=signal.pop('contract_origin_ns')
    for market,stamp in maker_times.items():times[market]=min(times.get(market,stamp),stamp)
    primary['maker_join10_minus_join5_settlement_net_cost2']={m:sum(v)/len(v) for m,v in primary_maker.items()}
    return {'schema':'polymarket_v7_profit_experiment_report_v2',**AUTH,'code_sha':manifest['code_sha'],
        'settlement_model_hash':manifest['frozen_model_hash'],'protocol_id':protocol['protocol_id'],
        'manifest_sha256':manifest['manifest_sha256'],'timestamp_ms':time.time_ns()//1000000,'counts':dict(counts),
        'selected_contracts':len({r['market_id'] for r in selections.values()}),'resolved_selected_contracts':len(resolved),
        'signal_cells':estimates,'fixed_signal_delay_margin_change':{k:interval([sum(v)/len(v) for v in c.values()],protocol,family) for k,c in decision_decay.items()},
        'maker_coverage':dict(maker_coverage),'maker_markout_coverage':dict(markout_coverage),
        'maker_frozen_execution':frozen_execution_summary(observations,protocol),
        'maker_metrics':{k:interval([sum(v)/len(v) for v in c.values()],protocol,family) for k,c in maker_groups.items()},
        'maker_paired_net_delta_vs_join5s':{k:interval([sum(v)/len(v) for v in c.values()],protocol,family) for k,c in paired.items()},
        'signal_analysis':signal,'confirmatory':confirmatory(primary,times,manifest,time.time_ns(),
            final_look=final_look,complete_primary_coverage=complete_primary_coverage),
        'censored_labels':dict(censors),'economic_conclusion':'FORWARD_RESEARCH_NO_PROFITABILITY_CLAIM_OR_POLICY_PROMOTION',
        'limitations':['L1 prices and aggregate features, not full depth or queue position verification.',
            'Native PAPER fills in these comparisons are counterfactual and excluded from canonical equity.',
            'One normalized share is a price diagnostic, not proof of venue minimum-size executability.',
            'All repeats within a contract are aggregated before uncertainty estimation.',
            'Markout is fill-conditioned; settlement surplus and cost stress are separate quantities.',
            'No probability-bound narrowing, capital increase or model promotion.']}


def final_window_audit(observations,manifest,labels,now_ns,closure=None):
    """Count the entire fixed selection population, including terminal censors."""
    start=manifest['forward_start_ns'];end=manifest['confirmatory_end_ns']
    selected={r['selection_key']:r for r in observations if r['kind']=='SIGNAL_SELECTION' and start<=r['origin_ns']<end}
    anchors={r['order']['record_id']:r for r in observations if r['kind']=='MAKER_ANCHOR' and start<=r['origin_ms']*1e6<end}
    window=[];delays={};comparisons={};markets=defaultdict(set);origin_times={}
    for r in list(selected.values())+list(anchors.values()):
        market=r['market_id'];markets[market].add(r['token_id'])
        stamp=r.get('origin_ns',r.get('origin_ms',0)*1e6)
        origin_times[market]=max(origin_times.get(market,0),stamp)
    for r in observations:
        kind=r['kind'];include=False
        if kind=='SIGNAL_SELECTION':include=r['selection_key'] in selected
        elif kind=='DELAY_LABEL':
            include=r['selection_key'] in selected
            if include:delays[(r['selection_key'],r['delay_ms'])]=r
        elif kind=='MAKER_ANCHOR':include=r['order']['record_id'] in anchors
        elif kind.startswith('MAKER_'):
            include=r.get('anchor_record_id') in anchors
            if include and kind=='MAKER_COMPARISON':comparisons[r['anchor_record_id']]=r
        if include:window.append(r)
    missing_delays=[(key,d) for key in selected for d in manifest['protocol']['signal']['delays_ms'] if (key,d) not in delays]
    missing_maker=sorted(set(anchors)-set(comparisons));missing_labels=[];invalid_labels=[]
    for market,tokens in markets.items():
        label=labels.get(market)
        if not label:missing_labels.append(market);continue
        raw=label.get('raw_response')
        try:
            verified=settlement(market,tokens,lambda _:raw)
            valid=(verified is not None and label.get('source_sha256')==digest(raw)
                and all(label.get(k)==verified[k] for k in ('market_id','winning_token_id','tokens','prices','settlement_closed'))
                and label.get('observed_ms',0)*1e6>=origin_times[market])
        except (TypeError,ValueError,AttributeError):valid=False
        if not valid:invalid_labels.append(market)
    signal_censors=sum(delays.get((key,1000),{}).get('state')!='OBSERVED' for key in selected)
    maker_censors=0
    for row in comparisons.values():
        arms={a['arm']:a for a in row['arms']}
        maker_censors+=any(arms.get(a,{}).get('state') not in ('OBSERVED','FLOW_FILTER_ABSTAIN')
            for a in ('JOIN_5S','JOIN_10S'))
    closure=closure or {}
    closure_valid=(closure.get('schema')=='polymarket_v7_confirmatory_window_closure_v1'
        and closure.get('manifest_sha256')==manifest['manifest_sha256']
        and all(closure.get(k)==v for k,v in AUTH.items())
        and closure.get('forward_end_ns')==end and end<=closure.get('closed_at_ns',0)<=now_ns
        and closure.get('pending_window_signals')==0 and closure.get('pending_window_maker_anchors')==0
        and closure.get('closure_sha256')==digest({k:v for k,v in closure.items() if k!='closure_sha256'})
        and closure.get('window_observations_sha256')==fixed_window_digest(observations,manifest))
    ready=now_ns>=end and closure_valid and not (missing_delays or missing_maker or missing_labels or invalid_labels)
    scope_labels={m:labels[m] for m in sorted(markets) if m in labels}
    scope={'window_observation_hashes':sorted(digest(r) for r in window),'settlements':scope_labels}
    return {'window_ended':now_ns>=end,'terminal_records_and_verified_settlements_complete':ready,
        'producer_window_closure_verified':closure_valid,'producer_window_closure':closure,
        'selected_origins':len(selected),'maker_anchors':len(anchors),'contracts':len(markets),
        'missing_delay_labels':missing_delays,'missing_maker_comparisons':missing_maker,
        'missing_settlement_contracts':sorted(missing_labels),'invalid_settlement_contracts':sorted(invalid_labels),
        'signal_primary_censored_origins':signal_censors,'maker_primary_censored_anchors':maker_censors,
        'complete_primary_causal_coverage':ready and signal_censors==0 and maker_censors==0,
        'scope_sha256':digest(scope),'scope':scope,
        'censoring_policy':'TERMINAL_CENSORS_ARE_PRESERVED; NO_CONFIRMATORY_INTERVAL_IF_ANY_PRIMARY_ORIGIN_IS_CENSORED'}


def read_final(path,manifest):
    value=json.loads(path.read_text())
    if (value.get('manifest_sha256')!=manifest['manifest_sha256']
            or value.get('final_sha256')!=digest({k:v for k,v in value.items() if k!='final_sha256'})):
        raise ValueError('immutable confirmatory final checksum or cohort mismatch')
    return value


def report_cohort(root, *, fetch_labels=True, request_budget=None):
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text())
    if digest({k:v for k,v in manifest.items() if k!='manifest_sha256'})!=manifest['manifest_sha256']:
        raise ValueError('experiment manifest checksum mismatch')
    observations=list(rows(root/'observations.jsonl'));token_sets=defaultdict(set)
    for row in observations:
        if row.get('token_id'):token_sets[row['market_id']].add(row['token_id'])
    cache=root/'settlements.json';labels=json.loads(cache.read_text()) if cache.exists() else {}
    def fetch(url):
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'polymarket-v7-paper-research'}),timeout=2) as response:return json.load(response)
    budget=request_budget if request_budget is not None else [12]
    if fetch_labels:
        for market in [k for k in token_sets if k not in labels]:
            if budget[0]<=0:break
            budget[0]-=1
            try:label=settlement(market,token_sets[market],fetch)
            except (OSError,ValueError,TypeError):continue
            if label:labels[market]=label
        atomic(cache,labels)
    report=summarize(observations,manifest,labels)
    if manifest['protocol']['inference'].get('confirmatory'):
        closure_path=root/'confirmatory_window_closure.json'
        closure=json.loads(closure_path.read_text()) if closure_path.exists() else None
        audit=final_window_audit(observations,manifest,labels,time.time_ns(),closure)
        final_path=root/'confirmatory_final.json';frozen=None
        if final_path.exists():frozen=read_final(final_path,manifest)
        elif fetch_labels and audit['terminal_records_and_verified_settlements_complete']:
            with (root/'.confirmatory_final.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                if final_path.exists():frozen=read_final(final_path,manifest)
                else:
                    result=summarize(observations,manifest,labels,final_look=True,
                        complete_primary_coverage=audit['complete_primary_causal_coverage'])['confirmatory']
                    implementations={}
                    for name,payload in ANALYSIS_BYTES.items():
                        sha=hashlib.sha256(payload).hexdigest();implementations[name]=sha
                        immutable(root/'final_analysis_sources'/(sha+'.py.gz'),gzip.compress(payload,mtime=0))
                    frozen={'schema':'polymarket_v7_immutable_confirmatory_final_v1',**AUTH,
                        'manifest_sha256':manifest['manifest_sha256'],'frozen_at_ns':time.time_ns(),
                        'coverage_audit':audit,'result':result,'implementation_sha256s':implementations}
                    frozen['final_sha256']=digest(frozen);immutable(final_path,canonical(frozen))
        if frozen:
            report['confirmatory']={**frozen['result'],'state':'IMMUTABLE_FINAL_PUBLISHED',
                'final_analysis_state':frozen['result']['state'],'final_sha256':frozen['final_sha256'],
                'frozen_at_ns':frozen['frozen_at_ns'],'coverage_audit':frozen['coverage_audit'],
                'subsequent_window_input_changed':audit['scope_sha256']!=frozen['coverage_audit']['scope_sha256'],
                'subsequent_changes_policy':'PRESERVE_FIRST_LOOK; LATER_DATA_IS_EXPLORATORY_AND_CANNOT_REPLACE_THE_FINAL'}
        else:report['confirmatory']['coverage_audit']=audit
    report['sources']={'observations_sha256':digest(observations),'settlements_sha256':digest(labels),'manifest':manifest}
    return report


def report_cohorts(root, *, fetch_labels=True):
    """Every immutable model/protocol generation remains individually visible."""
    root=Path(root);paths=sorted(root.rglob('manifest.json'));reports=[];excluded=[];seen=set();budget=[12]
    for path in paths:
        if not (path.parent/'observations.jsonl').exists():
            excluded.append({'path':str(path),'reason':'REGISTERED_WITHOUT_OBSERVATIONS_PRESERVED'});continue
        report=report_cohort(path.parent,fetch_labels=fetch_labels,request_budget=budget)
        identity=report['manifest_sha256']
        if identity in seen:
            previous=next(r for r in reports if r['manifest_sha256']==identity)
            if previous['sources']['observations_sha256']!=report['sources']['observations_sha256']:
                raise ValueError('duplicate cohort has conflicting observation prefixes')
            continue
        seen.add(identity);reports.append(report)
    return {'schema':'polymarket_v7_permanent_profit_cohort_report_v1',**AUTH,
        'timestamp_ms':time.time_ns()//1_000_000,'cohort_count':len(reports),'cohorts':reports,'preserved_exclusions':excluded,
        'model_strata':sorted({r['settlement_model_hash'] for r in reports}),
        'protocol_strata':sorted({r['protocol_id'] for r in reports}),
        'aggregation_semantics':'INDEPENDENT_MODEL_PROTOCOL_COHORTS_NO_SILENT_POOLING',
        'economic_conclusion':'FORWARD_RESEARCH_NO_PROFITABILITY_CLAIM_OR_POLICY_PROMOTION'}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--experiment-root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--offline',action='store_true',help='Read frozen settlement cache; never write any source or request labels')
    ap.add_argument('--all-cohorts',action='store_true',help='Retain separate model/protocol strata recursively')
    args=ap.parse_args();root=args.experiment_root
    if args.all_cohorts or not (root/'manifest.json').exists():
        report=report_cohorts(root,fetch_labels=not args.offline)
    else:report=report_cohort(root,fetch_labels=not args.offline)
    atomic(args.output,report)

if __name__=='__main__':main()
