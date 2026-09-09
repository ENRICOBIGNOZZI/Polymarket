"""Fixed-grid descriptive economics; all repeated observations get contract weight.

Historical rows without regime inputs remain UNKNOWN. Post-outcome movement
strata describe delay paths and must never be presented as entry-time filters.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime,timezone
import math
from v7_profit_inference import describe, interval
from v7_profit_protocol import digest

REGIMES={
    'volatility_ticks':[0,.1,1,10,1e12],
    'ask_liquidity_usd':[0,10,100,1000,1e15],
    'spread_ticks':[0,1.01,2.01,5.01,1e12],
    'utc_hour':[0,6,12,18,24],
    'external_disagreement_bp':[0,1,5,20,1e12],
    'absolute_oracle_distance_bp':[0,1,5,20,1e12],
    'external_shock_abs_100ms_bp':[0,.1,1,5,1e12],
}


def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)


def bucket(value,edges):
    if not finite(value):return 'UNKNOWN'
    return next((str(i) for i,(a,b) in enumerate(zip(edges,edges[1:])) if a<=value<b),'OUTSIDE_FIXED_GRID')


def regimes(row,grid):
    book=row.get('origin_book') or {};features=book.get('placement_features') or {}
    rich=row.get('raw_model_features') or {};rich=rich.get('features',rich)
    def abs_or_missing(v):return abs(v) if finite(v) else None
    tick=book.get('tick_size');ask=book.get('best_ask');bid=book.get('best_bid');depth=book.get('ask_depth_l1')
    values={
      'volatility_ticks':features.get('ew_vol_ticks') if book.get('features_valid') is True else None,
      'ask_liquidity_usd':ask*depth if finite(ask) and finite(depth) else None,
      'spread_ticks':(ask-bid)/tick if all(finite(v) for v in (ask,bid,tick)) and tick>0 else None,
      'utc_hour':datetime.fromtimestamp(row['origin_ns']/1e9,timezone.utc).hour,
      'external_disagreement_bp':rich.get('dispersion_bp'),
      'absolute_oracle_distance_bp':abs_or_missing(rich.get('oracle_margin_bp')),
      'external_shock_abs_100ms_bp':abs_or_missing(rich.get('return_100ms_bp'))}
    return {'outcome':row['outcome'],'margin':str(row['margin_bin']),'tte':str(row['tte_bin']),
            **{k:bucket(values[k],edges) for k,edges in grid.items()}}


def summarize_signal(selections,delays,manifest,settlements,now_ns):
    protocol=manifest['protocol'];cfg=protocol['signal'];grid=cfg.get('descriptive_regime_edges',REGIMES)
    cells=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    decay=defaultdict(lambda:defaultdict(lambda:defaultdict(list)))
    calibration=defaultdict(lambda:defaultdict(list));times={};primary=defaultdict(lambda:defaultdict(list))
    coverage=defaultdict(int);end=manifest.get('confirmatory_end_ns');start=manifest['forward_start_ns']
    def add(target,name,metric,market,value):
        if finite(value):target[name][metric][market].append(float(value))
    for row in sorted(selections.values(),key=lambda r:r['origin_ns']):
        market=row['market_id'];times.setdefault(market,row['origin_ns']);key=row['selection_key']
        region=regimes(row,grid);names=['ALL']+[k+'|'+v for k,v in region.items()]
        names+=['outcome_margin_tte|'+row['outcome']+'|'+str(row['margin_bin'])+'|'+str(row['tte_bin'])]
        p,pm=row['model_probability'],row['pm_probability'];outcome=settlements.get(market)
        if not all(finite(v) and 0<=v<=1 for v in (p,pm)):raise ValueError('invalid frozen probability')
        y=float(outcome['winning_token_id']==row['token_id']) if outcome and row['token_id'] in outcome['tokens'] else None
        coverage['selected_origins']+=1;coverage['resolved_origins']+=int(y is not None)
        for k,v in region.items():coverage[k+'|'+('missing' if v=='UNKNOWN' else 'observed')]+=1
        window=end is not None and start<=row['origin_ns']<end
        if y is not None:
            eps=1e-9
            loss=lambda x:-(y*math.log(max(eps,x))+(1-y)*math.log(max(eps,1-x)))
            metrics={'brier_model':(p-y)**2,'brier_pm':(pm-y)**2,'brier_improvement_over_pm':(pm-y)**2-(p-y)**2,
                'log_loss_model':loss(p),'log_loss_pm':loss(pm),'log_loss_improvement_over_pm':loss(pm)-loss(p),
                'model_probability':p,'pm_probability':pm,'observed_payoff':y,'calibration_error_model':p-y,'calibration_error_pm':pm-y}
            for name in names:
                for metric,value in metrics.items():add(cells,name,metric,market,value)
            for model,value in [('model',p),('pm',pm)]:
                calibration[model+'|'+str(min(9,int(value*10)))][market].append((value,y))
            if window:primary['model_brier_improvement_over_pm'][market].append(metrics['brier_improvement_over_pm'])
        valid={}
        for delay in cfg['delays_ms']:
            label=delays.get((key,delay))
            if not label or label.get('state')!='OBSERVED':continue
            if label.get('fixed_signal_probability')!=p:raise ValueError('delay changed frozen probability')
            cut=label['book_cut'];ask=cut['best_ask'];fee=label['fee_per_share'];risk=label['risk_allowance_per_share']
            if not all(finite(x) for x in (ask,fee,risk,label['point_net_margin'])):raise ValueError('incomplete observed delay economics')
            expected=p-ask-fee-risk
            if abs(expected-label['point_net_margin'])>1e-10:raise ValueError('delay economics identity mismatch')
            valid[delay]=label
            for name in names:
                add(decay,name,'EV_delay'+str(delay),market,expected)
                if y is not None:
                    for stress in cfg['cost_stress_multipliers']:
                        net=y-ask-stress*(fee+risk)
                        add(cells,name,f'settlement_surplus_delay{delay}_cost{stress}',market,net)
                        if window and name=='ALL' and delay==1000 and stress==2:
                            primary['selected_settlement_surplus_cost2_delay1000'][market].append(net)
        for a,b in zip(cfg['delays_ms'],cfg['delays_ms'][1:]):
            if a not in valid or b not in valid:continue
            delta=valid[b]['point_net_margin']-valid[a]['point_net_margin']
            price_delta=valid[b]['book_cut']['best_ask']-valid[a]['book_cut']['best_ask']
            movement='UP' if price_delta>1e-12 else 'DOWN' if price_delta< -1e-12 else 'UNCHANGED'
            for name in names+['realized_ask_movement|'+movement]:add(decay,name,f'EV_change_{a}_to_{b}',market,delta)
        if 0 in valid:
            for d,label in valid.items():
                if d==100:continue  # already the first adjacent pair; count each origin once
                for name in names:add(decay,name,f'EV_change_0_to_{d}',market,label['point_net_margin']-valid[0]['point_net_margin'])
    # Publish all specified marginal cells including empty / unavailable strata.
    fixed={'outcome':['YES','NO'],'margin':list(map(str,range(len(cfg['margin_edges'])-1))),
           'tte':list(map(str,range(len(cfg['tte_edges_seconds'])-1))),
           **{k:list(map(str,range(len(v)-1)))+['UNKNOWN','OUTSIDE_FIXED_GRID'] for k,v in grid.items()}}
    for k,bins in fixed.items():
        for b in bins:cells[k+'|'+b];decay[k+'|'+b]
    def reduce(values):
        ordered=sorted(values,key=lambda m:times.get(m,0));means=[sum(values[m])/len(values[m]) for m in ordered]
        result=describe(means);n=len(means)
        result['chronological_fold_means']=[sum(means[a*n//3:(a+1)*n//3])/len(means[a*n//3:(a+1)*n//3])
            if means[a*n//3:(a+1)*n//3] else None for a in range(3)]
        result['observations']=sum(map(len,values.values()));return result
    cal={}
    for name,contracts in calibration.items():
        cal[name]={'contracts':len(contracts),'mean_probability':sum(sum(p for p,_ in v)/len(v) for v in contracts.values())/len(contracts),
          'mean_payoff':sum(sum(y for _,y in v)/len(v) for v in contracts.values())/len(contracts)}
    return {'schema':'polymarket_v7_permanent_signal_analysis_v1','regime_edges':grid,'regime_definition_sha256':digest(grid),
      'registration_status':'PROSPECTIVELY_REGISTERED_DESCRIPTIVE_GRID' if 'descriptive_regime_edges' in cfg else 'POST_HOC_EXPLORATORY_GRID_ON_PRESERVED_LEGACY_PROTOCOL',
      'contract_weighting':'ONE_MEAN_PER_CONTRACT_WITHIN_CELL','probability_scope':'SELECTED_ORIGINS_ONLY_NOT_ALL_PM_OPPORTUNITIES',
      'cells':{k:{metric:reduce(v) for metric,v in metrics.items()} for k,metrics in sorted(cells.items())},
      'fixed_signal_delay':{k:{metric:reduce(v) for metric,v in metrics.items()} for k,metrics in sorted(decay.items())},
      'calibration_fixed_deciles':cal,'input_coverage':dict(coverage),
      'clock_interpretation':'CAUSAL_OBSERVER_RECEIVE_TIME_PRICE_RESPONSE_NOT_MEASURED_END_TO_END_INFRASTRUCTURE_ALPHA',
      'movement_strata':'REALIZED_ASK_MOVEMENT_IS_POST_ORIGIN_DESCRIPTIVE_ONLY_NOT_A_DEPLOYABLE_FILTER',
      'primary_contract_values':{k:{m:sum(v)/len(v) for m,v in by.items()} for k,by in primary.items()},
      'contract_origin_ns':times,'automatic_promotion':False}


def confirmatory(primary,times,manifest,now_ns):
    cfg=manifest['protocol']['inference'].get('confirmatory')
    if not cfg:return {'state':'NO_PROSPECTIVE_PRIMARY_ENDPOINT_REGISTRATION','automatic_promotion':False}
    end=manifest['confirmatory_end_ns'];ready=now_ns>=end;endpoints={}
    for key in cfg['primary_endpoints']:
        values=primary.get(key,{})
        ordered=[values[m] for m in sorted(values,key=lambda m:times.get(m,0))]
        result=interval(ordered,manifest['protocol'],cfg['family_size']) if ready else describe(ordered)
        result['interpretation']='FIXED_WINDOW_ANALYSIS_REQUIRES_SETTLEMENT_AND_CENSORING_COMPLETENESS_REVIEW' if ready else 'DESCRIPTIVE_PREVIEW_NOT_A_CONFIRMATORY_LOOK'
        endpoints[key]=result
    return {'state':'WINDOW_ENDED_AWAITING_COMPLETENESS_AUDIT' if ready else 'FORWARD_WINDOW_OPEN',
      'forward_start_ns':manifest['forward_start_ns'],'forward_end_ns':end,'endpoints':endpoints,
      'family_size':cfg['family_size'],'look_policy':cfg['look_policy'],'automatic_promotion':False}
