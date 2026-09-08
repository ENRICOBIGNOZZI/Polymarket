#!/usr/bin/env python3
"""Prospective research results. Public settlement labels; no portfolio mutation."""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import json
import math
from pathlib import Path
import time
import urllib.request
import urllib.parse
from v7_profit_protocol import digest
from v7_profit_experiments import AUTH,rows,atomic,finite


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


def interval(values,protocol,family):
    # Each value is already a within-contract mean, never a quote/attempt sample.
    import random
    n=len(values);result={'contracts':n,'mean':sum(values)/n if n else None,'interval':None,'chronological_fold_means':[],
         'state':'INSUFFICIENT_INDEPENDENT_CONTRACTS','method':'CONTRACT_BLOCK_PERCENTILE_BOOTSTRAP_BONFERRONI_APPROXIMATE'}
    minimum=protocol['inference']['minimum_contracts_for_interval']
    if n<minimum:return result
    rng=random.Random(protocol['inference']['bootstrap_seed'])
    repetitions=protocol['inference']['bootstrap_draws']
    draws=sorted(sum(rng.choices(values,k=n))/n for _ in range(repetitions))
    alpha=protocol['inference']['familywise_alpha']/family
    def quantile(p):
        x=p*(len(draws)-1);lo=int(x);hi=min(lo+1,len(draws)-1)
        return draws[lo]+(draws[hi]-draws[lo])*(x-lo)
    folds=protocol['inference']['chronological_folds']
    parts=[values[i*n//folds:(i+1)*n//folds] for i in range(folds)]
    result.update(interval=[quantile(alpha/2),quantile(1-alpha/2)],
        state='ESTIMATED_EXPLORATORY_INTERVAL',bootstrap_draws=repetitions,comparison_family_size=family,
        chronological_fold_means=[sum(x)/len(x) for x in parts],
        inference_limit='Approximate finite-sample bootstrap; contracts can share market regimes. No automatic promotion.')
    return result


def summarize(observations,manifest,settlements):
    protocol=manifest['protocol'];config=protocol['signal'];selections={};delays={};makers=[];counts=Counter();censors=Counter()
    for row in observations:
        if row.get('manifest_sha256')!=manifest['manifest_sha256'] or row.get('code_sha')!=manifest['code_sha']:
            raise ValueError('mixed experiment identity')
        if any(row.get(k)!=v for k,v in AUTH.items()):raise ValueError('research authority mismatch')
        counts[row['kind']]+=1
        if row['kind']=='SIGNAL_SELECTION':
            if row['origin_ns']<manifest['forward_start_ns']:raise ValueError('pre-registration contamination')
            key=row['selection_key']
            if key in selections and row!=selections[key]:raise ValueError('conflicting selection')
            selections[key]=row
        elif row['kind']=='DELAY_LABEL':
            key=(row['selection_key'],row['delay_ms'])
            if key in delays and row!=delays[key]:raise ValueError('conflicting delay label')
            delays[key]=row
            if row['state']!='OBSERVED':censors[row['state']]+=1
        elif row['kind']=='MAKER_COMPARISON':makers.append(row)
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
    maker_groups=defaultdict(lambda:defaultdict(list));maker_coverage=Counter();paired=defaultdict(list)
    for row in makers:
        outcome=settlements.get(row['market_id']);arm_pnl={}
        for arm in row['arms']:
            aid=arm['arm'];maker_coverage[aid+'|'+arm['state']]+=1
            if arm['state'] not in ('OBSERVED','FLOW_FILTER_ABSTAIN'):continue
            qty=arm['operational_filled_shares'];maker_groups[aid+'|filled_quantity'][row['market_id']].append(qty)
            maker_groups[aid+'|any_operational_fill'][row['market_id']].append(float(qty>0))
            for horizon in protocol['maker']['markout_horizons_ms']:
                cuts=[(f['quantity'],(f.get('markouts') or {}).get(str(horizon))) for f in arm['fills']]
                if qty>0 and cuts and all(v is not None for _,v in cuts):
                    maker_groups[aid+f'|mid_markout_{horizon}ms'][row['market_id']].append(sum(q*v['mid_minus_fill'] for q,v in cuts)/qty)
            if not outcome or row['token_id'] not in outcome['tokens']:continue
            y=float(outcome['winning_token_id']==row['token_id'])
            pnl=sum(f['quantity']*(y-f['price']) for f in arm['fills']);arm_pnl[aid]=pnl
            # Maker entry fee zero. Stress includes declared risk allowance and
            # is a research sensitivity, not an extra realized ledger debit.
            for stress in config['cost_stress_multipliers']:
                maker_groups[aid+f'|settlement_net_cost{stress}'][row['market_id']].append(pnl-stress*qty*config['execution_risk_per_share'])
        if 'JOIN_5S' in arm_pnl:
            for aid,pnl in arm_pnl.items():
                if aid!='JOIN_5S':paired[aid].append(pnl-arm_pnl['JOIN_5S'])
    return {'schema':'polymarket_v7_profit_experiment_report_v1',**AUTH,'code_sha':manifest['code_sha'],
        'manifest_sha256':manifest['manifest_sha256'],'timestamp_ms':time.time_ns()//1000000,'counts':dict(counts),
        'selected_contracts':len({r['market_id'] for r in selections.values()}),'resolved_selected_contracts':len(resolved),
        'signal_cells':estimates,'fixed_signal_delay_margin_change':{k:interval([sum(v)/len(v) for v in c.values()],protocol,family) for k,c in decision_decay.items()},
        'maker_coverage':dict(maker_coverage),'maker_metrics':{k:interval([sum(v)/len(v) for v in c.values()],protocol,family) for k,c in maker_groups.items()},
        'maker_paired_net_delta_vs_join5s':{k:interval(v,protocol,family) for k,v in paired.items()},
        'censored_labels':dict(censors),'economic_conclusion':'FORWARD_RESEARCH_NO_PROFITABILITY_CLAIM_OR_POLICY_PROMOTION',
        'limitations':['L1 prices and aggregate features, not full depth or queue position verification.',
            'Native PAPER fills in these comparisons are counterfactual and excluded from canonical equity.',
            'One normalized share is a price diagnostic, not proof of venue minimum-size executability.',
            'All repeats within a contract are aggregated before uncertainty estimation.',
            'Markout is fill-conditioned; settlement surplus and cost stress are separate quantities.',
            'No probability-bound narrowing, capital increase or model promotion.']}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--experiment-root',type=Path,required=True);ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();root=args.experiment_root
    if not (root/'manifest.json').exists():return
    manifest=json.loads((root/'manifest.json').read_text());observations=list(rows(root/'observations.jsonl'))
    token_sets=defaultdict(set)
    for row in observations:
        if row.get('token_id'):token_sets[row['market_id']].add(row['token_id'])
    cache=root/'settlements.json';labels=json.loads(cache.read_text()) if cache.exists() else {}
    def fetch(url):
        with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'polymarket-v7-paper-research'}),timeout=2) as response:return json.load(response)
    for market in [k for k in token_sets if k not in labels][:12]:
        try:label=settlement(market,token_sets[market],fetch)
        except (OSError,ValueError,TypeError):continue
        if label:labels[market]=label
    atomic(cache,labels)
    report=summarize(observations,manifest,labels)
    report['sources']={'observations_sha256':digest(observations),'settlements_sha256':digest(labels),'manifest':manifest}
    atomic(args.output,report)

if __name__=='__main__':main()
